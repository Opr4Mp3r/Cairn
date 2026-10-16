from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import yaml

from cairn.audit_evidence import repository_baseline
from cairn.audit_inputs import fact_view, graph_index
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.heartbeat import HeartbeatFailure
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import common, reason
from cairn.dispatcher.tasks.audit import reason_result
from cairn.dispatcher.tasks.reason_memory import load_memory
from cairn.server.models import AuditBudget, AuditMetadata, ProjectReason
from conftest import FakeClient, FakeDriver, FakeLease, make_intent, make_project
from test_audit_workflow import api, configuration, create, install_cli, plan, repository, review


@pytest.fixture
def harness(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    workspace = tmp_path / "work" / "proj_001"
    workspace.mkdir(parents=True)
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "app.py").write_text("# synthetic source\n")
    (workspace / "repo").symlink_to(repo, target_is_directory=True)
    project = make_project(intents=[make_intent()])
    project.project.mode = "audit"
    project.project.runtime_generation = 1
    project.project.audit = AuditMetadata(project_id=project.project.id, repo_path=str(repo),
        workspace_root=str(workspace.parent), workspace_path=str(workspace),
        baseline=repository_baseline(repo, workspace.parent), user_goal="inspect fixture",
        policy={"deployment": "synthetic"}, policy_digest="fixed-policy", budget=AuditBudget(), seed_ids={})
    (workspace / "input.json").write_text(json.dumps({"project_id": project.project.id,
        "repo_path": str(repo), "policy_digest": "fixed-policy", "materials": {}}))
    client, backend, driver, lease = FakeClient(project), LocalBackend(config.local), FakeDriver(), FakeLease()
    monkeypatch.setattr(reason, "get_driver", lambda *_a: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *_a: lease)
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))
    calls = 0

    def run(payload=None, *, process=None, during=None, cancellation=None, started_at=None):
        nonlocal calls
        calls += 1
        project.project.reason = ProjectReason(worker=config.workers[0].name, trigger="fixture",
            started_at=started_at or (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=calls)).isoformat(),
            last_heartbeat_at="2026-01-01T00:00:00Z")
        snapshot = project.model_copy(deep=True)

        def execute(*_a, **_k):
            if during:
                during()
            return process or ProcessResult(0, json.dumps(payload), "")

        monkeypatch.setattr(reason, "run_worker_process", execute)
        return reason.run_reason_task(config, client, backend, snapshot,
            yaml.safe_dump(snapshot.model_dump(mode="json", by_alias=True), allow_unicode=True),
            config.workers[0], cancellation or TaskCancellation())

    return project, client, workspace, driver, lease, run


def action(notes=None):
    data = {"intents": [{"from": ["f001"], "description": "check a specific condition"}]}
    if notes is not None:
        data["planning_notes"] = notes
    return {"accepted": True, "data": data}


def test_navigation_points_to_full_constraints_without_expanding_them(harness):
    project, _, workspace, driver, _, run = harness
    condition = "必须保持的完整条件" * 200
    graph = {"facts": [{"id": "f001", "description": "x" * 500 + "\n\n原始细节",
                        "audit": {"conditions": [condition], "premise_ids": ["f002"], "review_status": "unverified"}}],
             "intents": [{"id": "i001", "from": ["f001"], "description": "open", "to": None},
                         {"id": "i002", "to": "f002", "description": "closed"}],
             "hints": [{"id": "h001", "content": "不能忽略的纠偏"}]}
    text = yaml.safe_dump(graph, allow_unicode=True, sort_keys=False)
    index = graph_index(text)
    assert condition not in index and "节选" in index
    assert "f002" in index and "unverified" in index
    open_section = index.split("## Open Intents", 1)[1].split("## 已完成 Intent 定位", 1)[0]
    assert "i001" in open_section and "i002" not in open_section
    assert "不能忽略的纠偏" in index and "closed" not in index
    assert run(action()) == "success"
    context = json.loads(driver.execute_prompts[-1].split("## 固定审计上下文\n", 1)[1])
    assert context["reason_memory"]["note"]
    navigation = list(workspace.parent.parent.glob("prompts/*/graph-index.md"))
    assert len(navigation) == 1 and str(navigation[0]) in driver.execute_prompts[-1]
    full = yaml.safe_load(navigation[0].with_name("graph.yaml").read_text())
    full_facts = {fact["id"]: fact for fact in full["facts"]}
    assert full_facts["f001"] == fact_view(project.facts[2].model_dump(mode="json"))
    assert full_facts["goal"]["description"] != project.facts[1].description


