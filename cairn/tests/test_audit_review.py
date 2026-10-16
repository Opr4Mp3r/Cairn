from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from cairn.audit_evidence import repository_baseline
from cairn.cli import main
from cairn.controller.monitor import _strict_result, monitor_once, monitor_audit
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.tasks import bootstrap, reason
from cairn.server.models import AuditRecordRequest, CompletionClaimRequest, CreateAuditRequest, FinalAssessment

from test_audit_api import prepare
from test_audit_workflow import api, configuration, repository, install_cli


@pytest.fixture
def audit(api, tmp_path, request):
    client, http = api
    repo = repository(tmp_path)
    config = configuration(tmp_path)
    config.controller.timeouts.overseer = config.controller.timeouts.final_review = 15
    root = Path(config.local.workspace_root)
    baseline = repository_baseline(repo, root)
    evidence = [{"path": "app.py", "start_line": 1, "end_line": 2, "sha256": baseline["files"]["app.py"]}]
    body = CreateAuditRequest(
        title="review fixture", origin="fixed local source", goal="prove the fixture path", user_goal="check the fixture",
        repo_path=str(repo), workspace_root=str(root), baseline=baseline,
        policy={"effect": "trace anonymous reachability", "deployment": "local fixture",
                "attacker_capabilities": ["anonymous caller"], "scope": ["app.py"],
                "cvss": getattr(request, "param", {}).get("cvss", {"AV": "N", "PR": "N", "UI": "N"}), "evidence_requirements": ["source"],
                "threat_model": [{"kind": "inferred", "source": "fixture"}]},
        seeds=[{"key": "entry", "description": "public function returns fixture", "audit": {
            "review_status": "verified", "evidence": evidence, "conditions": ["local fixture"],
            "validation_level": "source_analysis", "review_notes": "initial source review",
        }}],
        intents=[{"from": ["entry"], "description": "trace actual authentication"}],
    )
    response = client.create_audit(body)
    assert response.ok, response.text
    project = response.data
    prepare(project)
    pid = project["project"]["id"]
    assert client.start_audit(pid).ok
    return client, http, config, pid, repo


def completion(audit, sources=None):
    client, http, config, pid, _repo = audit
    response = client.complete(pid, sources or ["f001"], "the requested evidence is available", "claude-main", "research-session")
    assert response.status_code == 202, response.text
    return response.data


