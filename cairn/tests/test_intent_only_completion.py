"""Final evidence review does not depend on an Overseer Fact approval queue."""
from copy import deepcopy

import pytest

from cairn.dispatcher.tasks.audit import candidate_fact_ids, reason_result
from cairn.server.models import AuditRecordRequest, ClosingReviewRequest, CompletionClaimRequest

from test_audit_workflow import api
from test_audit_review import audit, claim, completion, finish, node_review, review_body
from test_audit_budget import clock, close_result


def observation(audit, *, premises=(), evidence=True):
    client, _http, _config, pid, _repo = audit
    project = client.get_project(pid)
    data = project.facts[2].audit.model_copy(deep=True)
    data.review_status = "unverified"
    data.premise_ids = list(premises)
    if not evidence:
        data.evidence = []
    intent = client.create_intent(pid, list(premises) or ["origin"], "collect a local observation", "researcher")
    assert intent.ok, intent.text
    result = client.conclude(pid, intent.data["id"], "researcher", "conditional observation", audit=data)
    assert result.ok, result.text
    return result.data["fact"]["id"]


def test_unverified_candidate_and_premise_can_finish_without_fact_promotion(audit):
    client, _http, _config, pid, _repo = audit
    parent = observation(audit)
    child = observation(audit, premises=[parent])
    before = {fact.id: deepcopy(fact.audit) for fact in client.get_project(pid).facts}
    project = client.get_project(pid)
    assert {parent, child} <= set(candidate_fact_ids(project))
    assert reason_result(project, {"accepted": True, "data": {"complete": {
        "from": [child], "description": "submit conditional evidence for independent review",
    }}}, open_intents_empty=False, max_intents=2)[0] == "complete"

    request = claim(audit, completion(audit, [child])["id"])
    result = finish(audit, request, review_body(audit, request))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "approved"
    after = client.get_project(pid)
    assert after.project.status == "completed"
    assert {fact.id: fact.audit for fact in after.facts} == before
    assert [record.phase for record in client.get_audit(pid).records] == ["final_review"]


@pytest.mark.parametrize("missing_on", ["candidate", "premise"])
def test_final_approval_requires_source_evidence_for_every_unverified_premise(audit, missing_on):
    client, _http, _config, pid, _repo = audit
    parent = observation(audit, evidence=missing_on != "premise")
    child = observation(audit, premises=[parent], evidence=missing_on != "candidate")
    request = claim(audit, completion(audit, [child])["id"])
    result = finish(audit, request, review_body(audit, request))
    assert result.status_code == 409, result.text
    assert "evidence" in result.json()["detail"]
    project = client.get_project(pid)
    assert project.project.status == "active"
    assert not any(intent.to == "goal" for intent in project.intents)
    assert all(fact.audit.review_status == "unverified" for fact in project.facts if fact.id in (parent, child))


@pytest.mark.parametrize("source", ["refuted", "descendant"])
def test_refuted_premise_cannot_be_used_for_reason_or_direct_completion(audit, source):
    client, _http, _config, pid, _repo = audit
    parent = observation(audit)
    child = observation(audit, premises=[parent])
    assert client.record_audit_review(pid, node_review(audit, parent, decision="rejected")).ok
    project = client.get_project(pid)
    assert not set((parent, child)) & set(candidate_fact_ids(project))
    with pytest.raises(ValueError, match="non-refuted"):
        reason_result(project, {"accepted": True, "data": {"complete": {
            "from": [child], "description": "must not silently reuse the refuted premise",
        }}}, open_intents_empty=False, max_intents=2)
    result = client.complete(pid, [parent if source == "refuted" else child], "invalid candidate", "researcher")
    assert result.status_code == 409 and "refuted" in result.text
    assert client.list_completions(pid) == []
    project = client.get_project(pid)
    assert project.project.status == "active" and not project.project.audit.completion_pending


def test_final_approval_rechecks_refutation_after_request_was_created(audit):
    client, _http, _config, pid, _repo = audit
    parent = observation(audit)
    child = observation(audit, premises=[parent])
    request_id = completion(audit, [child])["id"]
    assert client.record_audit_review(pid, node_review(audit, parent, decision="rejected")).ok
    request = claim(audit, request_id)
    result = finish(audit, request, review_body(audit, request))
    assert result.status_code == 409 and "refuted" in result.json()["detail"]
    assert client.get_project(pid).project.status == "active"


@pytest.mark.parametrize("source", ["refuted", "descendant"])
def test_closing_cannot_accept_refuted_candidate_or_its_descendant(audit, clock, source):
    client, _http, _config, pid, _repo = audit
    parent = observation(audit)
    child = observation(audit, premises=[parent])
    assert client.record_audit_review(pid, node_review(audit, parent, decision="rejected")).ok
    clock(client.get_audit(pid).research_deadline)
    assert client.audit_budget(pid).data["audit"]["phase"] == "finalizing"
    state = client.claim_closing(pid, CompletionClaimRequest(owner="controller", timeout_seconds=30))
    assert state.ok, state.text
    state = state.data
    result = {"kind": "candidate", "from": [parent if source == "refuted" else child], "description": "invalid closing candidate"}
    body = ClosingReviewRequest(
        token=state["token"], input_digest=state["input_digest"],
        record=AuditRecordRequest(
            record_key="closing:" + state["token"], object_type="project", object_id=pid,
            input_digest=state["input_digest"], phase="closing", session="closing-session",
            attempt_status="success", result=result,
        ), result=result,
    )
    response = client.finish_closing(pid, body)
    assert response.status_code == 409 and "refuted" in response.text
    assert not client.submit_closing(pid).ok
    assert client.list_completions(pid) == []
    metadata = client.get_audit(pid)
    assert not metadata.completion_pending and metadata.closing.result is None


