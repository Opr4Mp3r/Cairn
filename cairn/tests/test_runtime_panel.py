from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time

import pytest

from cairn.controller.service import ManagedService
from cairn.controller import submissions
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.runtime.execution import run_worker_process
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.protocol.client import ApiResult
from cairn.server import db, runtime, services
from cairn.server.app import app
from test_audit_review import audit
from test_audit_workflow import api


@pytest.fixture
def panel(audit, tmp_path, monkeypatch):
    client, http, config, pid, repo = audit
    data = config.model_dump(mode="json")
    data["role_pools"] = {role: {"claudecode": 1} for role in
                          ("reason", "explore", "preflight", "seed_review", "overseer", "closing", "final_review")}
    data["role_pools"]["explore"] = {"claudecode": 3, "grok": 2}
    config = DispatchConfig.model_validate(data)
    service = ManagedService.__new__(ManagedService)
    service.config, service.directory = config, tmp_path / "service"
    service.database_id, service.service_id = "panel-db", "panel-service"
    service.jobs, service.backends, service._pause_cleanups = {}, {}, {}
    service.dispatcher = type("Dispatcher", (), {"container_manager": LocalBackend(config.local)})()
    service.health = lambda: {"ready": True, "managed": True, "database_id": service.database_id,
                             "service_id": service.service_id}
    monkeypatch.setattr(app.state, "managed_service", service, raising=False)
    with db.get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO service_identity VALUES(1,?)", (service.database_id,))
        runtime.initialize(conn, pid)
        runtime.activate(conn, pid)
        conn.execute("INSERT INTO audit_submissions(id,run_dir,revision,fingerprint,status,prepared_json,execution_json,project_id,created_at,updated_at) VALUES('panel',?,1,'x','running','{}',?,?,'now','now')",
                     (str(tmp_path / "run"), config.model_dump_json(), pid))
        workspace = services.audit_metadata(conn, pid).workspace_path
    return http, service, pid, config, Path(workspace), repo


def test_runtime_panel_uses_fixed_pool_and_actual_calls(panel):
    http, service, pid, config, workspace, _ = panel
    backend = service.dispatcher.container_manager
    worker = config.pool_workers("explore")[0]
    other = workspace.parent / "other"
    with backend.observe_call(workspace, worker, "explore_execute"), backend.observe_call(other, worker, "explore_execute"):
        data = http.get(f"/projects/{pid}/runtime").json()
        assert data["role_pools"]["explore"] == {"claudecode": 3, "grok": 2}
        assert data["slots_in_use"]["explore"] == {"claudecode": 1, "grok": 0}
        assert data["available_actions"] == ["pause"]
        assert data["occupancy_known"] and data["updated_at"]
        assert "cairn up" in data["manual_start"]
    assert http.get(f"/projects/{pid}/runtime").json()["slots_in_use"]["explore"]["claudecode"] == 0
    service.health = lambda: {"ready": False, "error": "controller stopped"}
    data = http.get(f"/projects/{pid}/runtime").json()
    assert data["slots_in_use"] is None and not data["occupancy_known"]
    assert not data["available_actions"] and data["blocked_reason"] == "controller stopped"


def test_controller_calls_share_the_operational_snapshot(panel):
    http, service, pid, config, _, _ = panel
    backend = LocalBackend(config.local)
    service.backends["panel"] = backend
    worker = config.pool_workers("overseer")[0]
    with backend.observe_call(service.directory, worker, "overseer_format_repair"):
        data = http.get(f"/projects/{pid}/runtime").json()
        assert data["slots_in_use"]["overseer"][worker.type] == 1


def test_runtime_controls_check_stale_generation_and_disable_transitions(panel):
    http, service, pid, _, _, _ = panel
    body = {"database_id": service.database_id, "service_id": service.service_id}
    assert http.post(f"/projects/{pid}/runtime/pause", json=body,
                     headers={"X-Cairn-Generation": "0"}).status_code == 409
    result = http.post(f"/projects/{pid}/runtime/pause", json=body,
                       headers={"X-Cairn-Generation": "1"})
    assert result.status_code == 202
    assert result.json()["state"] == "pausing" and not result.json()["available_actions"]
    with db.get_conn() as conn:
        runtime.freeze(conn, pid, "paused")
        conn.execute("UPDATE audit_submissions SET status='paused' WHERE project_id=?", (pid,))
    assert http.get(f"/projects/{pid}/runtime").json()["available_actions"] == ["resume"]
    service._pause_cleanups[pid] = 1
    assert not http.get(f"/projects/{pid}/runtime").json()["available_actions"]
    del service._pause_cleanups[pid]
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status='budget_exhausted' WHERE id=?", (pid,))
        runtime.freeze(conn, pid, "finished")
    assert not http.get(f"/projects/{pid}/runtime").json()["available_actions"]


