import json
from pathlib import Path
import shlex
import subprocess
import sys

from click.testing import CliRunner
import pytest

from cairn.cli import main
from cairn.controller.audit import AuditPlan
from cairn.controller.builder import prepare_builder, run_builder
from cairn.controller.models import AgentCallResult
from cairn.dispatcher.runtime.process import ProcessResult
from test_kritt_probe import probe


@pytest.fixture
def prepared(probe):
    config, config_path, view_path, output = probe
    config.controller.timeouts.build = 20
    config_path.write_text(json.dumps(config.model_dump(mode="json")))
    binary = config_path.parent / "bin" / "claude"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, shlex, subprocess, sys
context = json.loads(sys.argv[-1].rsplit("\n\n", 1)[1])
assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
args = sys.argv
session = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("-r") + 1]
state_path = pathlib.Path(f"session-{session}.json")
if "--session-id" in args:
    assert not state_path.exists()
    state = {"context": context}
else:
    assert state_path.exists(), "resume must use an existing session"
    state = json.loads(state_path.read_text())
with pathlib.Path("fake-harness-calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"session": session, "resumed": "-r" in args}) + "\n")
mode = os.environ.get("BUILDER_TEST_MODE", "")
if mode == "invalid":
    state_path.write_text(json.dumps(state))
    print("not-json")
    raise SystemExit
if "previous_response" in context:
    print(json.dumps(state["finish"]))
    raise SystemExit
if mode == "ask" and "reply" not in context:
    state_path.write_text(json.dumps(state))
    print(json.dumps({"action": "ask", "questions": ["部署状态是什么？"]}))
    raise SystemExit
initial = state["context"]
command = shlex.split(initial["kritt_query_command"])
def query(request):
    result = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)
search = query({"action": "search", "kind": "finding", "effect": "rce", "fields": ["summary", "file_path"], "limit": 1})
found = query({"action": "get", "ref": search["records"][0]["ref"]})
materials = json.loads(pathlib.Path(found["materials_path"]).read_text())
assert len(materials) > 1
source = pathlib.Path(initial["repo_path"]) / "module/install/control.php"
assert "requireLogin" in source.read_text()
result = {"action": "finish", "title": "安装入口静态研究", "origin": "所选仓库版本与用户静态阅读要求",
          "goal": "梳理入口的鉴权条件并保留未知", "policy": {
              "effect": "静态源码行为及必要条件", "deployment": "用户指定的已安装状态",
              "attacker_capabilities": ["无账号"], "scope": ["安装入口源码"], "cvss": {},
              "evidence_requirements": ["源码路径及行号"], "threat_model": [
                  {"claim": "入口存在权限条件", "kind": "inferred", "source": "module/install/control.php:2"},
                  {"claim": "官方接受范围未知", "kind": "unknown", "source": "未提供官方资料"}]},
          "seeds": [{"key": "entry", "description": "源码调用requireLogin，匿名可达性仍须追踪",
                     "audit": {"evidence": [{"path": "module/install/control.php", "start_line": 2}],
                               "conditions": ["部署状态待复核"], "review_status": "verified"}}],
          "intents": [{"from": ["entry"], "description": "独立核对上游守卫和匿名可达性"}],
          "hints": []}
