from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
import yaml

from cairn.audit_evidence import json_digest, repository_baseline
from cairn.controller import audit as controller_audit, builder, monitor
from cairn.controller.app import AgentCallResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.server.models import AuditRecordRequest, ClosingResult, FinalAssessment

from test_audit_budget import clock, close_result, finalize_phase
from test_audit_review import audit, node_review
from test_audit_workflow import api, configuration, plan, repository
from test_kritt_audit_reconnect import ready
from test_kritt_probe import probe


def plan_context():
    value = plan()
    value["goal"] = "original goal must survive"
    value["policy"]["effect"] = "effect is not the full goal"
    value["seeds"] = [{
        "key": key, "description": f"claim {key}\n\ndetailed analysis of {key}",
        "audit": {"conditions": [f"condition {key}"], "premise_ids": [],
                  "review_notes": f"notes {key}", "review_status": "unverified",
                  "evidence": [{"path": "app.py", "start_line": 1, "sha256": "a" * 64}]},
    } for key in ("first", "other")]
    return {"plan": value, "user_goal": "additional user boundary", "repo_path": "/test/source",
            "baseline": {"files": {"app.py": "a" * 64}, "digest": "b" * 64, "git_commit": "c" * 40},
            "requirements_path": "/test/requirements.json"}


def test_preflight_delivers_contract_and_short_plan_with_full_material_entry(tmp_path):
    context = plan_context()
    before = deepcopy(context)
    payload = controller_audit._plan_payload("preflight", context, tmp_path)
    assert payload["fixed_contract"] == {
        "goal": context["plan"]["goal"], "policy": context["plan"]["policy"], "user_goal": context["user_goal"],
    }
    assert "plan" not in payload and "seeds" not in payload
    assert all(seed["truncated"] for seed in payload["plan_overview"]["seeds"])
    assert "detailed analysis" not in json.dumps(payload)
    stored = json.loads(Path(payload["plan_reference"]).read_text())
    assert stored["seeds"][1]["description"] == context["plan"]["seeds"][1]["description"]
    assert stored["seeds"][1]["audit"]["conditions"] == ["condition other"]
    assert "sha256" not in stored["seeds"][1]["audit"]["evidence"][0]
    assert payload["baseline"] == {"git_commit": "c" * 40}
    assert context == before


def test_seed_review_inlines_only_assigned_full_seeds_and_keeps_others_accessible(tmp_path):
    context = {**plan_context(), "review_keys": ["first"]}
    payload = controller_audit._plan_payload("seed_review", context, tmp_path)
    assert [seed["key"] for seed in payload["seeds"]] == ["first"]
    assert payload["seeds"][0]["description"] == context["plan"]["seeds"][0]["description"]
    assert payload["seeds"][0]["audit"]["conditions"] == ["condition first"]
    assert payload["seeds"][0]["audit"]["review_notes"] == "notes first"
    assert payload["other_seed_keys"] == ["other"]
    assert "detailed analysis of other" not in json.dumps(payload)
    stored = json.loads(Path(payload["plan_reference"]).read_text())
    assert {seed["key"] for seed in stored["seeds"]} == {"first", "other"}


def successful_call(kwargs, data=None):
    raw = json.dumps(data) if data is not None else "invalid-json"
    return AgentCallResult(role=kwargs["role"], worker="fixture", phase=kwargs["phase"],
        workspace=str(kwargs["workspace"]), session=kwargs.get("session") or "original-session",
        prompt=kwargs["prompt"], status="success" if data is not None else "invalid_output",
        data=data, error=None if data is not None else "invalid JSON", response=raw,
        process=ProcessResult(0, raw, ""), elapsed_seconds=0)


def assert_model_schema(schema):
    assert "sha256" not in schema["$defs"]["EvidenceReference"]["properties"]


