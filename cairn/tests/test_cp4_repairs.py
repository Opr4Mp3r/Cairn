from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from cairn.controller import monitor
from cairn.controller.models import AgentCallResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.protocol.client import ApiResult
from cairn.server.models import AuditRecordRequest

from test_audit_review import api, audit, assessment, claim, completion, finish, node_review, review_body


def mark_nodes_reviewed(audit):
    client, _http, _config, pid, _repo = audit
    assert client.record_audit_review(pid, node_review(audit)).ok
    for intent in client.get_project(pid).intents:
        if intent.to is None:
            digest = client.get_review_input(pid, "intent", intent.id)["input_digest"]
            assert client.record_audit_review(pid, AuditRecordRequest(
                record_key="setup:" + intent.id, object_type="intent", object_id=intent.id,
                input_digest=digest, phase="overseer", session="setup-independent",
                attempt_status="success", decision="approved",
            )).ok


@pytest.mark.parametrize("change", ["hint", "inflight_fact"])
def test_r1_stale_attempt_exhaustion_releases_research_and_allows_explicit_retry(audit, change, monkeypatch):
    client, http, config, pid, _repo = audit
    held = []
    if change == "inflight_fact":
        for index in range(2):
            intent = client.create_intent(pid, ["f001"], f"inflight {index}", f"worker-{index}").data
            assert client.heartbeat(pid, intent["id"], f"worker-{index}").ok
            held.append(intent)
    request = completion(audit)
    final_deadline = client.get_audit(pid).final_deadline
    for index in range(2):
        request = claim(audit, request["id"])
        assert client.get_project(pid).project.audit.completion_pending
        if change == "hint":
            assert http.post(f"/projects/{pid}/hints", json={"content": f"new hint {index}", "creator": "human"}).status_code == 201
        else:
            assert client.conclude(pid, held[index]["id"], f"worker-{index}", "existing exploration result").ok
        response = finish(audit, request, review_body(audit, request))
        assert response.status_code == 200 and response.json()["status"] == "stale"
        request = response.json()
    assert request["attempts"] == 2 and request["decision"] is None
    assert not client.get_project(pid).project.audit.completion_pending
    if change == "hint":
        mark_nodes_reviewed(audit)
        monkeypatch.setattr(monitor, "_invoke", lambda *_args, **_kwargs: pytest.fail("exhausted stale must not make automatic Agent calls"))
        backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
        assert monitor.monitor_once(config, client, backend, pid)["status"] == "active"
    assert http.post(f"/projects/{pid}/audit/completions/{request['id']}/claim", json={"owner": "automatic"}).status_code == 409
    assert client.create_intent(pid, ["f001"], "continue research", "claude-main").ok
    retried = claim(audit, request["id"], retry=True)
    assert retried["attempts"] == 1
    assert client.get_audit(pid).final_deadline == final_deadline
    assert client.get_project(pid).project.status == "active"


def test_r1_round_deadline_expiration_does_not_keep_stale_pause(audit, monkeypatch):
    from cairn.server import audit_review, services

    client, http, _config, pid, _repo = audit
    request = claim(audit, completion(audit)["id"])
    http.post(f"/projects/{pid}/hints", json={"content": "changed review input", "creator": "human"})
    assert finish(audit, request, review_body(audit, request)).json()["status"] == "stale"
    now = datetime.fromisoformat(request["round_deadline"].replace("Z", "+00:00")) + timedelta(seconds=1)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(audit_review, "utcnow", lambda: stamp)
    monkeypatch.setattr(services, "utcnow", lambda: stamp)
    assert not client.get_project(pid).project.audit.completion_pending
    assert client.claim_reason(pid, "claude-main", "resume after stale round").ok
    assert http.post(f"/projects/{pid}/audit/completions/{request['id']}/claim", json={"owner": "automatic"}).status_code == 409
    assert claim(audit, request["id"], retry=True)["attempts"] == 1


def agent_result(config, *, phase, prompt, workspace, status="success", data=None):
    response = json.dumps(data) if data is not None else "partial before cancellation"
    cancelled = status == "cancelled"
    return AgentCallResult(
        role="reviewer", worker=config.workers[0].name, phase=phase, workspace=str(workspace),
        session=uuid.uuid4().hex, prompt=prompt, status=status, data=data, response=response,
        error=None if status == "success" else "controlled interruption",
        process=ProcessResult(0 if status == "success" else 130, response, "", cancelled=cancelled,
                              cancel_reason="controlled interruption" if cancelled else None), elapsed_seconds=0.01,
    )


