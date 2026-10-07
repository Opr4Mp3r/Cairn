import json
import os
from pathlib import Path
import sys

from click.testing import CliRunner
import pytest

from cairn.cli import main
from cairn.controller.builder import run_builder
from test_kritt_probe import probe


@pytest.fixture
def prepared(probe, monkeypatch):
    config, config_path, view_path, output = probe
    config.controller.timeouts.build = 20
    config_path.write_text(json.dumps(config.model_dump(mode="json")))
    binary = config_path.parent / "bin" / "claude"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
context = json.loads(sys.argv[-1].rsplit("\n\n", 1)[1])
assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
mode = os.environ.get("BUILDER_TEST_MODE", "")
last = context.get("last_tool_result") or {}
if mode == "invalid":
    print("not-json")
    raise SystemExit
if "previous_response" in context:
    print(json.dumps({"action": "query", "query": {"action": "search", "kind": "finding",
        "route": "candidates", "effect": "rce", "limit": 1}}))
    raise SystemExit
if mode == "repair" and last.get("action") == "describe":
    print("not-json")
    raise SystemExit
if mode == "ask" and len(context["messages"]) == 1:
    print(json.dumps({"action": "ask", "questions": ["部署状态是什么？"]}))
    raise SystemExit
if last.get("action") == "describe":
    result = {"action": "query", "query": {"action": "search", "kind": "finding",
        "route": "candidates", "effect": "rce", "limit": 1}}
elif last["action"] == "search":
    result = {"action": "query", "query": {"action": "get", "ref": last["records"][0]["ref"]}}
else:
    result = {"action": "finish", "title": "匿名远程代码执行", "origin": "用户要求研究默认安装后的匿名入口",
              "goal": "研究已安装默认配置下的匿名远程代码执行",
              "seeds": [{"key": "entry", "description": "从已读取的入口继续核对匿名可达性"}],
              "intents": [{"from": ["entry"], "description": "沿入口读到危险操作"}],
              "hints": ["保留部署和身份条件"]}
print(json.dumps(result))
''')
    binary.chmod(0o755)
    return config, config_path, view_path, output


def test_plan_is_saved_from_queries_without_creating_a_project(prepared, monkeypatch):
    config, _, view, folder = prepared
    monkeypatch.setattr("cairn.dispatcher.protocol.client.CairnClient.create_audit",
                        lambda *args, **kwargs: pytest.fail("builder must not create a project"))
    result = run_builder(config, target_name="test", user_goal="anonymous RCE", run_dir=folder, view_path=view)
    plan = json.loads((folder / "plan.json").read_text())
    materials = json.loads((folder / "selected-materials.json").read_text())
    assert result["status"] == "plan_ready"
    assert plan["title"] and plan["goal"] and plan["seeds"][0]["key"] == "entry"
    assert plan["intents"][0]["from"] == ["entry"] and "policy" not in plan
    assert materials and materials[0]["ref"]["table"]
    assert not (folder / "error.json").exists()
    assert not list(folder.glob("**/project.json"))


def test_question_is_answered_in_the_same_process(prepared, monkeypatch):
    monkeypatch.setenv("BUILDER_TEST_MODE", "ask")
    config, _, view, folder = prepared
    seen = []
    result = run_builder(config, target_name="test", user_goal="anonymous RCE", run_dir=folder, view_path=view,
                         ask=lambda questions: seen.append(questions) or "已安装，攻击者未登录")
    assert seen == [["部署状态是什么？"]] and result["status"] == "plan_ready"
    assert json.loads((folder / "plan.json").read_text())["seeds"]


def test_paginated_materials_are_available_to_the_next_builder_call(prepared):
    config, _, view, folder = prepared
    config.controller.kritt.response_rows = 1
    result = run_builder(config, target_name="test", user_goal="anonymous RCE", run_dir=folder, view_path=view)
    context = json.loads((folder / "call-003" / "input.json").read_text())
    materials = json.loads(Path(context["materials_path"]).read_text())
    assert result["status"] == "plan_ready"
    assert len(context["last_tool_result"]["records"]) == 1
    assert len(materials) > 1
    assert any(record["ref"]["table"] == "workflows.vulnerabilities" for record in materials)


def test_unanswered_question_ends_the_run(prepared, monkeypatch):
    monkeypatch.setenv("BUILDER_TEST_MODE", "ask")
    config, _, view, folder = prepared
    with pytest.raises(ValueError, match="部署状态是什么"):
        run_builder(config, target_name="test", user_goal="anonymous RCE", run_dir=folder, view_path=view)
    error = json.loads((folder / "error.json").read_text())
    assert error["status"] == "error" and not (folder / "plan.json").exists()


def test_second_invalid_output_ends_the_run(prepared, monkeypatch):
    monkeypatch.setenv("BUILDER_TEST_MODE", "invalid")
    config, _, view, folder = prepared
    with pytest.raises(RuntimeError, match="invalid_output"):
        run_builder(config, target_name="test", user_goal="anonymous RCE", run_dir=folder, view_path=view)
    error = json.loads((folder / "error.json").read_text())
    assert error["status"] == "error" and not (folder / "plan.json").exists()
    assert (folder / "call-001-format-repair" / "result.json").exists()


def test_invalid_output_ends_after_one_repair(prepared, monkeypatch):
    monkeypatch.setenv("BUILDER_TEST_MODE", "repair")
    config, _, view, folder = prepared
    result = run_builder(config, target_name="test", user_goal="anonymous RCE", run_dir=folder, view_path=view)
    assert result["status"] == "plan_ready"
    assert (folder / "call-001-format-repair" / "result.json").exists()


def test_cli_uses_the_documented_command(prepared, monkeypatch):
    config, config_path, view, folder = prepared
    seen = []

    def research(config, ready, **kwargs):
        seen.append(ready)
        return {"status": "completed", "project_id": "proj_001", "run_dir": ready["run_dir"],
                "server": "http://127.0.0.1:8000"}

    monkeypatch.setattr("cairn.controller.research.run_research", research)
    result = CliRunner().invoke(main, ["builder", "--config", str(config_path), "--target", "test",
                                        "--goal", "anonymous RCE", "--run-dir", str(folder), "--view", str(view)])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["status"] == "completed" and Path(seen[0]["plan"]).is_file()
    rejected = CliRunner().invoke(main, ["builder", "--config", str(config_path), "--target", "test",
                                          "--goal", "anonymous RCE", "--run-dir", str(folder) + "-2",
                                          "--prepare-only"])
    assert rejected.exit_code != 0