def test_initial_local_builder_projects_supplied_seeds_without_inventing_contract(tmp_path, monkeypatch):
    repo, config = repository(tmp_path), configuration(tmp_path)
    root = Path(config.local.workspace_root)
    baseline = repository_baseline(repo, root)
    seed = plan_context()["plan"]["seeds"][0]
    context = {"user_goal": "raw initial requirement", "repo_path": str(repo),
               "baseline": baseline, "input_seeds": [seed]}
    before = deepcopy(context)
    captured = []

    def call(_config, _backend, **kwargs):
        captured.append(json.loads(kwargs["prompt"].rsplit("\n\n", 1)[1]))
        return successful_call(kwargs, plan())

    monkeypatch.setattr(controller_audit, "call_agent", call)
    folder = tmp_path / "initial-build"
    folder.mkdir()
    controller_audit._stage(config, LocalBackend(config.local), repo, root, baseline, folder, [], TaskCancellation(),
                            "build", "builder", controller_audit.AuditPlan, context)
    payload = captured[0]
    assert "fixed_contract" not in payload
    assert payload["user_goal"] == context["user_goal"]
    assert payload["input_seeds"][0]["description"] == seed["description"]
    assert payload["input_seeds"][0]["audit"]["conditions"] == seed["audit"]["conditions"]
    assert payload["input_seeds"][0]["audit"]["review_notes"] == seed["audit"]["review_notes"]
    assert "sha256" not in payload["input_seeds"][0]["audit"]["evidence"][0]
    assert context == before


def test_local_builder_revision_and_format_repair_keep_contract_and_material_entry(tmp_path, monkeypatch):
    repo, config = repository(tmp_path), configuration(tmp_path)
    root = Path(config.local.workspace_root)
    baseline = repository_baseline(repo, root)
    context = {**plan_context(), "repo_path": str(repo), "baseline": baseline,
               "preflight_issues": ["preserve the initial boundary"]}
    folder = tmp_path / "stage"
    folder.mkdir()
    captured = []

    def call(_config, _backend, **kwargs):
        captured.append(json.loads(kwargs["prompt"].rsplit("\n\n", 1)[1]))
        return successful_call(kwargs, plan() if len(captured) == 2 else None)

    monkeypatch.setattr(controller_audit, "call_agent", call)
    controller_audit._stage(config, LocalBackend(config.local), repo, root, baseline, folder, [], TaskCancellation(),
                            "build", "builder", controller_audit.AuditPlan, context)
    assert len(captured) == 2
    for payload in captured:
        assert "plan" not in payload
        assert payload["fixed_contract"]["goal"] == context["plan"]["goal"]
        assert payload["fixed_contract"]["policy"] == context["plan"]["policy"]
        assert payload["fixed_contract"]["user_goal"] == context["user_goal"]
        assert payload["requirements_path"] == context["requirements_path"]
        assert payload["preflight_issues"] == context["preflight_issues"]
        assert_model_schema(payload["output_schema"])
    assert captured[0]["plan_reference"] == captured[1]["plan_reference"]
    assert json.loads(Path(captured[0]["plan_reference"]).read_text())["goal"] == context["plan"]["goal"]
    assert json.loads((folder / "01-build/raw-context.json").read_text()) == context