@pytest.mark.parametrize("phase", ["overseer", "final_review"])
@pytest.mark.parametrize("event", ["cancelled_result", "keyboard_interrupt"])
def test_r2_cancel_stops_all_later_agent_calls_and_keeps_current_receipt(audit, monkeypatch, phase, event):
    client, _http, config, pid, _repo = audit
    if phase == "final_review":
        mark_nodes_reviewed(audit)
    completion(audit)
    cancellation, calls = TaskCancellation(), []
    if event == "cancelled_result":
        def call(config, backend, **kwargs):
            calls.append(kwargs["phase"])
            return agent_result(config, phase=kwargs["phase"], prompt=kwargs["prompt"], workspace=kwargs["workspace"], status="cancelled")
        monkeypatch.setattr(monitor, "call_agent", call)
    else:
        class InterruptedProcess:
            reason = None
            communications = 0

            def start(self):
                calls.append(phase)

            def cancel(self, reason):
                self.reason = reason

            def communicate(self, timeout):
                self.communications += 1
                if self.communications == 1:
                    raise KeyboardInterrupt("controlled KeyboardInterrupt")
                return ProcessResult(130, "partial before cancellation", "", cancelled=True, cancel_reason=self.reason)

        monkeypatch.setattr(LocalBackend, "build_exec_process", lambda *_args, **_kwargs: InterruptedProcess())
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    result = monitor.monitor_audit(config, client, backend, pid, once=True, cancellation=cancellation)
    assert result["status"] == "cancelled" and cancellation.is_cancelled
    assert calls == [phase]
    records = [r for r in client.get_audit(pid).records if r.phase == phase and r.attempt_status == "error"]
    assert len(records) == 1
    raw = json.loads(Path(records[0].artifacts[0]).read_text())
    assert raw["status"] == "cancelled" and "partial" in raw["process"]["stdout"]
    assert client.get_project(pid).project.status == "active"


def test_r2_actual_sigterm_cli_exits_without_second_agent_process(tmp_path):
    root = Path(__file__).resolve().parents[2]
    original = root / "cairn/tests/fixtures/controller-sigterm-cli.py"
    script = tmp_path / "own-process-sigterm.py"
    script.write_text(original.read_text())
    env = {**os.environ, "PYTHONPATH": str(root / "cairn/src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(script)], cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(Path(result.stdout.splitlines()[0]).read_text())
    assert observed["os_sigterm_tested"]
    assert observed["agent_process_plans_after_signal"] == 1
    assert observed["exit_code"] != 0
    assert len(observed["records"]) == 1 and observed["records"][0]["attempt_status"] == "error"


@pytest.mark.parametrize("temporary_conflict", [False, True])
def test_r3_retry_waits_for_lease_then_is_consumed_once(audit, monkeypatch, temporary_conflict):
    client, _http, config, pid, _repo = audit
    mark_nodes_reviewed(audit)
    request = completion(audit)
    for _ in range(2):
        request = claim(audit, request["id"])
        assert finish(audit, request, review_body(audit, request, error="previous CLI error")).json()["status"] == "review_error"
    held = next(i for i in client.get_project(pid).intents if i.to is None)
    assert client.heartbeat(pid, held.id, "claude-main").ok
    deadline = client.get_audit(pid).final_deadline
    claims, calls, reports, sleeps = [], [], [], []
    cancellation = TaskCancellation()
    original_claim = client.claim_completion

    def tracked_claim(project_id, request_id, body):
        claims.append(body.retry)
        if temporary_conflict and len(claims) == 1:
            return ApiResult(409, text="temporary claim conflict")
        return original_claim(project_id, request_id, body)

    def call(config, backend, **kwargs):
        calls.append(kwargs["phase"])
        return agent_result(config, phase=kwargs["phase"], prompt=kwargs["prompt"], workspace=kwargs["workspace"],
                            status="failed" if len(calls) == 1 else "success", data=None if len(calls) == 1 else assessment())

    def sleep(_seconds):
        sleeps.append(True)
        if len(sleeps) == 1:
            assert client.release(pid, held.id, "claude-main").ok
        elif len(sleeps) >= 4:
            cancellation.cancel("bounded regression safety stop")

    monkeypatch.setattr(client, "claim_completion", tracked_claim)
    monkeypatch.setattr(monitor, "call_agent", call)
    monkeypatch.setattr(monitor, "time", SimpleNamespace(sleep=sleep))
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    result = monitor.monitor_audit(config, client, backend, pid, retry_review=request["id"], cancellation=cancellation, report=reports.append)
    assert reports[0].get("waiting") == "existing research leases"
    assert result["status"] == "completed"
    assert claims == ([True, True, False] if temporary_conflict else [True, False])
    assert calls == ["final_review", "final_review"]
    latest = client.list_completions(pid)[0]
    assert latest.status == "approved" and latest.attempts == 2
    assert client.get_audit(pid).final_deadline == deadline
