from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest
import requests

from cairn.cli import main
from cairn.controller.local import CLI
from cairn.controller.submissions import execution_snapshot, validate_server_url
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.server import db, submissions
from cairn.server.app import app
from test_kritt_audit_reconnect import ready
from test_kritt_probe import probe


ROLES = ("explore", "reason", "preflight", "seed_review", "overseer", "closing", "final_review")


def configured(probe):
    config, prepared, plan, _ = ready(probe)
    folder = Path(prepared["run_dir"])
    data = config.model_dump(mode="json")
    data["role_pools"] = {role: {"claudecode": 1} for role in ROLES}
    data["local"]["workspace_root"] = str(folder.parent / "service-work")
    data["controller"]["poll_interval"] = 1
    data["runtime"]["interval"] = 1
    config = DispatchConfig.model_validate(data)
    (folder / "run.json").write_text(json.dumps(prepared))
    (folder / "execution.json").write_text(execution_snapshot(config).model_dump_json())
    return config, prepared, plan


def wait_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("managed service did not reach the expected state")


FAKE_CLI = r'''
import fcntl, json, os, pathlib, sys, time
if "--help" in sys.argv:
    raise SystemExit(0)
assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
prompt = sys.argv[-1]
if '"output_schema"' in prompt:
    context = json.loads(prompt.rsplit("\n\n", 1)[1])
    props = context["output_schema"].get("properties", {})
    if "approved" in props:
        issues = ["keep the static deployment uncertainty"] if context["plan"]["goal"] == "revise-me" else []
        answer = {"approved": not issues, "issues": issues}
    elif "reviews" in props:
        answer = {"reviews": [{"key": seed["key"], "audit": {
            "review_status": "verified", "evidence": [{"path": "module/install/control.php", "start_line": 2}],
            "conditions": ["synthetic static source"], "validation_level": "source_analysis"}}
            for seed in context["plan"]["seeds"]]}
    else:
        target = context.get("target", {})
        answer = {"decision": "approved", "reason": "synthetic review", "hint": None,
            "fact_audit": {**target["audit"], "review_status": "verified"} if "audit" in target else None}
elif "执行一个代码审计探索" in prompt:
    release = pathlib.Path(os.environ["SERVICE_TEST_RELEASE"])
    started = release.with_suffix(".jsonl")
    with started.open("a") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.write(json.dumps({"pid": os.getpid(), "harness": pathlib.Path(sys.argv[0]).name}) + "\n")
    while not release.exists(): time.sleep(.02)
    answer = {"accepted": True, "data": {"description": "synthetic source contains requireLogin",
        "audit": {"review_status": "unverified", "evidence": [{"path": "module/install/control.php", "start_line": 2}],
        "conditions": ["synthetic static source"], "premise_ids": [], "validation_level": "source_analysis"}}}
else:
    answer = {"accepted": False, "reason": "fixture does not request another decision"}
text = json.dumps(answer)
print(json.dumps({"text": text}) if pathlib.Path(sys.argv[0]).name == "grok" else text)
'''


def test_submit_offline_is_http_only_and_does_not_start_processes(probe, monkeypatch):
    config, prepared, _ = configured(probe)
    path = Path(prepared["run_dir"]) / "config.json"
    path.write_text(config.model_dump_json())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("submission must not spawn a service"))
    monkeypatch.setattr(requests.Session, "get", lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError()))
    result = CliRunner().invoke(main, ["submit", "--server", "http://127.0.0.1:8000", "--run-dir", prepared["run_dir"],
                                      "--config", str(path)])
    assert result.exit_code == 1 and "manually" in result.output
    assert not (Path(prepared["run_dir"]) / "submission.json").exists()


def test_plain_viewer_is_not_an_execution_service(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "viewer.db")
    with TestClient(app) as client:
        health = client.get("/runtime/health").json()
        assert health["managed"] is False and health["ready"] is False
        assert health["database_id"] is None and health["service_id"] is None


@pytest.mark.parametrize("server", ["http://127.0.0.1", "http://127.0.0.1:0", "https://localhost:8000",
                                    "http://example.com:8000", "http://localhost:8000/api"])
