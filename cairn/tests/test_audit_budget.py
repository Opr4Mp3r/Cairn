from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from cairn.controller import monitor
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.config import LocalConfig
from cairn.server import services, audit_review
from cairn.server.routers import audits, intents, projects
from cairn.server.models import CompletionClaimRequest, ClosingReviewRequest, AuditRecordRequest

from test_audit_workflow import api
from test_audit_review import audit, node_review, completion, claim, review_body, finish


@pytest.fixture
def clock(audit, monkeypatch):
    client, _http, _config, pid, _repo = audit
    start = datetime.fromisoformat(client.get_audit(pid).started_at.replace("Z", "+00:00"))
    state = [start]
    def now():
        return state[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    for module in (services, audit_review, audits, intents, projects):
        monkeypatch.setattr(module, "utcnow", now)
    monkeypatch.setattr(monitor, "_remaining", lambda deadline: int((datetime.fromisoformat(deadline.replace("Z", "+00:00")) - state[0]).total_seconds()) - 5)
    def set_time(value):
        state[0] = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else start + timedelta(seconds=value)
    return set_time


def open_intent(audit):
    client, _http, _config, pid, _repo = audit
    return next(intent for intent in client.get_project(pid).intents if intent.to is None)


def held_near_deadline(audit, clock):
    client, _http, _config, pid, _repo = audit
    metadata = client.get_audit(pid)
    clock(metadata.budget.research_seconds - 1)
    intent = open_intent(audit)
    response = client.heartbeat(pid, intent.id, "claude-main")
    assert response.ok, response.text
    return intent.id, response.data["lease_id"]


def finalize_phase(audit, clock):
    client, _http, _config, pid, _repo = audit
    assert client.record_audit_review(pid, node_review(audit)).ok
    clock(client.get_audit(pid).research_deadline)
    response = client.audit_budget(pid)
    assert response.ok and response.data["status"] == "draining"
    assert response.data["audit"]["phase"] == "finalizing"
    return client.get_audit(pid)


def close_result(audit, *, kind="candidate", error=None):
    client, _http, _config, pid, _repo = audit
    response = client.claim_closing(pid, CompletionClaimRequest(owner="controller", timeout_seconds=30))
    assert response.ok, response.text
    state = response.data
    result = {"kind": kind, "from": ["f001"] if kind == "candidate" else [], "description": "only existing evidence"}
    body = ClosingReviewRequest(
        token=state["token"], input_digest=state["input_digest"],
        record=AuditRecordRequest(
            record_key="closing:" + state["token"], object_type="project", object_id=pid,
            input_digest=state["input_digest"], phase="closing", session="closing-session",
            attempt_status="error" if error else "success", error=error, result=None if error else result,
        ), result=None if error else result,
    )
    saved = client.finish_closing(pid, body)
    assert saved.ok, saved.text
    return saved.data, body


def test_deadline_blocks_new_research_without_waiting_for_dispatcher(audit, clock):
    client, _http, _config, pid, _repo = audit
    clock(client.get_audit(pid).research_deadline)
    assert client.create_intent(pid, ["f001"], "late work", "claude-main").status_code == 403
    assert client.claim_reason(pid, "claude-main", "late reason").status_code == 403
    assert client.heartbeat(pid, open_intent(audit).id, "claude-main").status_code == 403


def test_draining_preserves_only_admitted_lease_and_last_fact(audit, clock):
    client, _http, _config, pid, _repo = audit
    intent_id, token = held_near_deadline(audit, clock)
    metadata = client.get_audit(pid)
    clock(metadata.research_deadline)
    tick = client.audit_budget(pid)
    assert tick.data["audit"]["phase"] == "draining"
    assert token in tick.data["audit"]["draining_lease_ids"]
    assert client.heartbeat(pid, intent_id, "claude-main").status_code == 409
    assert client.heartbeat(pid, intent_id, "claude-main", lease_id="wrong").status_code == 409
    assert client.heartbeat(pid, intent_id, "claude-main", lease_id=token).ok
    data = client.get_project(pid).facts[2].audit.model_copy(update={"review_status": "unverified"})
    response = client.conclude(pid, intent_id, "claude-main", "last observed result", audit=data, lease_id=token)
    assert response.ok, response.text
    assert response.data["fact"]["audit"]["review_status"] == "unverified"
    tick = client.audit_budget(pid, finalize=True)
    assert tick.data["status"] == "draining" and tick.data["audit"]["phase"] == "finalizing"
    assert tick.data["audit"]["closing"]["status"] == "pending"
    assert tick.data["audit"]["research_deadline"] == metadata.research_deadline


def test_expired_admitted_lease_cannot_be_reclaimed(audit, clock):
    client, _http, _config, pid, _repo = audit
    intent_id, token = held_near_deadline(audit, clock)
    metadata = client.get_audit(pid)
    clock(metadata.research_deadline)
    client.audit_budget(pid)
    clock(metadata.budget.research_seconds + 20)
    assert client.heartbeat(pid, intent_id, "claude-main", lease_id=token).status_code == 409
    assert client.heartbeat(pid, intent_id, "claude-main").status_code == 403


def test_drain_deadline_rejects_late_fact_and_cannot_restart_budget(audit, clock):
    client, _http, _config, pid, _repo = audit
    intent_id, token = held_near_deadline(audit, clock)
    original = client.get_audit(pid)
    clock(original.drain_deadline)
    response = client.conclude(pid, intent_id, "claude-main", "too late", lease_id=token)
    assert response.status_code in (403, 409)
    client.audit_budget(pid)
    assert client.set_project_status(pid, "active").status_code == 409
    assert client.start_audit(pid).status_code == 409
    assert len(client.get_project(pid).facts) == 3
    assert client.get_audit(pid).final_deadline == original.final_deadline


def test_budget_cannot_end_before_final_reviews_and_closing(audit, clock):
    client, _http, _config, pid, _repo = audit
    clock(client.get_audit(pid).research_deadline)
    client.audit_budget(pid)
    assert client.audit_budget(pid, finalize=True).data["status"] == "draining"
    assert client.claim_closing(pid, CompletionClaimRequest(owner="controller")).ok
    assert not client.get_audit(pid).records
    assert client.audit_budget(pid, finalize=True).data["status"] == "draining"


def test_no_candidate_exits_with_budget_result_not_impossibility(audit, clock):
    client, _http, _config, pid, _repo = audit
    original = finalize_phase(audit, clock)
    saved, body = close_result(audit, kind="no_candidate")
    assert saved["status"] == "success"
    assert client.finish_closing(pid, body).ok
    ended = client.audit_budget(pid, finalize=True)
    assert ended.data["status"] == "budget_exhausted"
    assert not any(intent.to == "goal" for intent in client.get_project(pid).intents)
    assert client.get_audit(pid).final_deadline == original.final_deadline


def test_closing_candidate_submission_recovers_idempotently_and_can_complete(audit, clock, monkeypatch):
    client, _http, config, pid, _repo = audit
    finalize_phase(audit, clock)
    saved, _body = close_result(audit)
    assert saved["request_id"] is None
    assert client.audit_budget(pid, finalize=True).data["status"] == "draining"
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    def no_agent(*args, **kwargs):
        raise AssertionError("a successful persisted closing must not be called again")
    monkeypatch.setattr(monitor, "_invoke", no_agent)
    monitor._closing_once(config, client, backend, client.get_project(pid), TaskCancellation())
    request = client.list_completions(pid)[0]
    assert request.source_session == "closing-session"
    assert client.submit_closing(pid).data["id"] == request.id
    assert len(client.list_completions(pid)) == 1
    claimed = claim(audit, request.id)
    verdict = finish(audit, claimed, review_body(audit, claimed))
    assert verdict.status_code == 200, verdict.text
    assert client.audit_budget(pid, finalize=True).data["status"] == "completed"
    assert len([intent for intent in client.get_project(pid).intents if intent.to == "goal"]) == 1


def test_final_deadline_ends_without_dispatcher_or_successful_closing(audit, clock):
    client, _http, _config, pid, _repo = audit
    original = client.get_audit(pid)
    clock(original.final_deadline)
    result = client.audit_budget(pid, finalize=True)
    assert result.data["status"] == "budget_exhausted"
    assert result.data["audit"]["closing"]["status"] == "pending"
    assert result.data["audit"]["termination_reason"] == "final_deadline"
    assert client.audit_budget(pid).data == result.data
    assert client.set_project_status(pid, "active").status_code == 409


def test_closing_errors_have_bounded_attempts_and_do_not_become_no_candidate(audit, clock):
    client, _http, _config, pid, _repo = audit
    original = finalize_phase(audit, clock)
    first, _ = close_result(audit, error="CLI failure")
    second, _ = close_result(audit, error="CLI failure")
    assert second["attempts"] == 2 and second["round_deadline"] == first["round_deadline"]
    assert second["result"] is None and second["status"] == "error"
    assert client.claim_closing(pid, CompletionClaimRequest(owner="restored")).status_code == 409
    assert client.audit_budget(pid, finalize=True).data["status"] == "draining"
    clock(original.final_deadline)
    assert client.audit_budget(pid).data["status"] == "budget_exhausted"


def test_late_final_approval_does_not_overwrite_budget_result(audit, clock):
    client, _http, _config, pid, _repo = audit
    request = claim(audit, completion(audit)["id"])
    clock(client.get_audit(pid).final_deadline)
    ended = client.audit_budget(pid)
    assert ended.data["status"] == "budget_exhausted"
    assert finish(audit, request, review_body(audit, request)).status_code == 409
    assert not any(intent.to == "goal" for intent in client.get_project(pid).intents)


def test_stale_closing_receipt_cannot_be_persisted_as_success(audit, clock):
    client, http, _config, pid, _repo = audit
    finalize_phase(audit, clock)
    claimed = client.claim_closing(pid, CompletionClaimRequest(owner="controller")).data
    http.post(f"/projects/{pid}/hints", json={"content": "new relevant information", "creator": "human"})
    body = ClosingReviewRequest(
        token=claimed["token"], input_digest=claimed["input_digest"],
        record=AuditRecordRequest(record_key="closing:stale", object_type="project", object_id=pid,
                                 input_digest=claimed["input_digest"], phase="closing", session="independent",
                                 attempt_status="success"),
        result={"kind": "no_candidate", "description": "no proof"},
    )
    assert client.finish_closing(pid, body).status_code == 409
    assert client.get_audit(pid).closing.status == "running"


def test_monitor_final_graph_generates_candidate_then_independent_approval(audit, clock, monkeypatch, tmp_path):
    client, _http, config, pid, _repo = audit
    finalize_phase(audit, clock)
    from test_audit_review import assessment
    calls = []
    def invoke(*args, **kwargs):
        calls.append(kwargs["phase"])
        data = {"kind": "candidate", "from": ["f001"], "description": "existing verified result"} if kwargs["phase"] == "closing" else assessment()
        artifact = tmp_path / (kwargs["phase"] + ".json")
        artifact.write_text("{}")
        return SimpleNamespace(status="success", data=data, session="session-" + kwargs["phase"], error=None,
                               worker="claude-main", harness="claudecode"), artifact
    monkeypatch.setattr(monitor, "_invoke", invoke)
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    result = monitor.monitor_once(config, client, backend, pid)
    assert result["status"] == "completed"
    assert calls == ["closing", "final_review"]
    assert client.get_audit(pid).closing.request_id is not None


def test_controller_restore_after_budget_end_does_not_call_agent(audit, clock, monkeypatch):
    client, _http, config, pid, _repo = audit
    clock(client.get_audit(pid).final_deadline)
    monkeypatch.setattr(monitor, "_invoke", lambda *args, **kwargs: pytest.fail("ended audit invoked Agent"))
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    assert monitor.monitor_audit(config, client, backend, pid)["status"] == "budget_exhausted"


def test_closing_and_final_review_use_shared_cli_with_distinct_sessions(audit, clock, monkeypatch, tmp_path):
    import json
    from test_audit_workflow import install_cli
    from test_audit_review import assessment

    client, _http, config, pid, _repo = audit
    intent_id, token = held_near_deadline(audit, clock)
    clock(client.get_audit(pid).research_deadline)
    client.audit_budget(pid)
    last_fact = client.get_project(pid).facts[2].audit.model_copy(update={"review_status": "unverified"})
    written = client.conclude(pid, intent_id, "claude-main", "last source result", audit=last_fact, lease_id=token)
    assert written.ok, written.text
    proof_id = written.data["fact"]["id"]
    finalize_phase(audit, clock)
    config.controller.timeouts.closing = 15
    calls_file = install_cli(tmp_path, monkeypatch, [
        {"kind": "candidate", "from": [proof_id], "description": "existing evidence to assess independently"},
        assessment([proof_id]),
    ])
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    result = monitor.monitor_once(config, client, backend, pid)
    assert result["status"] == "completed"
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    sessions = [call["argv"][call["argv"].index("--session-id") + 1] for call in calls]
    assert len(sessions) == len(set(sessions)) == 2
    records = client.get_audit(pid).records
    assert {record.phase for record in records} >= {"closing", "final_review"}
    assert not any(record.phase == "overseer" and record.object_id == proof_id for record in records)
    assert all(call["cwd"] == client.get_audit(pid).workspace_path for call in calls)
    assert client.list_completions(pid)[0].from_ == [proof_id]


def test_closing_submit_lost_response_restores_without_new_agent(audit, clock, monkeypatch):
    client, _http, config, pid, _repo = audit
    finalize_phase(audit, clock)
    close_result(audit)
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    original = client.submit_closing
    def lost_response(project_id):
        original(project_id)
        raise OSError("connection lost after commit")
    monkeypatch.setattr(client, "submit_closing", lost_response)
    with pytest.raises(OSError):
        monitor._closing_once(config, client, backend, client.get_project(pid), TaskCancellation())
    monkeypatch.setattr(client, "submit_closing", original)
    monkeypatch.setattr(monitor, "_invoke", lambda *args, **kwargs: pytest.fail("successful closing repeated"))
    monitor._closing_once(config, client, backend, client.get_project(pid), TaskCancellation())
    assert len(client.list_completions(pid)) == 1


def test_reason_lease_can_renew_and_submit_but_not_create_intents_while_draining(audit, clock):
    client, _http, _config, pid, _repo = audit
    meta = client.get_audit(pid)
    clock(meta.budget.research_seconds - 1)
    reason = client.claim_reason(pid, "claude-main", "new fact")
    token = reason.data["reason"]["lease_id"]
    clock(meta.research_deadline)
    client.audit_budget(pid)
    assert client.reason_heartbeat(pid, "claude-main", lease_id=token).ok
    assert client.create_intent(pid, ["f001"], "new work", "claude-main").status_code == 403
    assert client.complete(pid, ["f001"], "existing proof", "claude-main", lease_id=token).status_code == 202
    assert client.release_reason(pid, "claude-main", lease_id=token).ok


def test_dispatcher_preserves_draining_task_until_finalizing(audit, clock):
    from cairn.dispatcher.scheduler.loop import DispatcherLoop
    from cairn.dispatcher.models import RunningTask
    from concurrent.futures import Future

    client, _http, _config, pid, _repo = audit
    intent_id, _token = held_near_deadline(audit, clock)
    clock(client.get_audit(pid).research_deadline)
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.client = client
    cancellation = TaskCancellation()
    loop.futures = {Future(): RunningTask(pid, "explore", "claude-main", cancellation, intent_id=intent_id)}
    summaries = loop._advance_audit_budgets(client.list_projects())
    loop._cancel_inactive_tasks(summaries)
    assert not cancellation.is_cancelled
    assert summaries[0].status == "draining"
    clock(client.get_audit(pid).drain_deadline)
    loop._cancel_inactive_tasks(loop._advance_audit_budgets(client.list_projects()))
    assert cancellation.is_cancelled


def test_approval_and_budget_settlement_are_serialized(audit, clock):
    from concurrent.futures import ThreadPoolExecutor

    client, _http, _config, pid, _repo = audit
    finalize_phase(audit, clock)
    close_result(audit)
    request = claim(audit, client.submit_closing(pid).data["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(finish, audit, request, review_body(audit, request))
        settlement = pool.submit(client.audit_budget, pid, finalize=True)
        assert approval.result().status_code == 200
        assert settlement.result().ok
    assert client.get_project(pid).project.status == "completed"
    assert len([intent for intent in client.get_project(pid).intents if intent.to == "goal"]) == 1


def test_expired_closing_claim_can_resume_within_original_round(audit, clock):
    client, _http, _config, pid, _repo = audit
    finalize_phase(audit, clock)
    first = client.claim_closing(pid, CompletionClaimRequest(owner="first", timeout_seconds=20)).data
    clock(first["attempt_deadline"])
    client.audit_budget(pid)
    assert client.get_audit(pid).closing.status == "error"
    second = client.claim_closing(pid, CompletionClaimRequest(owner="restored", timeout_seconds=20))
    assert second.ok, second.text
    assert second.data["attempts"] == 2
    assert second.data["round_deadline"] == first["round_deadline"]
    assert second.data["token"] != first["token"]


def test_closing_receipt_and_candidate_must_agree(audit, clock):
    client, _http, _config, pid, _repo = audit
    finalize_phase(audit, clock)
    state = client.claim_closing(pid, CompletionClaimRequest(owner="controller")).data
    body = ClosingReviewRequest(
        token=state["token"], input_digest=state["input_digest"],
        record=AuditRecordRequest(record_key="mismatched", object_type="project", object_id=pid,
                                 input_digest=state["input_digest"], phase="closing", session="closing",
                                 attempt_status="success", result={"kind": "no_candidate", "description": "no proof"}),
        result={"kind": "candidate", "from": ["f001"], "description": "proof"},
    )
    assert client.finish_closing(pid, body).status_code == 400
    assert client.get_audit(pid).closing.result is None


def test_inflight_bootstrap_result_can_submit_with_its_intent_lease(audit, clock):
    from cairn.dispatcher.tasks.bootstrap import _write_bootstrap_complete_result

    client, _http, _config, pid, _repo = audit
    intent_id, token = held_near_deadline(audit, clock)
    clock(client.get_audit(pid).research_deadline)
    client.audit_budget(pid)
    data = client.get_project(pid).facts[2].audit.model_copy(update={"review_status": "unverified"})
    outcome = _write_bootstrap_complete_result(
        client, pid, intent_id, "claude-main", "last observed source", "candidate only",
        source="bootstrap", phase_ms=1, audit=data, source_session="bootstrap-session", lease_id=token,
    )
    assert outcome == "success"
    requests = client.list_completions(pid)
    assert len(requests) == 1
    assert requests[0].from_ == ["f002"] and requests[0].source_session == "bootstrap-session"
    assert client.get_project(pid).project.status == "draining"


def outdated_no_candidate(audit, clock):
    from cairn.server.models import EvidenceReference

    client, _http, _config, pid, _repo = audit
    assert client.record_audit_review(pid, node_review(audit, decision="needs_evidence")).ok
    clock(client.get_audit(pid).research_deadline)
    client.audit_budget(pid)
    old, _ = close_result(audit, kind="no_candidate")
    fact = client.get_project(pid).facts[2]
    data = fact.audit.model_copy(deep=True)
    files = client.get_audit(pid).baseline["files"]
    path = "proof.py" if "proof.py" in files else "app.py"
    data.evidence.append(EvidenceReference(path=path, start_line=2, end_line=2,
                                           sha256=files[path]))
    assert client.record_audit_review(pid, node_review(audit, data=data)).ok
    return old


@pytest.fixture
def with_proof_file(monkeypatch):
    import test_audit_review

    original = test_audit_review.repository
    def repository_with_proof(tmp_path):
        repo = original(tmp_path)
        (repo / "proof.py").write_text("def proof():\n    return 'fixed proof'\n")
        return repo
    monkeypatch.setattr(test_audit_review, "repository", repository_with_proof)


def test_old_no_candidate_does_not_end_new_reviewed_evidence(with_proof_file, audit, clock):
    client, _http, _config, pid, _repo = audit
    old = outdated_no_candidate(audit, clock)
    original_deadline = client.get_audit(pid).final_deadline
    result = client.audit_budget(pid, finalize=True)
    assert result.data["status"] == "draining"
    current = client.get_audit(pid).closing
    assert current.status == "stale" and current.result.kind == "no_candidate"
    assert current.input_digest == old["input_digest"]
    assert current.round_deadline == old["round_deadline"]
    assert current.attempts == old["attempts"]
    clock(original_deadline)
    ended = client.audit_budget(pid, finalize=True)
    assert ended.data["status"] == "budget_exhausted"
    assert ended.data["audit"]["closing"]["status"] == "stale"
    assert ended.data["audit"]["closing"]["error"]


def test_new_snapshot_closing_uses_remaining_attempt_not_new_round(audit, clock):
    client, _http, _config, pid, _repo = audit
    old = outdated_no_candidate(audit, clock)
    request = client.claim_closing(pid, CompletionClaimRequest(owner="restored", timeout_seconds=30))
    assert request.ok, request.text
    assert request.data["status"] == "running"
    assert request.data["attempts"] == old["attempts"] + 1
    assert request.data["round_deadline"] == old["round_deadline"]
    assert request.data["input_digest"] != old["input_digest"]
    assert request.data["result"] is None


def test_monitor_restore_assesses_new_snapshot_instead_of_old_no_candidate(audit, clock, monkeypatch, tmp_path):
    from test_audit_review import assessment

    client, _http, config, pid, _repo = audit
    old = outdated_no_candidate(audit, clock)
    calls = []
    def invoke(*args, **kwargs):
        phase = kwargs["phase"]
        calls.append(phase)
        data = {"kind": "candidate", "from": ["f001"], "description": "new supported evidence"} if phase == "closing" else assessment()
        artifact = tmp_path / (phase + ".json")
        artifact.write_text("{}")
        return SimpleNamespace(status="success", data=data, session="repaired-" + phase, error=None,
                               worker="claude-main", harness="claudecode"), artifact
    monkeypatch.setattr(monitor, "_invoke", invoke)
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    result = monitor.monitor_once(config, client, backend, pid)
    assert result["status"] == "completed"
    assert calls == ["closing", "final_review"]
    closing = client.get_audit(pid).closing
    assert closing.attempts == 2 and closing.round_deadline == old["round_deadline"]


def test_changed_premise_cannot_reuse_old_no_candidate(audit, clock):
    client, _http, _config, pid, _repo = audit
    intent = open_intent(audit)
    token = client.heartbeat(pid, intent.id, "claude-main").data["lease_id"]
    data = client.get_project(pid).facts[2].audit.model_copy(update={"review_status": "unverified", "premise_ids": ["f001"]})
    written = client.conclude(pid, intent.id, "claude-main", "dependent claim", audit=data, lease_id=token)
    child = written.data["fact"]["id"]
    assert client.record_audit_review(pid, node_review(audit, object_id=child)).ok
    finalize_phase(audit, clock)
    close_result(audit, kind="no_candidate")
    assert client.record_audit_review(pid, node_review(audit, decision="needs_evidence")).ok
    assert client.record_audit_review(pid, node_review(audit, object_id=child, decision="needs_evidence")).ok
    result = client.audit_budget(pid, finalize=True)
    assert result.data["status"] == "draining"
    assert result.data["audit"]["closing"]["status"] == "stale"


def test_stale_closing_exhausted_attempts_preserve_gap_until_deadline(audit, clock, monkeypatch):
    client, _http, config, pid, _repo = audit
    original = finalize_phase(audit, clock)
    close_result(audit, error="CLI failure")
    old, _ = close_result(audit, kind="no_candidate")
    assert old["attempts"] == 2
    data = client.get_project(pid).facts[2].audit.model_copy(update={"conditions": ["local fixture", "newly confirmed condition"]})
    record = node_review(audit, data=data).model_copy(update={"record_key": "r1:newly-confirmed-condition"})
    assert client.record_audit_review(pid, record).ok
    monkeypatch.setattr(monitor, "_invoke", lambda *args, **kwargs: pytest.fail("attempt limit was reset"))
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    result = monitor.monitor_once(config, client, backend, pid)
    assert result["status"] == "draining"
    closing = client.get_audit(pid).closing
    assert closing.status == "stale" and closing.attempts == 2
    assert closing.round_deadline == old["round_deadline"]
    clock(original.final_deadline)
    result = monitor.monitor_once(config, client, backend, pid)
    assert result["status"] == "budget_exhausted"
    assert client.get_audit(pid).closing.status == "stale"


def test_stale_candidate_submission_cannot_reuse_existing_request(audit, clock):
    client, _http, _config, pid, _repo = audit
    finalize_phase(audit, clock)
    close_result(audit)
    old_request = client.submit_closing(pid).data["id"]
    data = client.get_project(pid).facts[2].audit.model_copy(update={"conditions": ["local fixture", "new condition"]})
    record = node_review(audit, data=data).model_copy(update={"record_key": "r1:new-candidate-condition"})
    assert client.record_audit_review(pid, record).ok
    assert client.submit_closing(pid).status_code == 409
    assert client.get_audit(pid).closing.request_id == old_request
    assert client.audit_budget(pid, finalize=True).data["status"] == "draining"