@pytest.mark.parametrize("revision", [False, True])
def test_builder_answer_and_format_repair_keep_known_contract_without_inventing_one(probe, monkeypatch, revision):
    config, prepared, original, _baseline = ready(probe)
    current_plan = controller_audit.AuditPlan.model_validate(original) if revision else None
    captured, sessions = [], []
    answers = iter([None, {"action": "ask", "questions": ["确认部署状态？"]}, {"action": "finish", **original}])

    def call(_config, _backend, **kwargs):
        captured.append(json.loads(kwargs["prompt"].rsplit("\n\n", 1)[1]))
        sessions.append(kwargs["session"])
        return successful_call(kwargs, next(answers))

    monkeypatch.setattr(builder, "call_agent", call)
    result = builder.continue_builder(config, prepared, current_plan=current_plan,
        preflight_issues=["preserve the initial boundary"] if revision else None,
        ask=lambda questions: "部署尚未验证")
    assert result["status"] == "plan_ready" and len(captured) == 3
    assert len(set(sessions)) == 1
    assert captured[-1]["reply"] == "部署尚未验证"
    for payload in captured:
        assert "plan" not in payload
        assert payload["requirements_path"] == prepared["requirements"]
        assert_model_schema(payload["output_schema"])
        if revision:
            assert payload["fixed_contract"] == {"goal": original["goal"], "policy": original["policy"],
                                                   "user_goal": prepared["user_goal"]}
            assert payload["preflight_issues"] == ["preserve the initial boundary"]
            assert payload["plan_reference"] == captured[0]["plan_reference"]
        else:
            assert "fixed_contract" not in payload and payload["user_goal"] == prepared["user_goal"]


def snapshot(payload):
    reference = payload["graph_reference"]
    full = Path(re.search(r"Full graph: (.+)", reference)[1])
    index = Path(re.search(r"Graph navigation: (.+)", reference)[1])
    return yaml.safe_load(full.read_text()), index.read_text()


@pytest.mark.parametrize("phase", ["overseer", "closing", "final_review"])
def test_monitor_scopes_default_payload_without_changing_raw_review_input(audit, tmp_path, monkeypatch, phase):
    client, _http, config, pid, _repo = audit
    project = client.get_project(pid)
    seed = next(fact for fact in project.facts if fact.id == "f001")
    # An off-candidate counterclaim must stay accessible through the global snapshot.
    contrary = seed.model_copy(deep=True, update={"id": "f002", "description": "counterevidence outside the candidate"})
    project.facts.append(contrary)
    seed.audit.conditions = ["identity and timing remain required"]
    seed.audit.review_notes = "read the existing experiment record"
    intent = next(intent for intent in project.intents if intent.to is None)
    context = client.get_review_input(pid, "intent", intent.id)["context"] if phase == "overseer" else {
        "graph": project.model_dump(mode="json", by_alias=True), "policy": project.project.audit.policy,
        **({"request": {"id": "r1", "from": [seed.id], "description": "candidate claim",
                        "proof_digest": "d" * 64, "review_token": "secret ownership"}} if phase == "final_review" else {}),
    }
    digest = json_digest(context)
    backend = LocalBackend(config.local)
    workspace = Path(project.project.audit.workspace_path)
    captured, scans = [], []
    original_scan = monitor.research_workspace

    def scan(*args):
        scans.append(True)
        return original_scan(*args)

    def answer(_config, _backend, **kwargs):
        payload = json.loads(kwargs["prompt"].rsplit("\n\n", 1)[1])
        captured.append(payload)
        if phase == "overseer":
            data = {"decision": "approved", "reason": "research direction remains in scope", "fact_audit": None}
        elif phase == "closing":
            data = {"kind": "no_candidate", "from": [], "description": "read the whole graph"}
        else:
            data = {"decision": "needs_evidence", "reason": "entry remains unknown", "proof_ids": [seed.id],
                    "checks": dict.fromkeys(["effect", "deployment", "attacker_capabilities", "scope", "cvss", "evidence"], False)}
        raw = json.dumps(data)
        return AgentCallResult(role=kwargs["role"], worker="fixture", phase=phase, workspace=str(workspace),
                               session="independent", prompt=kwargs["prompt"], status="success", data=data,
                               response=raw, process=ProcessResult(0, raw, ""), elapsed_seconds=0)

    monkeypatch.setattr(monitor, "research_workspace", scan)
    monkeypatch.setattr(monitor, "call_agent", answer)
    model = {"overseer": monitor.NodeAssessment, "closing": ClosingResult, "final_review": FinalAssessment}[phase]
    result, artifact = monitor._invoke(config, client, backend, project, phase=phase, context=context,
                                      model=model, timeout=10, cancellation=TaskCancellation())
    assert result.status == "success" and len(scans) == (0 if phase == "overseer" else 2)
    payload = captured[0]
    assert "graph" not in payload and "policy" not in payload
    assert payload["fixed_contract"]["goal"] == next(f.description for f in project.facts if f.id == "goal")
    assert payload["fixed_contract"]["policy"] == project.project.audit.policy
    graph, index = snapshot(payload)
    assert any(f["id"] == "f002" for f in graph["facts"]) and "f002" in index
    assert "counterevidence outside the candidate" not in json.dumps(payload)
    assert json_digest(context) == digest
    assert json.loads(artifact.with_name("raw-context.json").read_text()) == context
    if phase == "overseer":
        assert "reference_check" not in payload and "premises" not in payload
        assert "audit" not in payload["target"] and payload["target"]["id"] == intent.id
        assert payload["target"]["from"] == intent.from_
        saved_seed = next(fact for fact in graph["facts"] if fact["id"] == seed.id)
        assert saved_seed["audit"]["conditions"] == seed.audit.conditions
        assert saved_seed["audit"]["review_notes"] == seed.audit.review_notes
        assert "sha256" not in saved_seed["audit"]["evidence"][0]
    elif phase == "final_review":
        assert payload["request"] == {"id": "r1", "from": [seed.id], "description": "candidate claim"}
        assert set(payload["proof_facts"]) == {seed.id}
        assert payload["proof_facts"][seed.id]["audit"]["conditions"] == seed.audit.conditions
        assert payload["proof_facts"][seed.id]["audit"]["review_notes"] == seed.audit.review_notes