def test_execution_service_requires_an_explicit_fixed_local_address(server):
    with pytest.raises(ValueError):
        validate_server_url(server)


def test_submission_revision_and_project_creation_are_bound_and_idempotent(probe, tmp_path, monkeypatch):
    config, prepared, plan = configured(probe)
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "submissions.db")
    class Service:
        directory = tmp_path / "service"
        service_id = "service-fixture"
        database_id = "database-fixture"

        def health(self): return {"ready": True}
        def start(self): pass
        def close(self): pass
    service = Service()
    service.config = config
    app.state.managed_service = service
    body = {"run_dir": prepared["run_dir"], "execution": execution_snapshot(config).model_dump(mode="json"),
            "database_id": service.database_id, "service_id": service.service_id}
    try:
        with TestClient(app) as client:
            first = client.post("/audit-submissions", json=body)
            assert first.status_code == 202, first.text
            submission_id = first.json()["submission_id"]
            assert client.post("/audit-submissions", json=body).json()["submission_id"] == submission_id
            with db.get_conn() as conn:
                row = submissions.get_row(conn, submission_id)
                snapshot = json.loads(row["prepared_json"])
                assert Path(snapshot["plan"]).read_text() == Path(prepared["plan"]).read_text()
            submissions.claim_next()
            submissions.finish(submission_id, "needs_revision", issues=["fixture"])
            plan["goal"] += ", preserve unknown deployment"
            Path(prepared["plan"]).write_text(json.dumps(plan))
            assert client.post("/audit-submissions", json=body).status_code == 409
            original_save = submissions.save_snapshot
            def failed_save(*args, **kwargs):
                original_save(*args, **kwargs)
                raise OSError("fixture disk interruption")
            monkeypatch.setattr(submissions, "save_snapshot", failed_save)
            assert client.put(f"/audit-submissions/{submission_id}", json={**body, "revision": 1}).status_code == 422
            assert client.get(f"/audit-submissions/{submission_id}").json()["revision"] == 1
            monkeypatch.setattr(submissions, "save_snapshot", original_save)
            revised = client.put(f"/audit-submissions/{submission_id}", json={**body, "revision": 1})
            assert revised.status_code == 202 and revised.json()["revision"] == 2
            assert client.put(f"/audit-submissions/{submission_id}", json={**body, "revision": 1}).status_code == 409
            current = submissions.claim_next()
            audit_body = {**plan, "user_goal": prepared["user_goal"], "repo_path": prepared["source_root"],
                "workspace_root": config.local.workspace_root,
                "baseline": json.loads(Path(prepared["source_baseline"]).read_text())}
            assert client.post("/audits", json=audit_body).status_code == 403
            headers = {"X-Cairn-Submission-Token": current["worker_token"]}
            created = client.post("/audits", json=audit_body, headers=headers)
            assert created.status_code == 201, created.text
            project_id = created.json()["project"]["id"]
            again = client.post("/audits", json=audit_body, headers=headers)
            assert again.json()["project"]["id"] == project_id
            assert len(client.get("/projects").json()) == 1
            assert client.get(f"/audit-submissions/{submission_id}").json()["project_id"] == project_id
            assert client.delete(f"/projects/{project_id}").status_code == 204
            assert client.get(f"/projects/{project_id}").status_code == 404
            deleted = client.get(f"/audit-submissions/{submission_id}").json()
            assert deleted["status"] == "deleted" and deleted["project_id"] is None
            assert project_id in deleted["error"]
            submissions.finish(submission_id, "error", error="late controller result")
            repeated = client.post("/audit-submissions", json=body)
            assert repeated.status_code == 202 and repeated.json()["status"] == "deleted"
            assert repeated.json()["submission_id"] == submission_id
            plan["goal"] += " changed after deletion"
            Path(prepared["plan"]).write_text(json.dumps(plan))
            assert client.post("/audit-submissions", json=body).status_code == 409
            assert client.put(f"/audit-submissions/{submission_id}", json={**body, "revision": 2}).status_code == 409
            assert client.get("/projects").json() == [] and submissions.claim_next() is None
    finally:
        del app.state.managed_service