def claim(audit, request_id, **changes):
    _client, http, _config, pid, _repo = audit
    body = {"owner": "fixture-controller", "timeout_seconds": 30, "max_attempts": 2, **changes}
    response = http.post(f"/projects/{pid}/audit/completions/{request_id}/claim", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def assessment(sources=None, decision="approved"):
    return {
        "decision": decision, "reason": "checked source, actor, deployment and fixed policy",
        "proof_ids": sources or ["f001"],
        "checks": {key: decision == "approved" for key in ("effect", "deployment", "attacker_capabilities", "scope", "cvss", "evidence")},
        "cvss_vector": {"AV": "N", "AC": "L", "PR": "N", "UI": "N", "S": "U", "C": "H", "I": "H", "A": "H"},
    }


def review_body(audit, request, *, verdict=None, error=None, session="independent-final-session"):
    _client, _http, _config, pid, _repo = audit
    verdict = assessment(request["from"]) if verdict is None and error is None else verdict
    record = {
        "record_key": "final:" + request["review_token"], "object_type": "project", "object_id": pid,
        "input_digest": request["proof_digest"], "phase": "final_review", "session": session,
        "attempt_status": "error" if error else "success", "error": error,
        "decision": verdict["decision"] if verdict else None, "result": verdict,
    }
    return {"token": request["review_token"], "expected_audit_revision": request["audit_revision"],
            "policy_digest": request["policy_digest"], "proof_digest": request["proof_digest"],
            "record": record, "assessment": verdict}


def finish(audit, request, body):
    _client, http, _config, pid, _repo = audit
    return http.post(f"/projects/{pid}/audit/completions/{request['id']}/review", json=body)


def node_review(audit, object_id="f001", *, decision="approved", hint=None, data=None):
    client, _http, _config, pid, _repo = audit
    inputs = client.get_review_input(pid, "fact", object_id)
    fact = next(f for f in client.get_project(pid).facts if f.id == object_id)
    fact_data = fact.audit.model_copy(deep=True) if data is None else data
    fact_data.review_status = {"approved": "verified", "rejected": "refuted", "needs_evidence": "unverified"}[decision]
    return AuditRecordRequest(
        record_key=f"node:{object_id}:{inputs['input_digest']}:{decision}", object_type="fact", object_id=object_id,
        input_digest=inputs["input_digest"], phase="overseer", session="independent-node-session",
        attempt_status="success", decision=decision, fact_audit=fact_data, hint=hint,
        result={"reason": "checked the concrete claim against source"},
    )


def test_complete_is_idempotent_request_and_never_directly_completes(audit):
    client, _http, _config, pid, _repo = audit
    first, second = completion(audit), completion(audit)
    assert first["id"] == second["id"]
    project = client.get_project(pid)
    assert project.project.status == "active" and project.project.audit.completion_pending
    assert not any(intent.to == "goal" for intent in project.intents)
    assert client.get_audit(pid).audit_revision == 1


def test_pending_blocks_new_claims_but_preserves_existing_writeback(audit):
    client, _http, _config, pid, _repo = audit
    held = client.create_intent(pid, ["f001"], "already dispatched", "claude-main").data
    assert client.heartbeat(pid, held["id"], "claude-main").ok
    completion(audit)
    assert client.create_intent(pid, ["f001"], "new task", "claude-main").status_code == 409
    assert client.claim_reason(pid, "claude-main", "new fact").status_code == 409
    open_intent = next(i for i in client.get_project(pid).intents if i.to is None and i.id != held["id"])
    assert client.heartbeat(pid, open_intent.id, "claude-main").status_code == 409
    assert client.heartbeat(pid, held["id"], "claude-main").ok
    result = client.conclude(pid, held["id"], "claude-main", "existing result")
    assert result.ok and result.data["fact"]["audit"]["review_status"] == "unverified"


def test_independent_review_is_only_completion_path_and_receipt_is_idempotent(audit):
    client, _http, _config, pid, _repo = audit
    request = claim(audit, completion(audit)["id"])
    body = review_body(audit, request)
    response = finish(audit, request, body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert finish(audit, request, body).status_code == 200
    project = client.get_project(pid)
    assert project.project.status == "completed"
    assert len([i for i in project.intents if i.to == "goal"]) == 1
    assert next(i for i in project.intents if i.to == "goal").description.startswith("独立终审通过：")
    assert len(client.get_audit(pid).records) == 1
    assert _http.post(f"/projects/{pid}/reopen", json={"description": "rerun", "creator": "human"}).status_code == 409


@pytest.mark.parametrize("decision", ["rejected", "needs_evidence"])
def test_substantive_verdicts_release_pause_without_same_basis_auto_retry(audit, decision):
    client, _http, _config, pid, _repo = audit
    request = claim(audit, completion(audit)["id"])
    response = finish(audit, request, review_body(audit, request, verdict=assessment(decision=decision)))
    assert response.status_code == 200
    assert response.json()["status"] == decision
    assert client.get_project(pid).project.status == "active"
    assert not client.get_project(pid).project.audit.completion_pending
    assert completion(audit)["status"] == decision
    assert client.claim_completion(pid, request["id"], CompletionClaimRequest(owner="again")).status_code == 409
    assert client.create_intent(pid, ["f001"], "collect other evidence", "claude-main").ok


def test_execution_error_preserves_facts_and_same_proof_can_succeed(audit):
    client, _http, _config, pid, _repo = audit
    first = claim(audit, completion(audit)["id"])
    response = finish(audit, first, review_body(audit, first, error="CLI timeout", session=None))
    assert response.status_code == 200
    assert response.json()["status"] == "review_error" and response.json()["decision"] is None
    assert client.get_project(pid).facts[2].audit.review_status == "verified"
    second = claim(audit, first["id"])
    assert second["attempts"] == 2 and second["round_deadline"] == first["round_deadline"]
    assert finish(audit, second, review_body(audit, second)).json()["status"] == "approved"


def test_attempt_limit_releases_pause_and_explicit_retry_keeps_project_deadlines(audit):
    client, http, _config, pid, _repo = audit
    request = completion(audit)
    deadline = client.get_audit(pid).final_deadline
    for _ in range(2):
        request = claim(audit, request["id"])
        assert finish(audit, request, review_body(audit, request, error="CLI failed")).status_code == 200
    url = f"/projects/{pid}/audit/completions/{request['id']}/claim"
    assert http.post(url, json={"owner": "third"}).status_code == 409
    assert not client.get_project(pid).project.audit.completion_pending
    retry = claim(audit, request["id"], retry=True)
    assert retry["attempts"] == 1 and client.get_audit(pid).final_deadline == deadline


def test_review_requires_distinct_session_all_checks_and_matching_cvss(audit):
    request = claim(audit, completion(audit)["id"])
    assert finish(audit, request, review_body(audit, request, session="research-session")).status_code == 400
    body = review_body(audit, request)
    body["assessment"]["checks"]["deployment"] = False
    assert finish(audit, request, body).status_code == 422
    body = review_body(audit, request)
    body["assessment"]["cvss_vector"]["PR"] = "L"
    assert finish(audit, request, body).status_code == 409
    assert audit[0].get_project(audit[3]).project.status == "active"
    assert not audit[0].get_audit(audit[3]).records


def test_source_changes_cannot_be_approved_even_with_model_agreement(audit):
    request = claim(audit, completion(audit)["id"])
    (audit[4] / "app.py").write_text("changed source\n")
    assert finish(audit, request, review_body(audit, request)).status_code == 409
    assert audit[0].get_project(audit[3]).project.status == "active"


def test_related_premise_refutation_downgrades_only_dependents(audit):
    client, _http, _config, pid, _repo = audit
    original = client.get_project(pid).facts[2].audit
    dependent = client.create_intent(pid, ["f001"], "dependent", "claude-main").data
    independent = client.create_intent(pid, ["origin"], "independent", "claude-main").data
    dep = client.conclude(pid, dependent["id"], "claude-main", "derived claim", original.model_copy(update={"premise_ids": ["f001"], "review_status": "unverified"})).data["fact"]["id"]
    other = client.conclude(pid, independent["id"], "claude-main", "other observation", original.model_copy(update={"review_status": "unverified"})).data["fact"]["id"]
    assert client.record_audit_review(pid, node_review(audit, dep)).ok
    assert client.record_audit_review(pid, node_review(audit, other)).ok
    request = completion(audit, [dep])
    before = client.get_audit(pid)
    assert client.record_audit_review(pid, node_review(audit, decision="rejected", hint="entry is actually protected")).ok
    facts = {f.id: f.audit for f in client.get_project(pid).facts if f.audit}
    assert facts["f001"].review_status == "refuted"
    assert facts[dep].review_status == "unverified"
    assert facts[other].review_status == "verified"
    after = client.get_audit(pid)
    assert after.reason_revision == before.reason_revision + 1
    assert client.list_completions(pid)[0].status == "stale"
    reviewed = claim(audit, request["id"])
    assert finish(audit, reviewed, review_body(audit, reviewed)).status_code == 409


def test_duplicate_node_review_and_hint_are_atomic_and_do_not_self_retrigger(audit):
    client, _http, _config, pid, _repo = audit
    body = node_review(audit, hint="check the default deployment")
    first = client.record_audit_review(pid, body)
    assert first.status_code == 201, first.text
    meta = client.get_audit(pid)
    assert client.record_audit_review(pid, body).status_code == 200
    assert client.get_audit(pid).audit_revision == meta.audit_revision
    assert client.get_review_input(pid, "fact", "f001")["input_digest"] == first.data["output_digest"]
    assert len([h for h in client.get_project(pid).hints if h.creator == "overseer"]) == 1
    assert client.record_audit_review(pid, body.model_copy(update={"hint": "changed receipt"})).status_code == 409


def test_normal_intent_review_does_not_advance_reason_revision(audit):
    client, _http, _config, pid, _repo = audit
    intent = next(i for i in client.get_project(pid).intents if i.to is None)
    before = client.get_audit(pid)
    inputs = client.get_review_input(pid, "intent", intent.id)
    body = AuditRecordRequest(record_key="intent-approved", object_type="intent", object_id=intent.id,
                              input_digest=inputs["input_digest"], phase="overseer", session="independent",
                              attempt_status="success", decision="approved")
    assert client.record_audit_review(pid, body).ok
    assert client.get_audit(pid).audit_revision == before.audit_revision
    assert client.get_audit(pid).reason_revision == before.reason_revision


def test_stale_snapshot_does_not_complete_and_heartbeat_does_not_stale(audit):
    client, http, _config, pid, _repo = audit
    held = next(i for i in client.get_project(pid).intents if i.to is None)
    assert client.heartbeat(pid, held.id, "claude-main").ok
    request = claim(audit, completion(audit)["id"])
    assert client.heartbeat(pid, held.id, "claude-main").ok
    assert client.get_audit(pid).audit_revision == request["audit_revision"]
    http.post(f"/projects/{pid}/hints", json={"content": "new external evidence to consider", "creator": "human"})
    response = finish(audit, request, review_body(audit, request))
    assert response.status_code == 200 and response.json()["status"] == "stale"
    assert client.get_project(pid).project.status == "active"


@pytest.mark.parametrize("sources", [["origin"], ["goal"], ["f001", "f001"], ["missing"]])
def test_invalid_completion_sources_do_not_create_requests(audit, sources):
    client, _http, _config, pid, _repo = audit
    assert client.complete(pid, sources, "claim", "worker").status_code == 400
    assert client.list_completions(pid) == []


def test_monitor_reuses_shared_fake_cli_and_distinct_sessions(audit, tmp_path, monkeypatch):
    client, _http, config, pid, _repo = audit
    request = completion(audit)
    outputs = [assessment()]
    calls_file = install_cli(tmp_path, monkeypatch, outputs)
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    result = monitor_once(config, client, backend, pid)
    assert result["status"] == "completed"
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    sessions = [call["argv"][call["argv"].index("--session-id") + 1] for call in calls]
    assert len(sessions) == len(set(sessions)) == 1
    assert all(call["cwd"] == client.get_audit(pid).workspace_path for call in calls)
    assert client.list_completions(pid)[0].id == request["id"]
    records = client.get_audit(pid).records
    assert all(record.artifacts and Path(record.artifacts[0]).is_file() for record in records)


def test_monitor_duplicate_poll_does_not_make_extra_agent_calls(audit, tmp_path, monkeypatch):
    client, _http, config, pid, _repo = audit
    calls_file = install_cli(tmp_path, monkeypatch, [
        {"decision": "approved", "reason": "normal intent", "fact_audit": None, "hint": None},
    ])
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    assert monitor_audit(config, client, backend, pid, once=True)["reviewed"] == 1
    assert monitor_audit(config, client, backend, pid, once=True)["reviewed"] == 0
    assert len(calls_file.read_text().splitlines()) == 1


def test_monitor_rejects_multiple_objects_instead_of_selecting_success_example():
    raw = json.dumps(assessment(decision="rejected")) + "\nexample:\n" + json.dumps(assessment())
    with pytest.raises(ValueError):
        _strict_result(raw, FinalAssessment)


def test_audit_attach_cli_runs_one_review_round(audit, tmp_path, monkeypatch):
    client, _http, config, pid, _repo = audit
    install_cli(tmp_path, monkeypatch, [
        {"decision": "approved", "reason": "normal intent", "fact_audit": None, "hint": None},
    ])
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    monkeypatch.setattr("cairn.dispatcher.protocol.client.CairnClient", lambda _url: client)
    response = CliRunner().invoke(main, ["audit", "--project", pid, "--once", "--config", str(config_path)])
    assert response.exit_code == 0, response.output
    assert json.loads(response.output)["reviewed"] == 1


def test_reason_task_submits_request_and_scheduler_waits_for_review(audit, tmp_path, monkeypatch):
    client, _http, config, pid, _repo = audit
    install_cli(tmp_path, monkeypatch, [{"accepted": True, "data": {
        "complete": {"from": ["f001"], "description": "source objective is supported"},
    }}])
    assert client.claim_reason(pid, "claude-main", "review current evidence").ok
    project = client.get_project(pid)
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    assert reason.run_reason_task(config, client, backend, project, client.export_project(pid),
                                  config.workers[0], TaskCancellation()) == "success"
    request = client.list_completions(pid)[0]
    assert request.source_session
    assert client.get_project(pid).project.status == "active"
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config, loop.client, loop.container_manager = config, client, backend
    loop.futures, loop._cleanup_pending, loop._log_state = {}, set(), {}
    assert loop._try_dispatch_project(client.list_projects()[0]) is False
    assert loop.futures == {}


def test_bootstrap_complete_also_requires_independent_review(audit, tmp_path, monkeypatch):
    client, _http, config, pid, _repo = audit
    data = client.get_project(pid).facts[2].audit.model_copy(update={"review_status": "unverified"}).model_dump(mode="json")
    install_cli(tmp_path, monkeypatch, [{"accepted": True, "data": {
        "fact": {"description": "new source observation", "audit": data},
        "complete": {"description": "bootstrap believes the goal is met"},
    }}])
    intent = client.create_intent(pid, ["origin"], "bootstrap", "dispatcher.bootstrap").data
    assert client.heartbeat(pid, intent["id"], "claude-main").ok
    project = client.get_project(pid)
    intent = next(i for i in project.intents if i.id == intent["id"])
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    assert bootstrap.run_bootstrap_task(config, client, backend, project, intent, config.workers[0], TaskCancellation()) == "success"
    request = client.list_completions(pid)[0]
    assert request.source_session and request.status == "pending"
    updated = client.get_project(pid)
    assert updated.project.status == "active"
    assert next(f for f in updated.facts if f.id in request.from_).audit.review_status == "unverified"
    assert not any(i.to == "goal" for i in updated.intents)


def test_overseer_fact_mutation_is_invalid_output_and_retries_are_bounded(audit, tmp_path, monkeypatch):
    client, _http, config, pid, _repo = audit
    original = client.get_project(pid).facts[2].audit
    output = {"decision": "approved", "reason": "attempts to modify a fact",
              "fact_audit": original.model_dump(mode="json"), "hint": None}
    calls = install_cli(tmp_path, monkeypatch, [output, output])
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    assert monitor_audit(config, client, backend, pid, once=True)["reviewed"] == 1
    assert monitor_audit(config, client, backend, pid, once=True)["reviewed"] == 1
    assert monitor_audit(config, client, backend, pid, once=True)["reviewed"] == 0
    assert len(calls.read_text().splitlines()) == 2
    records = client.get_audit(pid).records
    assert len(records) == 2 and all(r.object_type == "intent" and r.attempt_status == "error" and r.decision is None for r in records)
    assert client.get_project(pid).facts[2].audit == original


@pytest.mark.parametrize("audit", [{"cvss": {"AV": "N", "PR": "N", "UI": "N", "any_of": [{"C": "H"}, {"I": "H"}]}}], indirect=True)
def test_cvss_disjunction_is_checked_against_the_actual_vector(audit):
    client, _http, _config, pid, _repo = audit
    request = claim(audit, completion(audit)["id"])
    body = review_body(audit, request)
    body["assessment"]["cvss_vector"].update(C="L", I="L")
    assert finish(audit, request, body).status_code == 409
    body["assessment"]["cvss_vector"]["I"] = "H"
    assert finish(audit, request, body).status_code == 200
    assert client.get_project(pid).project.status == "completed"


def test_review_cannot_remove_conditions_to_turn_a_conditional_claim_unconditional(audit):
    client, _http, _config, pid, _repo = audit
    data = client.get_project(pid).facts[2].audit.model_copy(update={"conditions": []})
    before = client.get_audit(pid)
    response = client.record_audit_review(pid, node_review(audit, data=data))
    assert response.status_code == 409
    assert client.get_project(pid).facts[2].audit.conditions == ["local fixture"]
    assert client.get_audit(pid).audit_revision == before.audit_revision
    assert not client.get_audit(pid).records


def test_cvss_constrained_policy_does_not_accept_missing_vector(audit):
    request = claim(audit, completion(audit)["id"])
    body = review_body(audit, request)
    body["assessment"]["cvss_vector"] = None
    assert finish(audit, request, body).status_code == 409


@pytest.mark.parametrize("audit", [{"cvss": {}}], indirect=True)
def test_source_only_goal_without_cvss_constraints_does_not_force_fake_vulnerability_score(audit):
    request = claim(audit, completion(audit)["id"])
    body = review_body(audit, request)
    body["assessment"]["cvss_vector"] = None
    assert finish(audit, request, body).status_code == 200


def test_intent_rejection_only_writes_hint_without_cancelling_research(audit, tmp_path, monkeypatch):
    client, _http, config, pid, _repo = audit
    original = client.get_project(pid)
    intent = next(i for i in original.intents if i.to is None)
    lease = client.heartbeat(pid, intent.id, "claude-main")
    assert lease.ok
    install_cli(tmp_path, monkeypatch, [
        {"decision": "rejected", "reason": "task assumes a missing initial capability", "fact_audit": None,
         "hint": "Keep the initial capability unknown and investigate its source."},
    ])
    backend = LocalBackend(LocalConfig(workspace_root=config.local.workspace_root))
    assert monitor_audit(config, client, backend, pid, once=True)["reviewed"] == 1
    current = client.get_project(pid)
    assert current.project.status == "active"
    assert current.facts == original.facts
    assert next(i for i in current.intents if i.id == intent.id).worker == "claude-main"
    assert next(i for i in current.intents if i.id == intent.id).to is None
    assert len(current.hints) == len(original.hints) + 1


@pytest.mark.parametrize("audit", [{"cvss": []}, {"cvss": {"any_of": [{}]}}], indirect=True)
def test_invalid_cvss_policy_shape_is_an_explicit_error_without_approval(audit):
    request = claim(audit, completion(audit)["id"])
    response = finish(audit, request, review_body(audit, request))
    assert response.status_code == 400
    assert audit[0].get_project(audit[3]).project.status == "active"
