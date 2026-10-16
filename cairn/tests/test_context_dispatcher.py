"""Audit roles get their scope and contract without changing stored research."""
import json

import pytest
import yaml

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import bootstrap, explore
from cairn.dispatcher.tasks.audit import research_prompt
from conftest import FakeDriver, FakeLease
from test_audit_workflow import configuration
from test_research_reason_memory import action, harness


def audit_context(prompt):
    return json.loads(prompt.split("## 固定审计上下文\n", 1)[1])


@pytest.mark.parametrize("name", ["reason.md", "explore.md", "explore_conclude.md", "bootstrap.md", "bootstrap_conclude.md"])
def test_every_research_role_gets_original_contract_once(harness, tmp_path, name):
    project, _, _, _, _, _ = harness
    config = configuration(tmp_path)
    project.facts[1].description = "ORIGINAL_GOAL_KEEP_EVERY_CONDITION"
    project.project.audit.policy = {"effect": "POLICY_EFFECT_IS_NOT_THE_GOAL", "sha256": "RESEARCH_SEMANTIC_VALUE"}
    project.project.audit.user_goal = "USER_REQUEST_ADDITIONAL_LIMIT"
    before = project.model_dump(mode="json", by_alias=True)
    replacements = {"graph_yaml": "/graph-index.md and /graph.yaml", "intent_id": "i001",
                    "intent_description": "current question", "intent_sources": '["f001"]',
                    "fact_ids": '["f001"]', "open_intents": "see navigation", "max_intents": "2",
                    "origin": "starting point", "goal": project.facts[1].description, "hints": "see navigation"}
    prompt = research_prompt(config, project, name, replacements)
    context = audit_context(prompt)
    assert context["fixed_contract"] == {
        "goal": "ORIGINAL_GOAL_KEEP_EVERY_CONDITION",
        "policy": project.project.audit.policy,
        "user_goal": "USER_REQUEST_ADDITIONAL_LIMIT",
    }
    for marker in ("ORIGINAL_GOAL_KEEP_EVERY_CONDITION", "POLICY_EFFECT_IS_NOT_THE_GOAL", "USER_REQUEST_ADDITIONAL_LIMIT"):
        assert prompt.count(marker) == 1
    assert "unverified_or_refuted" not in context
    assert "verified_fact_ids" not in context
    assert ("candidate_fact_ids" in context) == (name == "reason.md")
    assert set(context["baseline"]) == {"git_commit"}
    assert project.model_dump(mode="json", by_alias=True) == before


def test_reason_delivers_open_intent_only_through_navigation(harness):
    project, _, workspace, driver, _, run = harness
    project.intents[0].description = "OPEN_QUESTION_HEAD\n\n" + "LONG_DETAIL_NOT_IN_INITIAL_PROMPT" * 100
    project.hints[0].content = "HINT_HEAD\n\n" + "HINT_DETAIL_NOT_IN_INITIAL_PROMPT" * 100
    assert run(action()) == "success"
    prompt = driver.execute_prompts[-1]
    assert "OPEN_QUESTION_HEAD" not in prompt and "LONG_DETAIL_NOT_IN_INITIAL_PROMPT" not in prompt
    assert "HINT_DETAIL_NOT_IN_INITIAL_PROMPT" not in prompt
    navigation = next(workspace.parent.parent.glob("prompts/*/graph-index.md"))
    index = navigation.read_text()
    assert "OPEN_QUESTION_HEAD" in index and "LONG_DETAIL_NOT_IN_INITIAL_PROMPT" not in index
    assert "HINT_HEAD" in index and "HINT_DETAIL_NOT_IN_INITIAL_PROMPT" not in index
    full = yaml.safe_load(navigation.with_name("graph.yaml").read_text())
    assert full["intents"][0]["description"] == project.intents[0].description
    assert full["hints"][0]["content"] == project.hints[0].content


@pytest.mark.parametrize("module", [explore, bootstrap])
def test_local_roles_keep_sources_and_session_without_second_graph(harness, tmp_path, monkeypatch, module):
    project, client, workspace, _, _, _ = harness
    config = configuration(tmp_path)
    intent = project.intents[0]
    intent.description = "CURRENT_INTENT_FULL_TEXT"
    project.facts[2].description = "SOURCE_ONLY_ON_DEMAND\n\ncomplete conditional evidence"
    project.hints[0].content = "HINT_ONLY_ON_DEMAND"
    driver, lease = FakeDriver(), FakeLease()
    monkeypatch.setattr(module, "get_driver", lambda *_a: driver)
    monkeypatch.setattr(module.HeartbeatLease, "for_intent", lambda *_a: lease)
    results = iter([ProcessResult(0, "continue in the same session", ""),
                    ProcessResult(0, '{"accepted":false,"reason":"no new observation"}', "")])
    monkeypatch.setattr(module, "_run_process" if module is explore else "run_worker_process", lambda *_a, **_k: next(results))
    backend = LocalBackend(config.local)
    if module is explore:
        outcome = module.run_explore_task(config, client, backend, project,
            yaml.safe_dump(project.model_dump(mode="json", by_alias=True), allow_unicode=True),
            intent, config.workers[0], TaskCancellation())
    else:
        outcome = module.run_bootstrap_task(config, client, backend, project, intent, config.workers[0], TaskCancellation())
    assert outcome == "rejected"
    assert len(driver.execute_prompts) == len(driver.conclude_prompts) == 1
    first, conclude = driver.execute_prompts[0], driver.conclude_prompts[0]
    assert "CURRENT_INTENT_FULL_TEXT" in first and "CURRENT_INTENT_FULL_TEXT" in conclude
    assert '["f001"]' in first and '["f001"]' in conclude
    assert "SOURCE_ONLY_ON_DEMAND" not in first and "HINT_ONLY_ON_DEMAND" not in first
    assert "Read the global navigation once" not in first
    assert "Current Intent source IDs and Hint entries first" in first
    assert audit_context(first)["fixed_contract"] == audit_context(conclude)["fixed_contract"]
    snapshots = list(workspace.parent.parent.glob("prompts/*/graph.yaml"))
    assert len(snapshots) == 1
    assert "SOURCE_ONLY_ON_DEMAND" in snapshots[0].read_text()
    assert not client.concluded and not client.completed
