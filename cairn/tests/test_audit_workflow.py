from __future__ import annotations

from concurrent.futures import Future
import copy
import json
import os
from pathlib import Path
import sys

from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from cairn.cli import main
from cairn.controller.audit import (
    AuditPlan, SeedReviewResult, apply_seed_reviews, create_local_audit,
)
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.models import ReasonCheckpoint, RunningTask
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.tasks import explore, reason
from cairn.dispatcher.tasks.audit import (
    explore_result, reason_result, research_payload, research_prompt, research_workspace, supported_fact_ids,
)
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import AuditFactData, AuditInitialIntent, Fact

from conftest import make_config, make_project


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as http:
        class InProcessSession:
            def get(self, url, **_kwargs):
                return http.get(url)

            def request(self, method, url, **kwargs):
                kwargs.pop("timeout", None)  # TestClient 不是 requests 的网络传输。
                return http.request(method, url, **kwargs)

        client = CairnClient("http://testserver")
        session = InProcessSession()
        monkeypatch.setattr(client, "_session", lambda: session)
        yield client, http
        client.close()


def configuration(tmp_path):
    payload = make_config().model_dump()
    payload["runtime"].update(execution="local", interval=1, worker_healthcheck="disabled", max_workers=1, max_project_workers=1)
    payload["workers"][0].update(type="claudecode", name="claude-main")
    payload["local"] = {"workspace_root": str(tmp_path / "work"), "completed_action": "keep"}
    payload["controller"] = {
        "roles": {"builder": "claude-main", "reviewer": "claude-main"},
        "research_prompt_group": "audit", "max_goal_revisions": 0,
        "timeouts": {"build": 10, "preflight": 10, "seed_review": 10},
    }
    return DispatchConfig.model_validate(payload)


def repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "app.py").write_text("def public():\n    return 'fixture'\n", encoding="utf-8")
    return repo


def plan():
    return {
        "title": "controlled audit", "origin": "local source", "goal": "trace anonymous entry",
        "policy": {"effect": "trace anonymous reachability", "deployment": "local fixture",
                   "attacker_capabilities": ["anonymous caller"], "scope": ["app.py"], "cvss": {},
                   "evidence_requirements": ["actual source"], "threat_model": [{"source": "fixture", "kind": "inferred"}]},
        "seeds": [{"key": "entry", "description": "public function declared", "audit": {"review_status": "verified"}}],
        "intents": [{"from": ["entry"], "description": "verify the actual entry path"}],
    }


def review(status="verified", path="app.py", line=1):
    return {"reviews": [{"key": "entry", "audit": {
        "review_status": status, "evidence": [{"path": path, "start_line": line}],
        "conditions": ["local fixture"], "validation_level": "source_analysis",
        "review_notes": "read the actual source",
    }}]}


def observation():
    return {"accepted": True, "data": {"description": "function returns the fixture marker; deployment reachability is not proven",
        "audit": {"evidence": [{"path": "app.py", "start_line": 1, "end_line": 2}],
                  "conditions": ["local fixture"], "premise_ids": [], "review_status": "unverified",
                  "validation_level": "source_analysis", "review_notes": "local source observation"}}}


def install_cli(tmp_path, monkeypatch, outputs):
    responses = tmp_path / "responses.json"
    responses.write_text(json.dumps(outputs), encoding="utf-8")
    index, calls = tmp_path / "index", tmp_path / "calls.jsonl"
    monkeypatch.setenv("CP3_RESPONSES", str(responses))
    monkeypatch.setenv("CP3_INDEX", str(index))
    monkeypatch.setenv("CP3_CALLS", str(calls))
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    script = folder / "claude"
    script.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
if "--help" in sys.argv:
    print("fake Claude CLI")
    sys.exit(0)