def test_overseer_reads_transitive_source_conditions_from_graph_on_demand(audit):
    client, _http, config, pid, _repo = audit
    project = client.get_project(pid)
    source = next(fact for fact in project.facts if fact.id == "f001")
    source.audit.conditions = ["dependency condition retained only in graph"]
    derived = source.model_copy(deep=True, update={"id": "f002", "description": "derived observation"})
    derived.audit.premise_ids = [source.id]
    derived.audit.conditions = ["derived condition retained only in graph"]
    project.facts.append(derived)
    intent = next(intent for intent in project.intents if intent.to is None)
    intent.from_ = [derived.id]
    context = {"target": {"id": intent.id, "description": intent.description, "to_fact_id": None},
               "premises": {fact.id: {"description": fact.description, "audit": fact.audit.model_dump(mode="json")}
                            for fact in (source, derived)}}
    original = json_digest(context)
    payload = monitor._review_payload(LocalBackend(config.local), Path(project.project.audit.workspace_path),
                                      project, "overseer", context)
    assert payload["target"]["from"] == [derived.id] and "premises" not in payload
    assert "retained only in graph" not in json.dumps(payload)
    graph, _index = snapshot(payload)
    facts = {fact["id"]: fact for fact in graph["facts"]}
    assert facts[derived.id]["audit"]["premise_ids"] == [source.id]
    for fact in (source, derived):
        assert facts[fact.id]["audit"]["conditions"] == fact.audit.conditions
    assert json_digest(context) == original


def test_final_payload_preserves_transitive_prerequisites_and_other_branch_navigation(audit):
    client, _http, config, pid, _repo = audit
    project = client.get_project(pid)
    seed = next(fact for fact in project.facts if fact.id == "f001")
    derived = seed.model_copy(deep=True, update={"id": "f002", "description": "conditional derived claim"})
    derived.audit.premise_ids = [seed.id]
    project.facts.append(derived)
    payload = monitor._review_payload(LocalBackend(config.local), Path(project.project.audit.workspace_path),
                                      project, "final_review", {"request": {"from": [derived.id]}})
    assert set(payload["proof_facts"]) == {seed.id, derived.id}
    assert payload["proof_facts"][derived.id]["audit"]["premise_ids"] == [seed.id]


