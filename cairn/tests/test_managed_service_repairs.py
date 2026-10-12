from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from cairn.controller import submissions as client_module
from cairn.controller.local import CLI
from cairn.controller.service import ManagedService
from cairn.controller.submissions import execution_snapshot, submit_prepared
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.execution import run_worker_process
from cairn.server import db, submissions
from test_kritt_probe import probe
from test_managed_service import configured, wait_until


def test_dispatch_failure_invalidates_readiness_and_submissions_before_cleanup(probe, tmp_path, monkeypatch):
    config, _, _ = configured(probe)
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "fault.db")
    service = ManagedService(config, path, tmp_path / "service")
    dispatcher = service.dispatcher
    monkeypatch.setattr(dispatcher, "run_startup_healthchecks", lambda: None)
    monkeypatch.setattr(dispatcher, "_validate_server_settings", lambda: None)
    def broken_projects():
        raise ValueError("synthetic malformed project response")
    monkeypatch.setattr(dispatcher.client, "list_projects", broken_projects)
    with db.get_conn() as conn:
        for name, status in (("queued", "pending"), ("preparing", "preparing")):
            conn.execute("INSERT INTO audit_submissions(id,run_dir,revision,fingerprint,status,prepared_json,execution_json,created_at,updated_at) VALUES (?,?,1,'fixture',?,'{}',?,'fixture','fixture')",
                         (name, str(tmp_path / name), status, config.model_dump_json()))
    worker = config.pool_workers("explore")[0]
    workspace = dispatcher.container_manager.ensure_running("synthetic")
    token = dispatcher._new_cancellation()
    pool = ThreadPoolExecutor(max_workers=1)
    dispatcher.project_executors["synthetic"] = pool
    future = pool.submit(run_worker_process, dispatcher.container_manager, workspace, worker,
        [sys.executable, "-c", "import time; time.sleep(30)"], phase="synthetic", timeout_seconds=30, cancellation=token)
    dispatcher.futures[future] = RunningTask("synthetic", "explore", worker.name, token, intent_id="i1")
    review_token = TaskCancellation()
    review = service.executor.submit(run_worker_process, dispatcher.container_manager, workspace, worker,
        [sys.executable, "-c", "import time; time.sleep(30)"], phase="synthetic-review", timeout_seconds=30, cancellation=review_token)
    service.jobs["preparing"] = (review, review_token)
    service.control_thread = threading.Thread(target=lambda: service._stop.wait(10), daemon=True)
    entered, release = threading.Event(), threading.Event()
    original_close = dispatcher.close
    def held_close():
        entered.set()
        release.wait(10)
        original_close()
    monkeypatch.setattr(dispatcher, "close", held_close)
    try:
        service.start()
        assert entered.wait(5), "failure must cancel before waiting for pool cleanup"
        wait_until(lambda: future.done() and review.done(), seconds=5)
        health = service.health()
        assert service.dispatch_thread.is_alive() and not health["ready"]
        assert not health["dispatcher_ready"] and "malformed" in health["error"]
        assert token.is_cancelled and review_token.is_cancelled
        with db.get_conn() as conn:
            rows = conn.execute("SELECT status,error FROM audit_submissions").fetchall()
        assert len(rows) == 2 and all(row["status"] == "error" and "malformed" in row["error"] for row in rows)
        submissions.finish("preparing", "needs_revision", issues=["late review"])
        with db.get_conn() as conn:
            assert submissions.get_row(conn, "preparing")["status"] == "error"
    finally:
        release.set()
        service.close()


def test_up_with_occupied_port_leaves_existing_database_untouched(probe, tmp_path, monkeypatch):
    config, _, _ = configured(probe)
    monkeypatch.setattr(db, "_db_path", None)
    database = tmp_path / "occupied.db"
    db.configure(database)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id,title,status,created_at,mode) VALUES ('synthetic','fixture','active','fixture','audit')")
        conn.execute("INSERT INTO audit_submissions(id,run_dir,revision,fingerprint,status,prepared_json,execution_json,project_id,created_at,updated_at) VALUES ('fixture',?,1,'fixture','running','{}',?,'synthetic','fixture','fixture')",
                     (str(tmp_path / "run"), config.model_dump_json()))
    before = database.read_bytes()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        config.server = f"http://127.0.0.1:{listener.getsockname()[1]}"
        path = tmp_path / "config.json"
        path.write_text(config.model_dump_json())
        result = subprocess.run([*CLI, "up", "--config", str(path), "--db-path", str(database)],
                                capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and "address already in use" in result.stderr.lower()
    assert database.read_bytes() == before
    assert not (tmp_path / "occupied-service").exists()
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM projects").fetchone()[0] == "active"
        assert conn.execute("SELECT status FROM audit_submissions").fetchone()[0] == "running"
        assert conn.execute("SELECT count(*) FROM service_identity").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["database_id", "service_id"])
