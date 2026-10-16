from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from click.testing import CliRunner
from fastapi import HTTPException
import pytest
import requests

from cairn.cli import main
from cairn.controller.local import CLI
from cairn.controller.submissions import execution_snapshot
from cairn.dispatcher.models import ReasonCheckpoint
from cairn.dispatcher.protocol.client import CairnClient
from cairn.server import db, runtime, services
from cairn.server.models import CompletionClaimRequest

from test_audit_review import audit, completion, claim, finish, review_body, node_review
from test_audit_workflow import api
from test_kritt_probe import probe
from test_managed_service import configured, wait_until, FAKE_CLI
from test_scheduler_logic import _loop, _summary


@pytest.fixture
def active(audit, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(runtime, "monotonic", lambda: now[0])
    client, http, _, pid, _ = audit
    with db.get_conn() as conn:
        runtime.initialize(conn, pid)
        runtime.activate(conn, pid)
    http.headers["X-Cairn-Generation"] = "1"
    return audit, now


def settle_to(audit, elapsed):
    with db.get_conn() as conn:
        data = runtime.load(conn, audit[3])
        data.update(elapsed=elapsed, anchor=runtime.monotonic(),
                    valid_until=runtime.monotonic()+runtime.LEASE_SECONDS)
        runtime.save(conn, audit[3], data)


def pause_and_resume(audit):
    with db.get_conn() as conn:
        runtime.pause(conn, audit[3])
        runtime.freeze(conn, audit[3], "paused")
        runtime.resume(conn, services.audit_metadata(conn, audit[3]))
        runtime.activate(conn, audit[3])
        conn.execute("UPDATE projects SET status=? WHERE id=?",
                     ("active" if services.audit_metadata(conn, audit[3]).phase == "researching" else "draining", audit[3]))
    audit[1].headers["X-Cairn-Generation"] = "2"


def test_twenty_minutes_then_day_off_preserves_forty_minutes(active):
    audit, now = active
    with db.get_conn() as conn:
        meta = services.audit_metadata(conn, audit[3])
        meta.budget.research_seconds = 3600
        services.save_audit_metadata(conn, meta)
    # Renew the real persisted clock, rather than jumping past its lease.
    for _ in range(600):
        now[0] += 2
        with db.get_conn() as conn:
            runtime.heartbeat(conn)
    with db.get_conn() as conn:
        runtime.pause(conn, audit[3])
        runtime.freeze(conn, audit[3], "paused")
    now[0] += 86400
    data = audit[0].get_runtime(audit[3])
    assert data["elapsed_seconds"] == 1200
    assert data["research_remaining_seconds"] == 2400
    pause_and_resume(audit)
    assert audit[0].get_runtime(audit[3])["research_remaining_seconds"] == 2400


def test_restart_drops_previous_monotonic_anchor_and_requires_resume(active, monkeypatch):
    audit, now = active
    now[0] += 2
    with db.get_conn() as conn:
        runtime.heartbeat(conn)
    monkeypatch.setattr(runtime, "INSTANCE", "new-service-instance")
    now[0] += 86400
    with db.get_conn() as conn:
        runtime.restart(conn)
    data = audit[0].get_runtime(audit[3])
    assert data["elapsed_seconds"] == 2 and data["state"] == "interrupted"
    assert audit[0].create_intent(audit[3], ["f001"], "stale call", "old").status_code == 409
    pause_and_resume(audit)
    assert audit[0].get_runtime(audit[3])["generation"] == 2


def test_missed_heartbeat_caps_time_and_rejects_calls(active):
    audit, now = active
    now[0] += 24*3600
    assert audit[0].get_runtime(audit[3])["elapsed_seconds"] == runtime.LEASE_SECONDS
    with db.get_conn() as conn:
        runtime.heartbeat(conn)
    assert audit[0].get_runtime(audit[3])["state"] == "interrupted"
    assert audit[0].create_intent(audit[3], ["f001"], "expired call", "old").status_code == 409


@pytest.mark.parametrize("gap", [2, 8, 10.04, 15, 59.9])
def test_delayed_heartbeat_renews_without_interrupting_or_resetting_budget(active, caplog, gap):
    audit, now = active
    now[0] += gap
    with db.get_conn() as conn:
        runtime.require_running(conn, audit[3])
        runtime.heartbeat(conn)
        data = runtime.load(conn, audit[3])
        assert conn.execute("SELECT status FROM projects WHERE id=?", (audit[3],)).fetchone()[0] == "active"
    assert data["state"] == "running" and data["generation"] == 1
    assert data["elapsed"] == pytest.approx(gap)
    assert data["valid_until"] == pytest.approx(now[0] + 60)
    assert not any(event["event"] == "interrupted" for event in data["history"])
    warnings = [record for record in caplog.records if "runtime heartbeat delayed" in record.message]
    assert len(warnings) == (1 if gap >= 10 else 0)


@pytest.mark.parametrize("gap", [60, 61])
def test_hard_lease_expiry_still_requires_explicit_resume(active, gap):
    audit, now = active
    now[0] += gap
    with db.get_conn() as conn:
        with pytest.raises(HTTPException) as expired:
            runtime.require_running(conn, audit[3])
        assert expired.value.status_code == 409
        runtime.heartbeat(conn)
        data = runtime.load(conn, audit[3])
    assert data["state"] == "interrupted" and data["elapsed"] == 60
    now[0] += 2
    with db.get_conn() as conn:
        runtime.heartbeat(conn)
        assert runtime.load(conn, audit[3])["state"] == "interrupted"


def test_wall_clock_changes_do_not_change_business_budget(active, monkeypatch):
    audit, now = active
    for wall_time in ("2099-01-01T00:00:00Z", "2000-01-01T00:00:00Z"):
        monkeypatch.setattr(services, "utcnow", lambda: wall_time)
        now[0] += 2
        with db.get_conn() as conn:
            runtime.heartbeat(conn)
        assert audit[0].get_runtime(audit[3])["elapsed_seconds"] == now[0]-100
        assert audit[0].audit_budget(audit[3]).data["status"] == "active"


def test_round_attempt_deadlines_pause_and_interrupted_review_can_retry(active):
    audit, now = active
    request = claim(audit, completion(audit)["id"])
    assert request["attempt_deadline"] is None and request["attempt_deadline_seconds"] == 30
    with db.get_conn() as conn:
        runtime.pause(conn, audit[3])
        runtime.freeze(conn, audit[3], "paused")
    now[0] += 86400
    assert audit[0].list_completions(audit[3])[0].status == "pending"
    assert audit[0].get_runtime(audit[3])["review_remaining_seconds"][request["id"]] == 30
    pause_and_resume(audit)
    saved = audit[0].list_completions(audit[3])[0]
    assert saved.status == "review_error" and saved.review_token is None
    assert finish(audit, request, review_body(audit, request)).status_code == 409
    replacement = claim(audit, request["id"])
    assert replacement["attempts"] == 1
    assert finish(audit, replacement, review_body(audit, replacement)).json()["status"] == "approved"
    assert finish(audit, replacement, review_body(audit, replacement)).status_code == 200
    with db.get_conn() as conn:
        with pytest.raises(HTTPException):
            runtime.resume(conn, services.audit_metadata(conn, audit[3]))


def test_generation_is_checked_again_inside_write_transaction(active, monkeypatch):
    audit, _now = active
    original = services.next_hint_id
    # The handler is reached with generation 1, then a new execution generation
    # is observed before commit. The whole stale write must roll back.
    import cairn.server.routers.hints as hints
    def changed_generation(conn, pid):
        value = original(conn, pid)
        data = runtime.load(conn, pid)
        data["generation"] = 2
        runtime.save(conn, pid, data)
        return value
    monkeypatch.setattr(hints, "next_hint_id", changed_generation)
    count = len(audit[0].get_project(audit[3]).hints)
    assert audit[1].post(f"/projects/{audit[3]}/hints", json={"content": "stale hint", "creator": "old"}).status_code == 409
    assert len(audit[0].get_project(audit[3]).hints) == count
    assert audit[0].get_runtime(audit[3])["generation"] == 1


def test_old_generation_cannot_reclaim_an_unfinished_intent(active):
    audit, _now = active
    intent = next(i for i in audit[0].get_project(audit[3]).intents if i.to is None)
    old = audit[0].heartbeat(audit[3], intent.id, "same-worker")
    assert old.ok
    pause_and_resume(audit)
    audit[1].headers["X-Cairn-Generation"] = "1"
    assert audit[0].heartbeat(audit[3], intent.id, "same-worker").status_code == 409
    assert audit[0].conclude(audit[3], intent.id, "same-worker", "stale result").status_code == 409
    audit[1].headers["X-Cairn-Generation"] = "2"
    assert audit[0].heartbeat(audit[3], intent.id, "same-worker").ok
    assert audit[0].conclude(audit[3], intent.id, "same-worker", "new result").ok
    assert sum(f.description == "new result" for f in audit[0].get_project(audit[3]).facts) == 1


@pytest.mark.parametrize("phase,cap", [("researching", "research_seconds"), ("draining", "drain_seconds"), ("finalizing", None)])
def test_legacy_budget_is_explicit_bounded_and_one_time(audit, phase, cap):
    with db.get_conn() as conn:
        meta = services.audit_metadata(conn, audit[3])
        meta.phase = phase
        services.save_audit_metadata(conn, meta)
    with pytest.raises(HTTPException) as failure, db.get_conn() as conn:
        runtime.resume(conn, services.audit_metadata(conn, audit[3]))
    assert failure.value.detail["status"] == "needs_budget"
    meta = audit[0].get_audit(audit[3])
    maximum = getattr(meta.budget, cap) if cap else meta.budget.drain_seconds + meta.budget.final_review_seconds
    with pytest.raises(HTTPException) as failure, db.get_conn() as conn:
        runtime.resume(conn, services.audit_metadata(conn, audit[3]), maximum+1)
    assert failure.value.status_code == 422
    with db.get_conn() as conn:
        migrated = runtime.resume(conn, services.audit_metadata(conn, audit[3]), 10)
        first_generation = migrated["generation"]
        assert runtime.resume(conn, services.audit_metadata(conn, audit[3]))["generation"] == first_generation
    assert audit[0].get_runtime(audit[3])["stage_remaining_seconds"] == 10
    with pytest.raises(HTTPException), db.get_conn() as conn:
        runtime.resume(conn, services.audit_metadata(conn, audit[3]), 10)


def test_finalizing_resume_invalidates_interrupted_closing_and_never_opens_explore(active):
    audit, _now = active
    assert audit[0].record_audit_review(audit[3], node_review(audit)).ok
    settle_to(audit, audit[0].get_audit(audit[3]).budget.research_seconds)
    assert audit[0].audit_budget(audit[3]).data["audit"]["phase"] == "finalizing"
    closing = audit[0].claim_closing(audit[3], CompletionClaimRequest(owner="controller", timeout_seconds=30)).data
    pause_and_resume(audit)
    meta = audit[0].get_audit(audit[3])
    assert meta.phase == "finalizing" and meta.closing.token is None
    assert meta.closing.status == "error" and meta.closing.attempts == 0
    intent = next(i for i in audit[0].get_project(audit[3]).intents if i.to is None)
    assert audit[0].heartbeat(audit[3], intent.id, "new-worker").status_code == 403
    replacement = audit[0].claim_closing(audit[3], CompletionClaimRequest(owner="controller", timeout_seconds=30))
    assert replacement.ok and replacement.data["token"] != closing["token"]


def test_recovery_does_not_synthesize_a_consumed_reason_checkpoint():
    loop = _loop()
    summary = _summary("p001", "active")
    summary.unclaimed_intent_count = 1
    summary.runtime_generation = 1
    loop._initialize_reason_checkpoints([summary])
    assert summary.id in loop.reason_checkpoints
    summary.runtime_generation = 2
    loop._initialize_reason_checkpoints([summary])
    assert summary.id not in loop.reason_checkpoints
    loop.reason_checkpoints[summary.id] = ReasonCheckpoint(fact_count=2, hint_count=0, open_intent_count=1)
    loop._initialize_reason_checkpoints([summary])
    assert summary.id in loop.reason_checkpoints


def test_resume_offline_is_http_only(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("resume must not start a service"))
    monkeypatch.setattr(requests.Session, "get", lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError()))
    result = CliRunner().invoke(main, ["resume", "--server", "http://127.0.0.1:8000", "--project", "p001"])
    assert result.exit_code == 1 and "manually" in result.output