def test_successful_notes_survive_old_output_and_generation_change(harness):
    project, client, workspace, driver, _, run = harness
    assert run(action("暂搁 A；f001 出现新条件后再研究。")) == "success"
    first = load_memory(project, str(workspace))["previous_plan"]
    assert Path(first["receipt_path"]).is_file()
    assert set(first) == {"planning_notes", "reason_started_at", "receipt_path", "memory_path"}
    original = json.loads(Path(first["memory_path"]).read_text())
    assert original["audit_revision"] == 1 and original["policy_digest"] == "fixed-policy"
    assert run(action()) == "success"
    assert load_memory(project, str(workspace))["previous_plan"] == first
    assert run(action("  \n ")) == "success"
    assert load_memory(project, str(workspace))["previous_plan"] == first
    project.project.runtime_generation = 2
    assert run(action()) == "success"
    context = json.loads(driver.execute_prompts[-1].split("## 固定审计上下文\n", 1)[1])
    assert context["reason_memory"]["previous_plan"] == first
    assert len(client.created_intents) == 4
    assert project.project.audit.audit_revision == project.project.audit.reason_revision == 1
    assert len(project.facts) == 3 and len(project.hints) == 1


@pytest.mark.parametrize("process, expected", [
    (ProcessResult(124, "partial output must not become a plan", "", timed_out=True), "execution_timeout"),
    (ProcessResult(2, "ignored", "missing tool"), "command_failed"),
    (ProcessResult(0, "not JSON", ""), "invalid_output"),
])
def test_failure_is_available_next_time_without_erasing_plan(harness, process, expected):
    project, _, workspace, driver, _, run = harness
    assert run(action("previous successful plan")) == "success"
    assert run(process=process) == "failed"
    memory = load_memory(project, str(workspace))
    assert memory["previous_plan"]["planning_notes"] == "previous successful plan"
    assert expected in memory["last_failure"]["error"]
    assert "planning_notes" not in memory["last_failure"]
    assert Path(memory["last_failure"]["receipt_path"]).is_file()
    assert run(action()) == "success"
    assert expected in driver.execute_prompts[-1]
    assert "partial output must not become a plan" not in driver.execute_prompts[-1]


@pytest.mark.parametrize("late", ["cancelled", "generation", "new_reason", "new_lease", "inactive", "lease_lost"])
def test_late_attempt_cannot_replace_previous_plan(harness, late):
    project, _, workspace, _, lease, run = harness
    assert run(action("keep this plan")) == "success"
    cancellation = TaskCancellation()

    def during():
        if late == "cancelled":
            cancellation.cancel("paused")
        elif late == "generation":
            project.project.runtime_generation += 1
        elif late == "new_reason":
            project.project.reason.started_at = "2026-01-02T00:00:00Z"
        elif late == "new_lease":
            project.project.reason.lease_id = "new-claim-in-the-same-second"
        elif late == "inactive":
            project.project.status = "stopped"
        else:
            lease.failure = HeartbeatFailure(409, "lease lost")

    run(action("late plan"), during=during, cancellation=cancellation)
    assert load_memory(project, str(workspace))["previous_plan"]["planning_notes"] == "keep this plan"
    assert len(list((workspace / "research").rglob("reason-memory-*.json"))) == 1


def test_failed_action_does_not_save_successful_plan(harness):
    project, client, workspace, _, _, run = harness
    assert run(action("keep this plan")) == "success"
    client.create_intent = lambda *_a, **_kw: ApiResult(500, text="write failed")
    assert run(action("uncommitted plan")) == "failed"
    memory = load_memory(project, str(workspace))
    assert memory["previous_plan"]["planning_notes"] == "keep this plan"
    assert "intent_write_failed" in memory["last_failure"]["error"]


@pytest.mark.parametrize("status", [409, 500])
def test_partial_intent_write_keeps_plan_and_passes_failure_to_next_round(harness, status):
    project, client, workspace, driver, _, run = harness
    assert run(action("keep the completed plan")) == "success"
    replies = iter([ApiResult(201, {}), ApiResult(status, text="second Intent failed")])
    client.create_intent = lambda *_a, **_kw: next(replies)
    payload = action("both requested directions were queued")
    payload["data"]["intents"].append({"from": ["f001"], "description": "missing direction B"})
    assert run(payload) == "success"  # Keep the existing partial-success dispatch outcome.
    memory = load_memory(project, str(workspace))
    assert memory["previous_plan"]["planning_notes"] == "keep the completed plan"
    assert f"HTTP {status}" in memory["last_failure"]["error"]
    assert "created 1/2" in memory["last_failure"]["error"]
    assert "missing direction B" in memory["last_failure"]["error"]
    assert run({"accepted": True, "data": {}}) == "success"
    context = json.loads(driver.execute_prompts[-1].split("## 固定审计上下文\n", 1)[1])["reason_memory"]
    assert context["last_failure"] == memory["last_failure"]
    assert context["previous_plan"] == memory["previous_plan"]