@pytest.mark.parametrize("broken", ["missing", "goal", "cycle", "metadata"])
def test_candidate_selection_rejects_invalid_actual_premise_closures(audit, broken):
    project = audit[0].get_project(audit[3])
    fact = project.facts[2]
    if broken == "metadata":
        fact.audit = None
    else:
        fact.audit.premise_ids = [{"missing": "unknown", "goal": "goal", "cycle": fact.id}[broken]]
    assert fact.id not in candidate_fact_ids(project)
    with pytest.raises(ValueError, match="valid premise closure"):
        reason_result(project, {"accepted": True, "data": {"complete": {
            "from": [fact.id], "description": "invalid proof",
        }}}, open_intents_empty=False, max_intents=2)


def test_closing_can_submit_unverified_last_result_without_whole_graph_review(audit, clock):
    client, _http, _config, pid, _repo = audit
    proof = observation(audit)
    before = client.get_project(pid)
    clock(client.get_audit(pid).research_deadline)
    assert client.audit_budget(pid).data["audit"]["phase"] == "finalizing"
    state = client.claim_closing(pid, CompletionClaimRequest(owner="controller", timeout_seconds=30))
    assert state.ok, state.text
    state = state.data
    result = {"kind": "candidate", "from": [proof], "description": "final observation requires independent judgment"}
    body = ClosingReviewRequest(
        token=state["token"], input_digest=state["input_digest"],
        record=AuditRecordRequest(
            record_key="closing:" + state["token"], object_type="project", object_id=pid,
            input_digest=state["input_digest"], phase="closing", session="closing-session",
            attempt_status="success", result=result,
        ), result=result,
    )
    assert client.finish_closing(pid, body).ok
    submitted = client.submit_closing(pid)
    assert submitted.ok, submitted.text
    request = claim(audit, submitted.data["id"])
    finished = finish(audit, request, review_body(audit, request))
    assert finished.status_code == 200, finished.text
    after = client.get_project(pid)
    assert after.project.status == "completed"
    assert after.facts == before.facts
    assert [record.phase for record in client.get_audit(pid).records] == ["closing", "final_review"]


def test_no_candidate_budget_end_does_not_require_fact_reviews(audit, clock):
    client, _http, _config, pid, _repo = audit
    observation(audit, evidence=False)
    clock(client.get_audit(pid).research_deadline)
    assert client.audit_budget(pid).data["audit"]["phase"] == "finalizing"
    close_result(audit, kind="no_candidate")
    ended = client.audit_budget(pid, finalize=True)
    assert ended.data["status"] == "budget_exhausted"
    assert [record.phase for record in client.get_audit(pid).records] == ["closing"]


def test_intent_correction_adds_hint_without_changing_facts_or_blocking_research(audit):
    client, _http, _config, pid, _repo = audit
    project = client.get_project(pid)
    intent = next(item for item in project.intents if item.to is None)
    inputs = client.get_review_input(pid, "intent", intent.id)
    body = AuditRecordRequest(
        record_key="intent-correction", object_type="intent", object_id=intent.id,
        input_digest=inputs["input_digest"], phase="overseer", session="independent-supervisor",
        attempt_status="success", decision="needs_evidence", hint="keep the initial actor constraint",
    )
    assert client.record_audit_review(pid, body).ok
    after = client.get_project(pid)
    assert after.facts == project.facts
    assert after.project.status == "active" and not after.project.audit.completion_pending
    assert after.hints[-1].content == body.hint
    assert after.project.audit.reason_revision == project.project.audit.reason_revision + 1
    assert client.create_intent(pid, ["f001"], "continue research with the correction", "researcher").ok


@pytest.mark.parametrize("changed", ["completed_intent", "completion_pending", "draining", "finalizing"])
def test_late_intent_hint_cannot_change_completion_or_closed_direction(audit, clock, changed):
    client, _http, _config, pid, _repo = audit
    intent = next(item for item in client.get_project(pid).intents if item.to is None)
    if changed == "completed_intent":
        assert client.conclude(pid, intent.id, "researcher", "already concluded").ok
    elif changed == "completion_pending":
        completion(audit)
    else:
        if changed == "draining":
            metadata = client.get_audit(pid)
            clock(metadata.budget.research_seconds - 1)
            assert client.heartbeat(pid, intent.id, "researcher").ok
        clock(client.get_audit(pid).research_deadline)
        assert client.audit_budget(pid).data["audit"]["phase"] == changed
    inputs = client.get_review_input(pid, "intent", intent.id)
    body = AuditRecordRequest(
        record_key="late-intent-hint", object_type="intent", object_id=intent.id,
        input_digest=inputs["input_digest"], phase="overseer", session="independent-supervisor",
        attempt_status="success", decision="needs_evidence", hint="revisit this direction",
    )
    before = client.get_audit(pid)
    response = client.record_audit_review(pid, body)
    assert response.status_code == 409 and "Hint" in response.text
    after = client.get_audit(pid)
    assert after.audit_revision == before.audit_revision
    assert not client.get_project(pid).hints and not after.records
    failed = body.model_copy(update={"attempt_status": "error", "decision": None, "hint": None,
                                    "error": "direction moved on before supervision returned"})
    assert client.record_audit_review(pid, failed).ok
    assert len(client.get_audit(pid).records) == 1
    assert not client.get_project(pid).hints