index = pathlib.Path(os.environ["CP3_INDEX"])
n = int(index.read_text()) if index.exists() else 0
outputs = json.loads(pathlib.Path(os.environ["CP3_RESPONSES"]).read_text())
index.write_text(str(n + 1))
with open(os.environ["CP3_CALLS"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
print(json.dumps(outputs[n]))
''', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])
    return calls


def create(config, client, repo, *, cancellation=None):
    root = Path(config.local.workspace_root)
    return create_local_audit(config, client, LocalBackend(LocalConfig(workspace_root=str(root))),
                              repo=repo, user_goal="trace anonymous entry", workspace_root=root, cancellation=cancellation)


def test_controller_builds_reviews_and_activates_using_shared_cli(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    calls_path = install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    result = create(config, client, repo)
    project = client.get_project(result["project_id"])
    assert project.project.status == "active" and project.project.mode == "audit"
    assert project.project.bootstrap_enabled is False
    seed = next(fact for fact in project.facts if fact.id == project.project.audit.seed_ids["entry"])
    assert seed.audit.review_status == "verified" and seed.audit.evidence[0].sha256
    audit = client.get_audit(result["project_id"])
    assert [entry.phase for entry in audit.records] == ["build", "preflight", "seed_review"]
    sessions = {entry.session for entry in audit.records}
    assert len(sessions) == 3 and None not in sessions
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert len(calls) == 3 and all("--session-id" in call["argv"] for call in calls)
    assert all(call["cwd"] == result["controller_run"] for call in calls)
    assert Path(result["workspace"], "repo").resolve() == repo
    assert all(Path(path).is_file() for entry in audit.records for path in entry.artifacts)
    assert client.start_audit(result["project_id"]).data["started_at"] == audit.started_at


def test_controller_and_dispatcher_write_audited_fact_end_to_end(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    calls_path = install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review(), observation()])
    result = create(config, client, repo)
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    loop = DispatcherLoop(config_path)
    loop.client.close()
    loop.client = client
    loop.run(once=True)
    project = client.get_project(result["project_id"])
    facts = [fact for fact in project.facts if fact.id not in {"origin", "goal", *project.project.audit.seed_ids.values()}]
    assert len(facts) == 1
    assert facts[0].audit.review_status == "unverified"
    assert facts[0].audit.evidence[0].path == "app.py" and facts[0].audit.evidence[0].sha256
    assert any(intent.to == facts[0].id for intent in project.intents)
    assert project.project.audit.reason_revision == 2
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert calls[-1]["cwd"] == result["workspace"]
    assert "审计" in calls[-1]["argv"][-1]
    receipts = list(Path(result["workspace"], "research").glob("explore_execute-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["session"] == calls[-1]["argv"][calls[-1]["argv"].index("--session-id") + 1]
    assert receipt["process"]["returncode"] == 0
    assert project.project.status != "completed"


@pytest.mark.parametrize("path,line", [("missing.py", 1), ("app.py", 500), ("../outside.py", 1)])
def test_unverifiable_seed_is_kept_unverified_with_verification_intent(api, tmp_path, monkeypatch, path, line):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review(path=path, line=line)])
    project = client.get_project(create(config, client, repo)["project_id"])
    seed = next(fact for fact in project.facts if fact.audit)
    assert seed.audit.review_status == "unverified"
    assert "引用复核未通过" in seed.audit.review_notes
    assert any("独立验证前提" in intent.description for intent in project.intents)


def test_preflight_rejection_does_not_create_a_project(api, tmp_path, monkeypatch):
    client, http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": False, "issues": ["deployment condition is ambiguous"]}])
    with pytest.raises(ValueError, match="preflight failed"):
        create(config, client, repo)
    assert http.get("/projects").json() == []
    assert list((tmp_path / "work/controller").rglob("result.json"))


def test_goal_revision_continues_builder_but_keeps_independent_reviewers(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    config.controller.max_goal_revisions = 1
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": False, "issues": ["clarify scope"]}, plan(), {"approved": True, "issues": []}, review()])
    result = create(config, client, repo)
    audit = client.get_audit(result["project_id"])
    assert audit.user_goal == "trace anonymous entry"
    assert len({record.session for record in audit.records}) == 4
    builders = [record.session for record in audit.records if record.phase == "build"]
    assert len(builders) == 2 and builders[0] == builders[1]
    assert [record.decision for record in audit.records if record.phase == "preflight"] == ["rejected", "approved"]


def test_policy_schema_and_bounded_format_repair_do_not_rerun_research(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    malformed = plan()
    malformed["policy"]["scope"] = "app.py"
    calls_path = install_cli(tmp_path, monkeypatch, [malformed, plan(), {"approved": True, "issues": []}, review()])
    project_id = create(config, client, repo)["project_id"]
    records = client.get_audit(project_id).records
    assert [record.attempt_status for record in records] == ["error", "success", "success", "success"]
    assert records[0].decision is None and records[0].error
    assert records[1].phase == "build_format_repair"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert "不重新研究" in calls[1]["argv"][-1] and "previous_response" in calls[1]["argv"][-1]
    schema = AuditPlan.model_json_schema()
    assert schema["$defs"]["AuditPolicy"]["properties"]["scope"]["type"] == "array"


def test_review_references_use_node_ids_and_can_repair_description_alias(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    bad = review()
    bad["reviews"][0]["audit"]["premise_ids"] = [plan()["origin"]]
    fixed = review()
    fixed["reviews"][0]["audit"]["premise_ids"] = ["origin"]
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, bad, fixed])
    project_id = create(config, client, repo)["project_id"]
    audit = client.get_audit(project_id)
    assert [record.attempt_status for record in audit.records] == ["success", "success", "error", "success"]
    assert audit.records[-1].phase == "seed_review_format_repair"
    seed = next(fact for fact in client.get_project(project_id).facts if fact.audit)
    assert seed.audit.premise_ids == ["origin"] and seed.audit.review_status == "verified"


def test_seed_reviews_must_cover_all_keys_and_preserve_unsupported_premises(tmp_path):
    repo = repository(tmp_path)
    audit_plan = AuditPlan.model_validate(plan())
    with pytest.raises(ValueError, match="every seed"):
        apply_seed_reviews(audit_plan, SeedReviewResult(reviews=[]), repo)
    parent = copy.deepcopy(plan()["seeds"][0])
    parent["key"] = "parent"
    audit_plan.seeds.append(type(audit_plan.seeds[0]).model_validate(parent))
    results = review()
    results["reviews"][0]["audit"]["premise_ids"] = ["parent"]
    results["reviews"].append({"key": "parent", "audit": {"review_status": "unverified"}})
    apply_seed_reviews(audit_plan, SeedReviewResult.model_validate(results), repo)
    assert audit_plan.seeds[0].audit.review_status == "unverified"


def test_mode_selects_prompt_and_contract_without_polluting_generic(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    audited = client.get_project(create(config, client, repo)["project_id"])
    generic = make_project()
    replacements = {"graph_yaml": "/graph.yaml", "intent_id": "i001", "intent_description": "look", "fact_ids": "[]", "open_intents": "[]", "max_intents": "2", "origin": "start", "goal": "end", "hints": "[]"}
    for name in ["reason.md", "explore.md", "explore_conclude.md", "bootstrap.md", "bootstrap_conclude.md"]:
        assert "固定审计上下文" in research_prompt(config, audited, name, replacements)
        assert "固定审计上下文" not in research_prompt(config, generic, name, replacements)
    assert explore_result(generic, {"description": "generic fact"}) == ("fact", "generic fact", None)
    with pytest.raises(ValidationError):
        explore_result(audited, {"accepted": True, "data": {"description": "missing evidence metadata"}})
    forged = observation()
    forged["data"]["audit"]["review_status"] = "verified"
    with pytest.raises(ValueError, match="unverified"):
        explore_result(audited, forged)
    with pytest.raises(ValueError, match="unsupported"):
        reason_result(audited, {"accepted": True, "data": {"complete": {"from": ["origin"], "description": "claim"}}}, open_intents_empty=False, max_intents=2)
    truncated = json.dumps(observation())[:-1]
    assert research_payload(audited, truncated)["accepted"] is True
    assert explore_result(audited, research_payload(audited, truncated))[0] == "fact"
    rejected = json.dumps({"accepted": False, "data": observation()})[:-1]
    assert research_payload(audited, rejected)["accepted"] is False
    with pytest.raises(ValueError, match="top-level"):
        research_payload(audited, json.dumps(observation()["data"]))


def test_reason_does_not_self_trigger_and_keeps_changes_during_execution(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    project = client.get_project(create(config, client, repo)["project_id"])
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.reason_checkpoints = {project.project.id: ReasonCheckpoint(len(project.facts), len(project.hints), 1, 1)}
    client.create_intent(project.project.id, ["origin"], "another direction", "reason")
    project = client.get_project(project.project.id)
    assert project.project.audit.audit_revision == 2 and project.project.audit.reason_revision == 1
    assert loop._reason_trigger(project) is None
    loop.futures = {}
    loop._log_state = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    future = Future()
    loop.futures[future] = RunningTask(project.project.id, "reason", "claude-main", TaskCancellation(), fact_count=len(project.facts), hint_count=len(project.hints), open_intent_count=2, reason_revision=1)
    project.project.audit.reason_revision = 2
    future.set_result("success")
    loop._reap_futures()
    assert loop.reason_checkpoints[project.project.id].reason_revision == 1
    assert "reason_revision:1->2" in loop._reason_trigger(project)


def test_source_changes_stop_research_instead_of_reading_new_baseline(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    project = client.get_project(create(config, client, repo)["project_id"])
    (repo / "new.py").write_text("new source\n")
    with pytest.raises(ValueError, match="baseline_changed"):
        research_workspace(config, client, LocalBackend(LocalConfig()), project)
    assert client.get_project(project.project.id).project.status == "stopped"


def test_explore_conclude_fallback_keeps_audit_contract_and_session(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    calls_path = install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review(), "not structured", observation()])
    result = create(config, client, repo)
    project = client.get_project(result["project_id"])
    intent = next(item for item in project.intents if item.to is None)
    client.heartbeat(project.project.id, intent.id, "claude-main")
    outcome = explore.run_explore_task(config, client, LocalBackend(LocalConfig(workspace_root=str(tmp_path / "other"))),
                                       project, client.export_project(project.project.id), intent, config.workers[0], TaskCancellation())
    assert outcome == "success"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    first, second = calls[-2]["argv"], calls[-1]["argv"]
    assert first[first.index("--session-id") + 1] == second[second.index("-r") + 1]
    updated = client.get_project(project.project.id)
    fact_id = next(item for item in updated.intents if item.id == intent.id).to
    assert next(fact for fact in updated.facts if fact.id == fact_id).audit.evidence[0].sha256


def test_audit_cli_resolves_workspace_relative_to_configuration(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    config.local.workspace_root = "relative-work"
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    monkeypatch.setattr("cairn.dispatcher.protocol.client.CairnClient", lambda _url: client)
    result = CliRunner().invoke(main, ["audit", "--once", "--config", str(config_path), "--repo", str(repo), "--goal", "trace anonymous entry"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert Path(data["workspace"]).parent == tmp_path / "relative-work"
    assert data["status"] == "active"


@pytest.mark.parametrize("accepted", [True, False])
def test_cp3_r1_single_fenced_answer_preserves_acceptance(accepted):
    project = make_project()
    project.project.mode = "audit"
    payload = observation() if accepted else {"accepted": False, "reason": "not verified"}
    for raw in (json.dumps(payload), json.dumps(payload)[:-1]):
        assert research_payload(project, f"```json\n{raw}\n```") == payload


@pytest.mark.parametrize("form", ["refusal_then_example", "conflicting_fences", "conflicting_raw", "duplicate_acceptance"])
def test_cp3_r1_ambiguous_wrappers_are_not_accepted(form):
    project = make_project()
    project.project.mode = "audit"
    success = json.dumps(observation())
    refusal = '{"accepted": false, "reason": "not verified"}'
    outputs = {
        "refusal_then_example": refusal + f"\nExample:\n```json\n{success}\n```",
        "conflicting_fences": f"```json\n{refusal}\n```\n```json\n{success}\n```",
        "conflicting_raw": success + "\n" + refusal,
        "duplicate_acceptance": success.replace('"accepted": true', '"accepted": false, "accepted": true'),
    }
    with pytest.raises(ValueError, match="top-level"):
        research_payload(project, outputs[form])


@pytest.mark.parametrize("action", ["fact", "intents", "complete"])
def test_cp3_r1_refusal_example_never_writes_graph_or_complete(api, tmp_path, monkeypatch, action):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    result = create(config, client, repo)
    project = client.get_project(result["project_id"])
    seed_id = project.project.audit.seed_ids["entry"]
    candidate = observation() if action == "fact" else {"accepted": True, "data": {
        action: [{"from": [seed_id], "description": "EXAMPLE_ONLY"}] if action == "intents"
        else {"from": [seed_id], "description": "EXAMPLE_ONLY"}}}
    raw = '{"accepted": false, "reason": "not verified"}\nExample:\n```json\n' + json.dumps(candidate) + "\n```"
    complete_calls = []
    original_complete = client.complete

    def complete(*args, **kwargs):
        complete_calls.append(args)
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(client, "complete", complete)
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: ProcessResult(0, raw, ""))
    monkeypatch.setattr(reason, "run_worker_process", lambda *_args, **_kwargs: ProcessResult(0, raw, ""))
    backend = LocalBackend(config.local)
    export = client.export_project(project.project.id)
    if action == "fact":
        intent = next(item for item in project.intents if item.to is None)
        assert client.heartbeat(project.project.id, intent.id, config.workers[0].name).ok
        outcome = explore.run_explore_task(config, client, backend, project, export, intent, config.workers[0], TaskCancellation())
    else:
        assert client.claim_reason(project.project.id, config.workers[0].name, "CP3 R1 regression").ok
        outcome = reason.run_reason_task(config, client, backend, project, export, config.workers[0], TaskCancellation())
    updated = client.get_project(project.project.id)
    assert outcome in {"failed", "rejected"}
    assert updated.facts == project.facts
    assert [(item.id, item.description, item.to) for item in updated.intents] == [(item.id, item.description, item.to) for item in project.intents]
    assert updated.project.audit.audit_revision == project.project.audit.audit_revision
    assert updated.project.status == "active" and not complete_calls
    receipts = list(Path(result["workspace"], "research").glob("*.json"))
    assert receipts and all(json.loads(path.read_text())["process"]["stdout"] == raw for path in receipts)


@pytest.mark.parametrize("ignored_dir", ["node_modules", ".venv"])
def test_cp3_r2_unsnapshotted_seed_is_never_a_supported_premise(api, tmp_path, monkeypatch, ignored_dir):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    path = f"{ignored_dir}/pkg/entry.js"
    evidence = repo / path
    evidence.parent.mkdir(parents=True)
    evidence.write_text("anonymous = true;\n")
    audit_plan = plan()
    audit_plan["policy"]["scope"] = [path]
    install_cli(tmp_path, monkeypatch, [audit_plan, {"approved": True, "issues": []}, review(path=path)])
    result = create(config, client, repo)
    project = client.get_project(result["project_id"])
    seed_id = project.project.audit.seed_ids["entry"]
    seed = next(fact for fact in project.facts if fact.id == seed_id)
    assert path not in project.project.audit.baseline["files"]
    assert seed.audit.review_status == "unverified"
    assert "not snapshotted" in seed.audit.review_notes and seed_id not in supported_fact_ids(project)
    assert any("独立验证前提" in item.description for item in project.intents)
    evidence.write_text("anonymous = false;\n")
    assert research_workspace(config, client, LocalBackend(config.local), project) == result["workspace"]
    with pytest.raises(ValueError, match="unsupported"):
        reason_result(project, {"accepted": True, "data": {"complete": {"from": [seed_id], "description": "old premise"}}}, open_intents_empty=False, max_intents=2)


@pytest.mark.parametrize("failure", ["delete_all", "unreadable", "changed", "missing_baseline"])
def test_cp3_r3_invalid_baseline_stops_server_before_agent(api, tmp_path, monkeypatch, failure):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    calls_path = install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    project = client.get_project(create(config, client, repo)["project_id"])
    assert project.project.audit.seed_ids["entry"] in supported_fact_ids(project)
    intent = next(item for item in project.intents if item.to is None)
    export = client.export_project(project.project.id)
    assert client.heartbeat(project.project.id, intent.id, config.workers[0].name).ok
    if failure == "delete_all":
        (repo / "app.py").unlink()
    elif failure == "unreadable":
        original_read = Path.read_bytes

        def read_bytes(path):
            if path == repo / "app.py":
                raise PermissionError("fixture source read denied")
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
    elif failure == "changed":
        (repo / "app.py").write_text("def private():\n    return False\n")
    else:
        project.project.audit.baseline.pop("files")
    process_calls = []

    def execute(*_args, **_kwargs):
        process_calls.append(True)
        return ProcessResult(0, '{"accepted": false}', "")

    monkeypatch.setattr(explore, "_run_process", execute)
    outcome = explore.run_explore_task(config, client, LocalBackend(config.local), project, export, intent, config.workers[0], TaskCancellation())
    updated = client.get_project(project.project.id)
    assert outcome == "failed"
    assert updated.project.status == "stopped" and not process_calls
    assert updated.facts == project.facts
    assert [(item.id, item.to) for item in updated.intents] == [(item.id, item.to) for item in project.intents]
    assert len(calls_path.read_text().splitlines()) == 3


def test_cp3_r3_stop_failure_is_reported_and_does_not_start_agent(api, tmp_path, monkeypatch, caplog):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    project = client.get_project(create(config, client, repo)["project_id"])
    intent = next(item for item in project.intents if item.to is None)
    (repo / "app.py").write_text("changed\n")
    monkeypatch.setattr(client, "set_project_status", lambda *_args: ApiResult(503, text="fixture stop unavailable"))
    process_calls = []
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: process_calls.append(True))
    outcome = explore.run_explore_task(config, client, LocalBackend(config.local), project, client.export_project(project.project.id), intent, config.workers[0], TaskCancellation())
    assert outcome == "failed" and not process_calls
    assert client.get_project(project.project.id).project.status == "active"
    assert "stop failed" in caplog.text and "503" in caplog.text and project.project.id in caplog.text


@pytest.mark.parametrize("failure", ["receipt", "cancelled", "interrupt", "response_lost", "cancelled_during_receipt", "interrupted_during_receipt"])
def test_cp3_r4_postactivation_failure_stops_identifiable_project(api, tmp_path, monkeypatch, failure):
    client, http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    cancellation = TaskCancellation()
    activated = []
    original_start, original_write = client.start_audit, Path.write_text

    def start(project_id):
        response = original_start(project_id)
        assert response.ok and client.get_project(project_id).project.status == "active"
        activated.append(project_id)
        if failure == "cancelled":
            cancellation.cancel("fixture cancelled after start")
        elif failure == "interrupt":
            raise KeyboardInterrupt("fixture interrupted after start")
        elif failure == "response_lost":
            raise OSError("fixture activation response lost")
        return response

    def write_text(path, *args, **kwargs):
        if failure == "receipt" and path.name == "project.json":
            raise OSError("fixture receipt disk full")
        result = original_write(path, *args, **kwargs)
        if path.name == "project.json":
            if failure == "cancelled_during_receipt":
                cancellation.cancel("fixture cancelled during receipt")
            elif failure == "interrupted_during_receipt":
                raise KeyboardInterrupt("fixture interrupted during receipt")
        return result

    monkeypatch.setattr(client, "start_audit", start)
    monkeypatch.setattr(Path, "write_text", write_text)
    with pytest.raises(ValueError, match="project .*stopped") as error:
        create(config, client, repo, cancellation=cancellation)
    assert len(activated) == 1 and activated[0] in str(error.value)
    assert client.get_project(activated[0]).project.status == "stopped"
    assert all(item["status"] == "stopped" for item in http.get("/projects").json())
    receipts = list((tmp_path / "work/controller").rglob("project.json"))
    if failure in {"cancelled_during_receipt", "interrupted_during_receipt"}:
        assert len(receipts) == 1 and json.loads(receipts[0].read_text())["project_id"] == activated[0]
    else:
        assert not receipts


def test_cp3_r4_compensation_failure_reports_uncertain_activation(api, tmp_path, monkeypatch):
    client, _http = api
    repo, config = repository(tmp_path), configuration(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    activated = []
    original_start = client.start_audit

    def start(project_id):
        response = original_start(project_id)
        assert response.ok
        activated.append(project_id)
        raise OSError("fixture activation response lost")

    monkeypatch.setattr(client, "start_audit", start)
    monkeypatch.setattr(client, "set_project_status", lambda *_args: ApiResult(503, text="fixture stop unavailable"))
    with pytest.raises(RuntimeError, match="state uncertain") as error:
        create(config, client, repo)
    assert len(activated) == 1 and activated[0] in str(error.value)
    assert "503" in str(error.value) and "fixture activation response lost" in str(error.value)
    assert client.get_project(activated[0]).project.status == "active"