RECOVERY_CLI = FAKE_CLI.replace(
    'while not release.exists(): time.sleep(.02)',
    'if len(started.read_text().splitlines()) > 1:\n        while not release.exists(): time.sleep(.02)'
).replace(
    '"harness": pathlib.Path(sys.argv[0]).name}',
    '"harness": pathlib.Path(sys.argv[0]).name, "cwd": os.getcwd(), "session": sys.argv[sys.argv.index("--session-id") + 1]}'
).replace(
    'else:\n    answer = {"accepted": False, "reason": "fixture does not request another decision"}',
    '''elif "审计研究判断" in prompt:
    with pathlib.Path(os.environ["SERVICE_TEST_RELEASE"]).with_suffix(".reasons").open("a") as log:
        log.write(json.dumps({"cwd": os.getcwd(), "pid": os.getpid()}) + "\\n")
    answer = {"accepted": True, "data": {}}
else:
    answer = {"accepted": False, "reason": "fixture does not request another decision"}'''
)


def test_real_service_pause_crash_and_double_resume_continue_original_graph(probe, tmp_path, monkeypatch):
    config, prepared, plan = configured(probe)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        config.server = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config_file = tmp_path / "service.json"
    config_file.write_text(config.model_dump_json())
    binary = tmp_path / "bin" / "claude"
    binary.write_text(f"#!{sys.executable}\n{RECOVERY_CLI}")
    binary.chmod(0o755)
    release = tmp_path / "release"
    started = release.with_suffix(".jsonl")
    reasons = release.with_suffix(".reasons")
    monkeypatch.setenv("SERVICE_TEST_RELEASE", str(release))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    plan["intents"] = [{"from": ["entry"], "description": f"read synthetic direction {n}"} for n in range(4)]
    Path(prepared["plan"]).write_text(json.dumps(plan))
    database = tmp_path / "managed.db"
    log = (tmp_path / "service.log").open("w")
    process = None
    def start_service():
        return subprocess.Popen([*CLI, "up", "--config", str(config_file), "--db-path", str(database)],
                                stdout=log, stderr=log, start_new_session=True)
    def calls():
        return [json.loads(line) for line in started.read_text().splitlines()] if started.exists() else []
    def exists(pid):
        try:
            import os
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    try:
        process = start_service()
        with closing(CairnClient(config.server, timeout=2, trust_env=False)) as client:
            def healthy():
                try: return (h if (h := client.service_health())["ready"] else None)
                except requests.RequestException: return None
            first_health = wait_until(healthy)
            from cairn.controller.submissions import submit_prepared
            submission = submit_prepared(config.server, Path(prepared["run_dir"]))
            pid, sid = submission["project_id"], submission["submission_id"]
            assert submission["status"] == "running"
            wait_until(lambda: len(calls()) >= 2)
            wait_until(lambda: len(client.get_project(pid).facts) == 4)
            original = client.get_project(pid)
            original_ids = {f.id: f.description for f in original.facts}
            interrupted = next(i.id for i in original.intents if i.worker and i.to is None)
            result = CliRunner().invoke(main, ["pause", "--server", config.server, "--run-dir", prepared["run_dir"]])
            assert result.exit_code == 0, result.output
            frozen = wait_until(lambda: (r if (r := client.get_runtime(pid))["state"] == "paused" else None))
            wait_until(lambda: not exists(calls()[1]["pid"]))
            assert process.poll() is None and client.service_health()["ready"]
            time.sleep(.1)
            assert client.get_runtime(pid)["elapsed_seconds"] == frozen["elapsed_seconds"]
            identity = {k: first_health[k] for k in ("database_id", "service_id")}
            reason_count_before_resume = len(reasons.read_text().splitlines()) if reasons.exists() else 0
            def resume():
                with closing(CairnClient(config.server, trust_env=False)) as caller:
                    return caller.control_runtime(pid, "resume", identity)
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda _: resume(), range(2)))
            assert all(r.ok for r in responses), [r.text for r in responses]
            running = wait_until(lambda: (r if (r := client.get_runtime(pid))["state"] == "running" else None))
            assert running["generation"] == 2
            wait_until(lambda: len(calls()) >= 3)
            assert calls()[1]["session"] != calls()[2]["session"]
            assert len(client.list_projects()) == 1
            def resumed_reason_finished():
                if not reasons.exists() or len(reasons.read_text().splitlines()) <= reason_count_before_resume:
                    return None
                project = client.get_project(pid)
                return project if project.project.reason is None else None
            settled = wait_until(resumed_reason_finished)
            assert settled.project.reason is None
            assert len(reasons.read_text().splitlines()) > reason_count_before_resume
            assert client.get_submission(sid)["project_id"] == pid
            workspace = Path(config.local.workspace_root) / pid
            original_materials = (workspace / "kritt-materials.json").read_bytes()
            process.kill()
            process.wait(timeout=5)
            (workspace / "input.json").unlink()
            (workspace / "kritt-materials.json").unlink()
            Path(prepared["materials"]).write_text('{"updated": "caller inputs changed after admission"}')
            orphan = calls()[2]["pid"]
            assert exists(orphan)
            time.sleep(.1)
            process = start_service()
            second_health = wait_until(healthy)
            assert second_health["database_id"] == first_health["database_id"]
            assert second_health["service_id"] != first_health["service_id"]
            wait_until(lambda: not exists(orphan))
            restored = client.get_runtime(pid)
            assert restored["state"] == "interrupted"
            assert restored["elapsed_seconds"] <= running["elapsed_seconds"]+2.5
            assert client.get_submission(sid)["status"] == "interrupted"
            assert len(calls()) == 3
            result = CliRunner().invoke(main, ["resume", "--server", config.server, "--run-dir", prepared["run_dir"]])
            assert result.exit_code == 0, result.output
            wait_until(lambda: len(calls()) >= 4)
            assert client.get_runtime(pid)["generation"] == 3
            assert (workspace / "kritt-materials.json").read_bytes() == original_materials
            stale = client.for_generation(2)
            try:
                assert stale.conclude(pid, interrupted, "pool-explore-claudecode-1", "stale result").status_code == 409
                assert stale.set_project_status(pid, "stopped").status_code == 409
            finally:
                stale.close()
            release.touch()
            wait_until(lambda: len(client.get_project(pid).facts) >= 5)
            recovered = client.get_project(pid)
            assert all(next(f.description for f in recovered.facts if f.id == key) == text for key, text in original_ids.items())
            assert next(i for i in recovered.intents if i.id == interrupted).to is not None
            receipts = Path(config.local.workspace_root) / pid / "research"
            assert (receipts / "generation-001").exists()
            assert (receipts / "generation-003").exists()
            assert client.set_project_status(pid, "stopped").ok
            wait_until(lambda: client.get_runtime(pid)["state"] == "paused")
            assert client.service_health()["ready"]
    finally:
        release.touch()
        if process and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try: process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()