def test_manual_up_and_client_submission_keep_service_alive_with_project_pool(probe, tmp_path, monkeypatch):
    config, prepared, plan = configured(probe)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config.server = f"http://127.0.0.1:{port}"
    config_file = tmp_path / "service.json"
    config_file.write_text(config.model_dump_json())
    binaries = tmp_path / "bin"
    for name in ("claude", "grok"):
        binary = binaries / name
        binary.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
        binary.chmod(0o755)
    release = tmp_path / "release"
    monkeypatch.setenv("SERVICE_TEST_RELEASE", str(release))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    log = (tmp_path / "service.log").open("w")
    process = subprocess.Popen([*CLI, "up", "--config", str(config_file), "--db-path", str(tmp_path / "managed.db")],
                               stdout=log, stderr=log, start_new_session=True)
    try:
        with closing(CairnClient(config.server, timeout=.5, trust_env=False)) as client:
            def ready_service():
                try: return client.service_health() if client.service_health()["ready"] else None
                except requests.RequestException: return None
            health = wait_until(ready_service)
            identity = {key: health[key] for key in ("database_id", "service_id")}
            initial = client.list_projects()
            assert initial == [] and process.poll() is None
            task_config = config.model_dump(mode="json")
            task_config["role_pools"]["explore"] = {"claudecode": 2, "grok": 1}
            snapshot = execution_snapshot(DispatchConfig.model_validate(task_config)).model_dump(mode="json")
            plan["intents"] = [{"from": ["entry"], "description": f"read synthetic direction {n}"} for n in range(5)]
            original_goal = plan["goal"]
            plan["goal"] = "revise-me"
            Path(prepared["plan"]).write_text(json.dumps(plan))
            response = client.submit_audit({"run_dir": prepared["run_dir"], "execution": snapshot, **identity})
            assert response.ok, response.text
            sid = response.data["submission_id"]
            returned = wait_until(lambda: (r if (r := client.get_submission(sid))["status"] == "needs_revision" else None))
            assert returned["issues"] and returned["project_id"] is None
            assert client.list_projects() == [] and client.service_health()["ready"]
            plan["goal"] = original_goal
            Path(prepared["plan"]).write_text(json.dumps(plan))
            revised = client.revise_submission(sid, {"run_dir": prepared["run_dir"], "execution": snapshot, "revision": 1, **identity})
            assert revised.ok and revised.data["revision"] == 2 and revised.data["submission_id"] == sid
            receipts_path = Path(prepared["run_dir"]) / "query-receipts.json"
            saved_receipts = receipts_path.read_bytes()
            receipts_path.write_text("[]")
            running = wait_until(lambda: (r if (r := client.get_submission(sid))["status"] == "running" else None))
            pid = running["project_id"]
            workspace = Path(config.local.workspace_root) / pid
            assert (workspace / "query-receipts.json").read_bytes() == saved_receipts
            started = release.with_suffix(".jsonl")
            wait_until(lambda: started.exists() and len(started.read_text().splitlines()) == 3)
            calls = [json.loads(line) for line in started.read_text().splitlines()]
            assert sorted(row["harness"] for row in calls) == ["claude", "claude", "grok"]
            effective = client.get_execution_config(pid)
            assert effective["role_pools"]["explore"] == {"claudecode": 2, "grok": 1}
            assert effective["runtime"]["max_project_workers"] == 4
            # The service default has one Explore; editing caller inputs cannot alter the admitted snapshot.
            plan["goal"] = "changed after submission"
            Path(prepared["plan"]).write_text(json.dumps(plan))
            release.touch()
            wait_until(lambda: len(client.get_project(pid).facts) >= 6)
            assert client.get_project(pid).project.mode == "audit"
            assert next(f.description for f in client.get_project(pid).facts if f.id == "goal") != plan["goal"]
            assert client.set_project_status(pid, "stopped").ok
            wait_until(lambda: client.get_submission(sid)["status"] == "paused")
            assert client.get_runtime(pid)["state"] == "paused"
            assert client.service_health()["service_id"] == health["service_id"]
            assert client.service_health()["ready"] and process.poll() is None
            assert client.get_project(pid).project.status == "stopped"
    finally:
        release.touch()
        process.send_signal(signal.SIGTERM)
        try: process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        log.close()