@pytest.mark.parametrize("stage", ["read", "serialize"])
@pytest.mark.parametrize("kind", ["cancelled", "lease_lost"])
def test_memory_rechecks_invalidation_after_blocking_read_and_before_write(harness, monkeypatch, stage, kind):
    project, client, workspace, _, lease, run = harness
    assert run(action("keep valid previous plan")) == "success"
    cancellation = TaskCancellation()

    def invalidate():
        if kind == "cancelled":
            cancellation.cancel("paused while saving memory")
        else:
            lease.failure = HeartbeatFailure(None, "lost while saving memory")

    if stage == "read":
        def get_project(_project_id):
            invalidate()
            return project
        monkeypatch.setattr(client, "get_project", get_project)
    else:
        original = json.dumps

        def dumps(value, *args, **kwargs):
            content = original(value, *args, **kwargs)
            if isinstance(value, dict) and "attempt_started_ns" in value:
                invalidate()
            return content
        monkeypatch.setattr(json, "dumps", dumps)
    run(action("late plan"), cancellation=cancellation)
    assert load_memory(project, str(workspace))["previous_plan"]["planning_notes"] == "keep valid previous plan"
    assert len(list((workspace / "research").rglob("reason-memory-*.json"))) == 1


def test_same_second_failure_order_and_old_memory_compatibility(harness):
    project, _, workspace, _, _, run = harness
    stamp = "2026-01-01T00:00:01+00:00"
    assert run(action("old memory remains usable"), started_at=stamp) == "success"
    previous = load_memory(project, str(workspace))["previous_plan"]
    path = Path(previous["memory_path"])
    old = json.loads(path.read_text())
    old.pop("attempt_started_ns")
    path.write_text(json.dumps(old))
    for error in ["first failure", "newest failure"]:
        assert run(process=ProcessResult(2, "", error), started_at=stamp) == "failed"
    memory = load_memory(project, str(workspace))
    assert memory["previous_plan"]["planning_notes"] == "old memory remains usable"
    assert "newest failure" in memory["last_failure"]["error"]


def test_notes_only_keep_empty_queue_contract_and_require_string(harness):
    project, _, _, _, _, _ = harness
    with pytest.raises(ValueError, match="intents is required"):
        reason_result(project, {"accepted": True, "data": {"planning_notes": "nothing to do"}},
                      open_intents_empty=True, max_intents=3)
    with pytest.raises(ValueError, match="planning_notes must be a string"):
        reason_result(project, action(["not a string"]), open_intents_empty=False, max_intents=3)
    assert reason_result(project, {"accepted": True, "data": {"planning_notes": "waiting"}},
                         open_intents_empty=False, max_intents=3) == ("noop", None)


def test_legal_noop_notes_do_not_change_graph(harness):
    project, client, workspace, _, _, run = harness
    assert run({"accepted": True, "data": {"planning_notes": "wait for existing Intent"}}) == "success"
    assert not client.created_intents and not client.completed
    assert load_memory(project, str(workspace))["previous_plan"]["planning_notes"] == "wait for existing Intent"
    assert project.project.audit.reason_revision == 1


@pytest.mark.parametrize("rounds", [2, 20])
def test_actual_reason_claim_releases_and_reuses_notes_without_graph_changes(api, tmp_path, monkeypatch, rounds):
    config, repo = configuration(tmp_path), repository(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {"approved": True, "issues": []}, review()])
    client, _ = api
    result = create(config, client, repo)
    project_id = result["project_id"]
    initial = client.get_project(project_id)
    from cairn.server.routers import projects
    fixed_time = projects.utcnow()
    monkeypatch.setattr(projects, "utcnow", lambda: fixed_time)
    backend, driver = LocalBackend(config.local), FakeDriver()
    monkeypatch.setattr(reason, "get_driver", lambda *_a: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *_a: FakeLease())
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))
    outputs = iter([{"accepted": True, "data": {} if rounds == 2 and index == 1
                     else {"planning_notes": f"plan-{index}"}} for index in range(rounds)])
    monkeypatch.setattr(reason, "run_worker_process", lambda *_a, **_k: ProcessResult(0, json.dumps(next(outputs)), ""))
    leases = set()
    for _ in range(rounds):
        assert client.claim_reason(project_id, config.workers[0].name, "test memory").ok
        claimed = client.get_project(project_id)
        assert claimed.project.reason.lease_id
        leases.add(claimed.project.reason.lease_id)
        assert claimed.project.reason.started_at == fixed_time
        assert reason.run_reason_task(config, client, backend, claimed, client.export_project(project_id),
                                      config.workers[0], TaskCancellation()) == "success"
    current = client.get_project(project_id)
    assert current.project.reason is None
    assert current.facts == initial.facts and current.intents == initial.intents and current.hints == initial.hints
    assert current.project.audit.audit_revision == initial.project.audit.audit_revision
    assert current.project.audit.reason_revision == initial.project.audit.reason_revision
    assert current.project.status == "active" and not current.project.audit.completion_pending
    assert len(leases) == rounds
    expected = "plan-0" if rounds == 2 else "plan-19"
    assert load_memory(current, result["workspace"])["previous_plan"]["planning_notes"] == expected
    assert ("plan-0" if rounds == 2 else "plan-18") in driver.execute_prompts[-1]