def test_idle_monitor_does_not_scan_source_or_start_agent(audit, monkeypatch):
    client, _http, config, pid, _repo = audit
    assert client.record_audit_review(pid, node_review(audit)).ok
    intent = next(i for i in client.get_project(pid).intents if i.to is None)
    inputs = client.get_review_input(pid, "intent", intent.id)
    record = AuditRecordRequest(record_key="done-intent", object_type="intent", object_id=intent.id,
        input_digest=inputs["input_digest"], phase="overseer", session="independent", attempt_status="success", decision="approved")
    assert client.record_audit_review(pid, record).ok
    monkeypatch.setattr(monitor, "research_workspace", lambda *args: pytest.fail("idle monitor scanned the source"))
    monkeypatch.setattr(monitor, "call_agent", lambda *args, **kwargs: pytest.fail("idle monitor started an Agent"))
    assert monitor.monitor_audit(config, client, LocalBackend(config.local), pid, once=True)["reviewed"] == 0


@pytest.mark.parametrize("change_during_call", [False, True])
def test_final_review_still_checks_source_before_and_after_agent(audit, monkeypatch, change_during_call):
    client, _http, config, pid, repo = audit
    project = client.get_project(pid)
    context = {"request": {"from": ["f001"], "description": "candidate"}}
    calls = []

    def answer(_config, _backend, **kwargs):
        calls.append(True)
        (repo / "app.py").write_text("changed during the isolated test\n")
        data = {"decision": "needs_evidence", "reason": "conditional claim", "proof_ids": ["f001"],
                "checks": dict.fromkeys(["effect", "deployment", "attacker_capabilities", "scope", "cvss", "evidence"], False)}
        raw = json.dumps(data)
        return AgentCallResult(role="reviewer", worker="fixture", phase="final_review", workspace=kwargs["workspace"].as_posix(),
            session="isolated-test", prompt=kwargs["prompt"], status="success", data=data, response=raw,
            process=ProcessResult(0, raw, ""), elapsed_seconds=0)

    monkeypatch.setattr(monitor, "call_agent", answer)
    if not change_during_call:
        (repo / "app.py").write_text("changed before the isolated test\n")
    with pytest.raises(ValueError, match="baseline check failed"):
        monitor._invoke(config, client, LocalBackend(config.local), project, phase="final_review", context=context,
                        model=FinalAssessment, timeout=10, cancellation=TaskCancellation())
    assert len(calls) == int(change_during_call)
    assert client.get_project(pid).project.status == "stopped"


@pytest.mark.parametrize("changed", [False, True])
def test_cached_closing_checks_source_before_first_submission(audit, clock, monkeypatch, changed):
    client, _http, config, pid, repo = audit
    finalize_phase(audit, clock)
    close_result(audit)
    scans, submitted = [], []
    original_scan, original_submit = monitor.research_workspace, client.submit_closing

    def scan(*args):
        scans.append(True)
        return original_scan(*args)

    def submit(*args):
        submitted.append(True)
        return original_submit(*args)

    monkeypatch.setattr(monitor, "research_workspace", scan)
    monkeypatch.setattr(client, "submit_closing", submit)
    monkeypatch.setattr(monitor, "_invoke", lambda *args, **kwargs: pytest.fail("cached closing invoked an Agent"))
    if changed:
        (repo / "app.py").write_text("changed baseline\n")
        with pytest.raises(ValueError, match="baseline check failed"):
            monitor._closing_once(config, client, LocalBackend(config.local), client.get_project(pid), TaskCancellation())
        assert not submitted and not client.list_completions(pid)
        assert client.get_project(pid).project.status == "stopped"
    else:
        monitor._closing_once(config, client, LocalBackend(config.local), client.get_project(pid), TaskCancellation())
        assert len(submitted) == 1 and len(client.list_completions(pid)) == 1
    assert len(scans) == 1