state["finish"] = result
state_path.write_text(json.dumps(state))
print("not-json" if mode == "repair" else json.dumps(result))
''')
    binary.chmod(0o755)
    return config, config_path, view_path, output


def build(prepared, **kwargs):
    config, _, view, folder = prepared
    return run_builder(config, target_name="test", user_goal="读取已安装状态下的入口源码",
                       run_dir=folder, view_path=view, **kwargs)


def test_prepare_saves_verified_inputs_and_a_working_query_command_without_agent(probe, monkeypatch):
    config, _, view, folder = probe
    reference = folder.parent / "scope.txt"
    reference.write_text("仅阅读安装入口源码", encoding="utf-8")
    config.controller.kritt.targets["test"].references = [str(reference)]

    def forbidden(*args, **kwargs):
        pytest.fail("offline preparation must not call an Agent, backend, or database")

    for name in ("call_agent", "LocalBackend", "read_kritt_view"):
        monkeypatch.setattr(f"cairn.controller.builder.{name}", forbidden)
    result = prepare_builder(config, target_name="test", user_goal="  读取已安装状态下的入口源码  ",
                             run_dir=folder, view_path=view)
    assert result == json.loads((folder / "run.json").read_text())
    assert result["status"] == "prepared" and result["calls"] == 0 and result["builder_session"] is None
    assert result["queries"] == result["query_pages"] == 0
    assert json.loads(Path(result["requirements"]).read_text()) == {
        "user_goal": result["user_goal"], "messages": [{"role": "user", "content": result["user_goal"]}],
    }
    baseline = json.loads(Path(result["source_baseline"]).read_text())
    assert baseline["files"] and baseline["source_manifest"]["commit_sha"] == result["commit_sha"]
    assert Path(result["view"]).read_bytes() == view.read_bytes()
    references = json.loads(Path(result["references"]).read_text())
    assert Path(references[0]["path"]).read_text() == reference.read_text()
    assert json.loads(Path(result["plan_schema"]).read_text()) == AuditPlan.model_json_schema()
    assert json.loads(Path(result["materials"]).read_text()) == []
    assert json.loads((folder / "query-receipts.json").read_text())["queries"] == []
    assert not Path(result["plan"]).exists() and not list(folder.glob("call-*"))
    assert not (folder / "cairn.db").exists() and not (folder / "project.json").exists()
    query = subprocess.run(shlex.split(result["kritt_query_command"]), input='{"action":"describe"}',
                           capture_output=True, text=True, check=True)
    assert json.loads(query.stdout)["status"] == "success"


def test_plan_uses_native_queries_and_is_an_unverified_audit_plan(prepared, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr("cairn.dispatcher.protocol.client.CairnClient.create_audit",
                        lambda *args, **kwargs: pytest.fail("R-CP1 must not create a project"))
    result = build(prepared)
    folder = prepared[-1]
    plan = AuditPlan.model_validate(json.loads((folder / "plan.json").read_text()))
    assert result["status"] == "plan_ready" and result["calls"] == 1 and result["queries"] == 2
    assert plan.seeds[0].audit.review_status == "unverified" and plan.seeds[0].audit.conditions
    assert plan.policy.cvss == {} and plan.policy.threat_model[1]["kind"] == "unknown"
    assert json.loads((folder / "selected-materials.json").read_text())
    assert (folder / "source-baseline.json").exists() and (folder / "kritt-view.json").exists()
    assert not (folder / "error.json").exists() and not list(folder.glob("**/project.json"))
    assert "Builder：读取 Kritt 视图" in caplog.text
    assert "Builder：核验源码" in caplog.text
    assert "Builder：保存参考资料与需求" in caplog.text
    assert f"phase=builder session={result['builder_session']} timeout=" in caplog.text
    assert "status=success elapsed=" in caplog.text
    assert "Builder：方案已就绪" in caplog.text
    assert "已返回 ask" not in caplog.text


def test_answer_resumes_same_session_and_saves_requirements(prepared, monkeypatch, caplog):
    monkeypatch.setenv("BUILDER_TEST_MODE", "ask")
    caplog.set_level("INFO")
    seen = []

    def reply(questions):
        assert "Builder 已返回 ask：需要补充 1 个问题" in caplog.text
        assert "Builder：等待用户答复" in caplog.text
        seen.append(questions)
        return "已安装，攻击者未登录"

    result = build(prepared, ask=reply)
    calls = [json.loads(line) for line in (prepared[-1] / "fake-harness-calls.jsonl").read_text().splitlines()]
    assert seen == [["部署状态是什么？"]] and result["calls"] == 2
    assert calls[0]["session"] == calls[1]["session"] == result["builder_session"]
    assert not calls[0]["resumed"] and calls[1]["resumed"]
    assert json.loads((prepared[-1] / "requirements.json").read_text())["messages"][-1]["content"] == "已安装，攻击者未登录"
    assert f"Builder：已收到答复，继续 session={result['builder_session']}" in caplog.text


def test_timeout_preserves_session_in_error_receipt(prepared, monkeypatch):
    def timed_out(config, backend, **kwargs):
        return AgentCallResult(role="builder", worker="test-worker", phase=kwargs["phase"],
                               workspace=str(kwargs["workspace"]), session="timed-out-session",
                               prompt=kwargs["prompt"], status="timed_out", error="Agent 调用超时",
                               process=ProcessResult(-15, "", "", timed_out=True), elapsed_seconds=18)

    monkeypatch.setattr("cairn.controller.builder.call_agent", timed_out)
    with pytest.raises(RuntimeError, match="timed_out"):
        build(prepared)
    error = json.loads((prepared[-1] / "error.json").read_text())
    call = json.loads((prepared[-1] / "call-001" / "result.json").read_text())
    assert error["builder_session"] == call["session"] == "timed-out-session"
    assert error["calls"] == 1 and not (prepared[-1] / "call-001-format-repair").exists()


def test_paginated_get_is_delivered_inside_one_harness_call(prepared):
    result = build(prepared)
    folder = prepared[-1]
    saved = json.loads((folder / "query-receipts.json").read_text())
    assert result["calls"] == 1 and result["query_pages"] > result["queries"]
    assert len(saved["queries"]) == 2 and len(saved["ledger"]) > 1
    assert (folder / "call-001" / "result.json").exists() and not (folder / "call-002").exists()


def test_large_reference_and_baseline_stay_out_of_model_input(prepared, monkeypatch):
    config, _, _, folder = prepared
    source = folder.parent / "security.txt"
    source.write_text("REFERENCE_MARKER" + "x" * 1_100_000)
    config.controller.kritt.targets["test"].references = [str(source)]
    from cairn.controller import builder
    original = builder.verify_source
    def baseline(*args, **kwargs):
        value = original(*args, **kwargs)
        value["files"].update({f"file-{n}.php": "b" * 64 for n in range(20_000)})
        return value
    monkeypatch.setattr(builder, "verify_source", baseline)
    build(prepared)
    payload = (folder / "call-001" / "input.json").read_text()
    assert "REFERENCE_MARKER" not in payload and "file-19999.php" not in payload
    assert len(payload.encode()) < 30_000
    index = json.loads((folder / "references.json").read_text())
    assert Path(index[0]["path"]).read_text() == source.read_text()
    assert (folder / "source-baseline.json").stat().st_size > 1_000_000


def test_unanswered_question_preserves_query_and_error_files(prepared, monkeypatch):
    monkeypatch.setenv("BUILDER_TEST_MODE", "ask")
    with pytest.raises(ValueError, match="部署状态是什么"):
        build(prepared)
    error = json.loads((prepared[-1] / "error.json").read_text())
    assert error["status"] == "error" and not (prepared[-1] / "plan.json").exists()


def test_invalid_output_has_one_repair_in_the_same_session(prepared, monkeypatch):
    monkeypatch.setenv("BUILDER_TEST_MODE", "repair")
    result = build(prepared)
    folder = prepared[-1]
    first = json.loads((folder / "call-001" / "result.json").read_text())
    repair = json.loads((folder / "call-001-format-repair" / "result.json").read_text())
    assert first["session"] == repair["session"] == result["builder_session"]
    assert first["status"] == "invalid_output" and repair["status"] == "success"
    assert result["queries"] == 2  # Format repair does not restart retrieval.
    monkeypatch.setenv("BUILDER_TEST_MODE", "invalid")
    other = (*prepared[:3], folder.with_name("invalid-output"))
    with pytest.raises(RuntimeError, match="invalid_output"):
        build(other)
    assert (other[-1] / "call-001-format-repair" / "result.json").exists()
    assert not (other[-1] / "plan.json").exists()


def test_cli_hands_audit_plan_to_research_without_new_product_flags(prepared, monkeypatch):
    config, config_path, view, folder = prepared
    config.controller.research_prompt_group = "audit"
    config_path.write_text(config.model_dump_json())
    received = []
    def research(config, ready, **kwargs):
        AuditPlan.model_validate(json.loads(Path(ready["plan"]).read_text()))
        assert kwargs["ask"] and kwargs["cancellation"]
        received.append(ready)
        return {"status": "stopped", "run_dir": ready["run_dir"]}
    monkeypatch.setattr("cairn.controller.research.run_research", research)
    result = CliRunner().invoke(main, ["builder", "--config", str(config_path), "--target", "test",
                                     "--goal", "读取源码", "--run-dir", str(folder), "--view", str(view)])
    assert result.exit_code == 0 and len(received) == 1
    assert json.loads(result.stdout)["status"] == "stopped"
    assert "Builder：读取 Kritt 视图" in result.stderr
    assert "Agent 调用开始" in result.stderr and "Agent 调用结束" in result.stderr
    assert (folder / "plan.json").exists() and not (folder / "cairn.db").exists()
    rejected = CliRunner().invoke(main, ["builder", "--config", str(config_path), "--target", "test",
                                       "--goal", "读取源码", "--run-dir", str(folder) + "-2", "--prepare-only"])
    assert rejected.exit_code != 0


def test_cli_questions_and_prompt_use_stderr(prepared, monkeypatch):
    config, config_path, view, folder = prepared
    config.controller.research_prompt_group = "audit"
    config_path.write_text(config.model_dump_json())
    monkeypatch.setenv("BUILDER_TEST_MODE", "ask")

    def interactive_builder(*args, **kwargs):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        return run_builder(*args, **kwargs)

    monkeypatch.setattr("cairn.controller.builder.run_builder", interactive_builder)
    monkeypatch.setattr("cairn.controller.research.run_research", lambda *args, **kwargs: {"status": "stopped"})
    result = CliRunner().invoke(main, ["builder", "--config", str(config_path), "--target", "test",
                                     "--goal", "读取源码", "--run-dir", str(folder), "--view", str(view)],
                                input="已安装，攻击者未登录\n")
    assert result.exit_code == 0
    assert "部署状态是什么？" in result.stderr and "请补充:" in result.stderr
    assert "部署状态是什么？" not in result.stdout and "请补充:" not in result.stdout
    assert json.loads(result.stdout.splitlines()[-1]) == {"status": "stopped"}