def test_logs_are_bounded_and_project_scoped(panel, tmp_path):
    http, _, pid, _, workspace, _ = panel
    folder = workspace / "research/generation-001"
    folder.mkdir(parents=True)
    (folder / "explore.json").write_text("a" * 40000 + "end")
    outside = tmp_path / "other-project.json"
    outside.write_text("private to the other project")
    (folder / "external.json").symlink_to(outside)
    response = http.get(f"/projects/{pid}/runtime/logs")
    assert response.status_code == 200
    data = response.json()
    assert data["files"][0]["id"] == "research/generation-001/explore.json"
    assert len(data["text"].encode()) == 32768 and data["text"].endswith("end")
    for name in ("../other-project.json", str(outside), "research/generation-001/external.json"):
        assert http.get(f"/projects/{pid}/runtime/logs", params={"log": name}).status_code == 404
    assert http.get("/projects/missing/runtime/logs").status_code == 404


def test_observation_tracks_real_process_and_releases_after_exit(panel, tmp_path):
    _, _, _, config, _, _ = panel
    backend = LocalBackend(config.local)
    worker = config.pool_workers("explore")[0]
    release = tmp_path / "release"
    script = "import pathlib,time,sys;p=pathlib.Path(sys.argv[1]);\nwhile not p.exists(): time.sleep(.01)\nprint('done')"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_worker_process, backend, str(tmp_path), worker,
                                 [sys.executable, "-c", script, str(release)], phase="explore_execute", timeout_seconds=5)
        try:
            end = time.monotonic() + 2
            while not backend.active_calls() and time.monotonic() < end:
                time.sleep(.01)
            assert backend.active_calls() == [{"role": "explore", "harness": worker.type, "count": 1}]
        finally:
            release.touch()
        assert future.result(timeout=5).stdout.strip() == "done"
    assert not backend.active_calls()


def test_history_viewer_keeps_database_identity_and_read_only_runtime(panel, monkeypatch):
    http, _, pid, _, _, _ = panel
    monkeypatch.delattr(app.state, "managed_service")
    health = http.get("/runtime/health").json()
    data = http.get(f"/projects/{pid}/runtime").json()
    assert health["database_id"] == data["database_id"] == "panel-db"
    assert not data["managed"] and not data["available_actions"]
    assert data["slots_in_use"] is None


def test_legacy_run_directory_still_resolves_before_a_control_request(panel, tmp_path, monkeypatch):
    http, service, pid, _, _, _ = panel
    with db.get_conn() as conn:
        conn.execute("DELETE FROM audit_submissions WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM project_runtime WHERE project_id=?", (pid,))
    folder = db._db_path.parent
    (folder / "project.json").write_text(json.dumps({"project_id": pid}))
    data = http.get(f"/projects/{pid}/runtime").json()
    assert data["clock_mode"] == "legacy" and data["run_dir"] == str(folder)
    assert data["submission_id"] is None
    requests = []
    class Client:
        def __init__(self, *_args, **_kwargs): pass
        def close(self): pass
        def service_health(self):
            return {"managed": True, "ready": True, "database_id": service.database_id,
                    "service_id": service.service_id}
        def get_runtime(self, project_id): return http.get(f"/projects/{project_id}/runtime").json()
        def control_runtime(self, project_id, action, body):
            requests.append((project_id, action, body))
            return ApiResult(202, {"project_id": project_id})
    monkeypatch.setattr(submissions, "CairnClient", Client)
    result = submissions.control_prepared("http://127.0.0.1:8000", "resume", run_dir=folder, remaining_seconds=10)
    assert result["project_id"] == pid and requests[0][2]["remaining_seconds"] == 10
    wrong = tmp_path / "wrong-run"
    wrong.mkdir()
    (wrong / "project.json").write_text(json.dumps({"project_id": pid}))
    with pytest.raises(ValueError, match="another Cairn project"):
        submissions.control_prepared("http://127.0.0.1:8000", "resume", run_dir=wrong, remaining_seconds=10)
    assert len(requests) == 1