def test_legacy_native_run_is_adopted_without_creating_a_new_project(probe, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from cairn.server.app import app
    from cairn.controller.service import ManagedService
    from test_audit_api import prepare
    from cairn.controller import submissions as controls
    from cairn.dispatcher.protocol.client import ApiResult
    config, prepared, plan = configured(probe)
    folder = Path(prepared["run_dir"])
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(folder / "cairn.db")
    (folder / "config.yaml").write_text(config.model_dump_json())
    with TestClient(app) as http:
        body = {**plan, "user_goal": prepared["user_goal"], "repo_path": prepared["source_root"],
                "workspace_root": config.local.workspace_root,
                "baseline": json.loads(Path(prepared["source_baseline"]).read_text()),
                "budget": {"research_seconds": 60, "drain_seconds": 10, "final_review_seconds": 20}}
        created = http.post("/audits", json=body)
        assert created.status_code == 201, created.text
        project = created.json()
        pid = project["project"]["id"]
        (folder / "project.json").write_text(json.dumps({"project_id": pid}))
        prepare(project)
        assert http.post(f"/projects/{pid}/audit/start").status_code == 200
        assert http.put(f"/projects/{pid}/status", json={"status": "stopped"}).status_code == 200
        class LegacyService:
            resume = ManagedService.resume
            directory = tmp_path / "service"
            jobs = {}
            database_id = "legacy-fixture-db"
            service_id = "legacy-fixture-service"
            def health(self): return {"ready": True}
        service = LegacyService()
        service.config = config
        app.state.managed_service = service
        identity = {key: getattr(service, key) for key in ("database_id", "service_id")}
        try:
            missing = http.post(f"/projects/{pid}/runtime/resume", json=identity)
            assert missing.status_code == 409 and missing.json()["detail"]["status"] == "needs_budget"
            invalid = http.post(f"/projects/{pid}/runtime/resume", json={**identity, "remaining_seconds": 61})
            assert invalid.status_code == 422
            assert http.get(f"/projects/{pid}/runtime").json()["clock_mode"] == "legacy"
            class Client:
                def __init__(self, *_args, **_kwargs): pass
                def close(self): pass
                def service_health(self): return {"managed": True, "ready": True, **identity}
                def get_runtime(self, project_id): return http.get(f"/projects/{project_id}/runtime").json()
                def control_runtime(self, project_id, action, body):
                    response = http.post(f"/projects/{project_id}/runtime/{action}", json=body)
                    return ApiResult(response.status_code, response.json())
            monkeypatch.setattr(controls, "CairnClient", Client)
            migrated = controls.control_prepared("http://127.0.0.1:8000", "resume", run_dir=folder, remaining_seconds=40)
            assert migrated["project_id"] == pid and migrated["stage_remaining_seconds"] == 40
            assert len(http.get("/projects").json()) == 1
            assert http.post(f"/projects/{pid}/runtime/resume", json=identity).json()["generation"] == migrated["generation"]
            assert http.post(f"/projects/{pid}/runtime/resume", json={**identity, "remaining_seconds": 40}).status_code == 409
            with db.get_conn() as conn:
                row = conn.execute("SELECT * FROM audit_submissions").fetchone()
                assert row["project_id"] == pid and row["run_dir"] == str(folder)
                assert json.loads(row["execution_json"])["controller"]["budget"] == body["budget"]
        finally:
            del app.state.managed_service


def test_paused_project_can_still_be_renamed_and_deleted(active):
    audit, _now = active
    with db.get_conn() as conn:
        runtime.pause(conn, audit[3])
        runtime.freeze(conn, audit[3], "paused")
    assert audit[1].put(f"/projects/{audit[3]}/title", json={"title": "renamed paused task"}).status_code == 200
    assert audit[1].delete(f"/projects/{audit[3]}").status_code == 204


def test_preparing_recovery_restores_workspace_and_existing_preflight_receipts(probe, api, tmp_path, monkeypatch):
    from cairn.controller.research import import_audit_plan
    from cairn.controller.service import ManagedService
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.server.app import app
    config, prepared, _plan = configured(probe)
    binary = tmp_path / "bin" / "claude"
    binary.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
    binary.chmod(0o755)
    client, http = api
    original_start = client.start_audit
    monkeypatch.setattr(client, "start_audit", lambda _pid: (_ for _ in ()).throw(RuntimeError("handoff interrupted")))
    with pytest.raises(ValueError, match="handoff interrupted"):
        import_audit_plan(config, client, LocalBackend(config.local), prepared, external_builder=True)
    pid = client.list_projects()[0].id
    project = client.get_project(pid)
    workspace = Path(config.local.workspace_root) / pid
    assert project.project.audit.started_at is None
    (workspace / "input.json").unlink()
    with db.get_conn() as conn:
        runtime.initialize(conn, pid)
        runtime.freeze(conn, pid, "interrupted")
        runtime.resume(conn, services.audit_metadata(conn, pid))
        conn.execute("DELETE FROM audit_records WHERE project_id=? AND phase='preflight'", (pid,))
        conn.execute("INSERT INTO audit_submissions (id,run_dir,revision,fingerprint,status,prepared_json,execution_json,project_id,worker_token,created_at,updated_at) VALUES ('fixture',?,1,'fixed','preparing',?,?,?,'preparation-token','now','now')",
                     (prepared["run_dir"], json.dumps(prepared), config.model_dump_json(), pid))
    class Service:
        service_id = "preparation-fixture"
        database_id = "preparation-database"
        directory = tmp_path / "service"
        def health(self): return {"ready": False}
        def start(self): pass
        def close(self): pass
    app.state.managed_service = Service()
    http.headers["X-Cairn-Submission-Token"] = "preparation-token"
    try:
        service = ManagedService.__new__(ManagedService)
        service._restore_workspace(config, prepared, project)
        service._restore_preparation(client, prepared, pid)
        count = len(client.get_audit(pid).records)
        service._restore_preparation(client, prepared, pid)
        assert len(client.get_audit(pid).records) == count
        assert any(r.phase == "preflight" and r.decision == "approved" for r in client.get_audit(pid).records)
        assert original_start(pid).ok
        assert client.get_runtime(pid)["generation"] == 2
        assert len(client.list_projects()) == 1
    finally:
        del app.state.managed_service


def test_live_legacy_parent_blocks_startup_instead_of_being_killed(tmp_path, monkeypatch):
    from cairn.controller import local
    (tmp_path / "research.json").write_text(json.dumps({"processes": {"server": {"pid": 12345}}}))
    monkeypatch.setattr(local, "process_identities", lambda _pids: {12345: "existing process"})
    monkeypatch.setattr(local, "stop_agent_groups", lambda *a: pytest.fail("never kill an unverified service parent"))
    with pytest.raises(ValueError, match="manually"):
        local.stop_legacy_agents(tmp_path)


def test_successful_closing_survives_pause_and_submits_original_candidate(active):
    from test_audit_budget import close_result
    audit, now = active
    assert audit[0].record_audit_review(audit[3], node_review(audit)).ok
    settle_to(audit, audit[0].get_audit(audit[3]).budget.research_seconds)
    assert audit[0].audit_budget(audit[3]).data["audit"]["phase"] == "finalizing"
    result, _body = close_result(audit)
    pause_and_resume(audit)
    restored = audit[0].get_audit(audit[3]).closing
    assert restored.status == "success" and restored.result.model_dump(by_alias=True) == result["result"]
    candidate = audit[0].submit_closing(audit[3])
    assert candidate.ok
    claimed = claim(audit, candidate.data["id"])
    assert finish(audit, claimed, review_body(audit, claimed)).json()["status"] == "approved"
    finished_elapsed = audit[0].get_runtime(audit[3])["elapsed_seconds"]
    now[0] += 86400
    assert audit[0].get_runtime(audit[3])["elapsed_seconds"] == finished_elapsed
    assert audit[0].get_runtime(audit[3])["state"] == "finished"


def test_process_cleanup_is_scoped_to_the_paused_project(tmp_path, monkeypatch):
    import os
    from cairn.controller.local import stop_agent_groups
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV
    registry = tmp_path / "registry"
    monkeypatch.setenv(REGISTRY_ENV, str(registry))
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    first_dir = backend.ensure_running("first")
    second_dir = backend.ensure_running("second")
    processes = [backend.build_exec_process(directory, {}, [sys.executable, "-c", "import time; time.sleep(30)"])
                 for directory in (first_dir, second_dir)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        for process in processes:
            process.start()
        futures = [executor.submit(process.communicate, None) for process in processes]
        try:
            wait_until(lambda: len(list(registry.glob("*.json"))) == 2)
            stop_agent_groups(registry, {os.getpid()}, {first_dir})
            assert futures[0].done()
            assert not futures[1].done() and processes[1]._process.poll() is None
        finally:
            for process in processes:
                process.cancel("test cleanup")
            for future in futures:
                future.result(timeout=5)