def test_server_checks_expected_identity_before_reading_or_creating_inputs(probe, tmp_path, monkeypatch, field):
    config, _, _ = configured(probe)
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "identity.db")
    service = SimpleNamespace(config=config, directory=tmp_path / "service", database_id="db-A", service_id="instance-A",
                              health=lambda: {"ready": True})
    body = {"run_dir": str(tmp_path / "missing-run"), "execution": execution_snapshot(config).model_dump(mode="json"),
            "database_id": service.database_id, "service_id": service.service_id}
    body[field] = "different"
    with pytest.raises(HTTPException) as rejected:
        submissions.accept(service, submissions.SubmissionRequest.model_validate(body))
    assert rejected.value.status_code == 409
    assert not service.directory.exists()
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM audit_submissions").fetchone()[0] == 0


def test_repeated_submit_to_another_database_preserves_locator_and_never_posts(probe, monkeypatch):
    _, prepared, _ = configured(probe)
    folder = Path(prepared["run_dir"])
    locator = folder / "submission.json"
    original = {"submission_id": "original", "database_id": "db-A", "service_id": "old-instance",
                "run_dir": str(folder), "status": "running"}
    locator.write_text(json.dumps(original))
    before = locator.read_bytes()
    class Client:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def service_health(self):
            return {"managed": True, "ready": True, "database_id": "db-B", "service_id": "new-instance"}
        def submit_audit(self, body): pytest.fail("wrong database must be rejected before POST")
        def get_submission(self, sid): pytest.fail("wrong database must be rejected before looking up a same-named ID")
    monkeypatch.setattr(client_module, "CairnClient", Client)
    with pytest.raises(ValueError, match="another Cairn database"):
        submit_prepared("http://127.0.0.1:8000", folder, wait=False)
    assert locator.read_bytes() == before


def test_same_database_new_service_instance_reuses_submission_and_pins_request(probe, monkeypatch):
    _, prepared, _ = configured(probe)
    folder = Path(prepared["run_dir"])
    old = {"submission_id": "original", "database_id": "db-A", "service_id": "old-instance",
           "run_dir": str(folder), "status": "running", "project_id": "proj_001"}
    (folder / "submission.json").write_text(json.dumps(old))
    current = {**old, "service_id": "new-instance"}
    posted = []
    class Client:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def service_health(self):
            return {"managed": True, "ready": True, "database_id": "db-A", "service_id": "new-instance"}
        def get_submission(self, sid):
            assert sid == "original"
            return current
        def submit_audit(self, body):
            posted.append(body)
            return SimpleNamespace(ok=True, status_code=202, data=current)
    monkeypatch.setattr(client_module, "CairnClient", Client)
    result = submit_prepared("http://127.0.0.1:8000", folder, wait=False)
    assert result["submission_id"] == old["submission_id"] and len(posted) == 1
    assert posted[0]["database_id"] == "db-A" and posted[0]["service_id"] == "new-instance"


def test_waiting_submit_exits_when_execution_components_fail(probe, monkeypatch):
    _, prepared, _ = configured(probe)
    folder = Path(prepared["run_dir"])
    result = {"submission_id": "original", "database_id": "db-A", "service_id": "instance-A",
              "run_dir": str(folder), "status": "pending", "project_id": None}
    class Client:
        checks = 0
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def service_health(self):
            self.checks += 1
            return {"managed": True, "ready": self.checks == 1, "error": "Dispatcher failed",
                    "database_id": "db-A", "service_id": "instance-A"}
        def get_submission(self, sid): return result
        def submit_audit(self, body): return SimpleNamespace(ok=True, status_code=202, data=result)
    monkeypatch.setattr(client_module, "CairnClient", Client)
    with pytest.raises(ValueError, match="restart it manually: Dispatcher failed"):
        submit_prepared("http://127.0.0.1:8000", folder)
