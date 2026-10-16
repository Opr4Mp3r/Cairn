from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sys

import pytest

from cairn.controller.app import call_agent
from cairn.controller.models import FileReadResult
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.tasks import common, explore, reason

from conftest import FakeClient, make_config, make_intent, make_project
from test_role_pool_scheduler import make_pool_loop
from test_scheduler_logic import _summary


CLI = r'''
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
harness = Path(sys.argv[0]).name
resume = args[2] if harness == "codex" and args[:2] == ["exec", "resume"] else None
if harness == "pi" and "--session" in args:
    resume = args[args.index("--session") + 1]
session = resume or str(uuid.uuid4())
with open(os.environ["CAIRN_TEST_TRACE"], "a") as trace:
    trace.write(json.dumps({"harness": harness, "argv": args, "session": session, "cwd": os.getcwd()}) + "\n")
text = os.environ["CAIRN_TEST_ANSWER"]
if os.environ.get("CAIRN_TEST_NEEDS_RESUME") and not resume:
    text = "Read the source; need to finish the final response."
failed = bool(os.environ.get("CAIRN_TEST_FAILED"))
def emit(value): print(json.dumps(value), flush=True)
if harness == "codex":
    assert "--json" in args and "--skip-git-repo-check" in args
    emit({"type": "thread.started", "thread_id": session})
    emit({"type": "turn.started"})
    emit({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": text}})
    emit({"type": "item.completed", "item": {"type": "agent_message", "phase": "final_answer", "text": text}})
    emit({"type": "turn.failed", "error": {"message": "fixture failure"}} if failed else {"type": "turn.completed"})
else:
    assert args[args.index("--mode") + 1] == "json"
    emit({"type": "session", "id": session})
    emit({"type": "agent_start"})
    message = {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": "error" if failed else "stop"}
    emit({"type": "turn_end", "message": message})
    emit({"type": "agent_end", "messages": [message]})
'''


@pytest.fixture
def native_clis(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in ("pi", "codex"):
        binary = folder / name
        binary.write_text(f"#!{sys.executable}\n" + CLI)
        binary.chmod(0o755)
    trace = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAIRN_TEST_TRACE", str(trace))
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))
    return trace


def config_for(harness):
    data = make_config().model_dump()
    data["runtime"].update(execution="local", worker_healthcheck="disabled")
    data["workers"][0]["type"] = harness
    data["controller"] = {"roles": {"builder": "test-worker", "reviewer": "test-worker"}}
    return DispatchConfig.model_validate(data)


def calls(trace):
    return [json.loads(line) for line in trace.read_text().splitlines()]


def test_controller_mixed_pool_resumes_on_original_harness(native_clis, tmp_path, monkeypatch):
    data = config_for("pi").model_dump()
    data["role_pools"] = {role: {"pi": 1, "codex": 1} for role in (
        "explore", "reason", "preflight", "seed_review", "overseer", "closing", "final_review",
    )}
    config = DispatchConfig.model_validate(data)
    backend = LocalBackend(LocalConfig())
    monkeypatch.setenv("CAIRN_TEST_ANSWER", json.dumps({"path": "/fixture", "content": "observed"}))

    def invoke(session=None):
        return call_agent(config, backend, role="reviewer", prompt="read fixture",
            result_model=FileReadResult, workspace=tmp_path, timeout_seconds=10,
            phase="preflight_format_repair" if session else "preflight", session=session)

    first, second = invoke(), invoke()
    continued_first, continued_second, third = invoke(first.session), invoke(second.session), invoke()
    results = [first, second, continued_first, continued_second, third]
    assert all(item.status == "success" for item in results)
    assert [item.harness for item in results] == ["pi", "codex", "pi", "codex", "pi"]
    assert continued_first.session == first.session and continued_second.session == second.session
    assert len({first.session, second.session, third.session}) == 3
    assert all(item.data == {"path": "/fixture", "content": "observed"} for item in results)
    assert all("--model" not in item["argv"] and "--provider" not in item["argv"] for item in calls(native_clis))


@pytest.mark.parametrize("harness", ["pi", "codex"])
def test_research_cli_conclude_uses_exact_session(native_clis, tmp_path, monkeypatch, harness):
    monkeypatch.setenv("CAIRN_TEST_ANSWER", json.dumps({"accepted": True, "data": {"description": "native fact"}}))
    monkeypatch.setenv("CAIRN_TEST_NEEDS_RESUME", "1")
    config = config_for(harness)
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    result = explore.run_explore_task(config, client, backend, project, "facts: []", intent,
        config.workers[0], TaskCancellation())
    assert result == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "native fact")]
    invocations = calls(native_clis)
    assert len(invocations) == 2 and invocations[0]["session"] == invocations[1]["session"]
    assert ("--session" in invocations[1]["argv"]) if harness == "pi" else invocations[1]["argv"][:2] == ["exec", "resume"]


@pytest.mark.parametrize("harness", ["pi", "codex"])
def test_research_reason_accepts_final_message(native_clis, tmp_path, monkeypatch, harness):
    monkeypatch.setenv("CAIRN_TEST_ANSWER", json.dumps({"accepted": True, "data": {
        "intents": [{"from": ["f001"], "description": "inspect the remaining condition"}],
    }}))
    config = config_for(harness)
    project = make_project()
    client = FakeClient(project)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    result = reason.run_reason_task(config, client, backend, project, "facts: []",
        config.workers[0], TaskCancellation())
    assert result == "success"
    assert len(client.created_intents) == 1
    assert client.created_intents[0][2] == "inspect the remaining condition"


@pytest.mark.parametrize("harness", ["pi", "codex"])
def test_native_failure_never_becomes_controller_or_research_success(native_clis, tmp_path, monkeypatch, harness):
    monkeypatch.setenv("CAIRN_TEST_FAILED", "1")
    monkeypatch.setenv("CAIRN_TEST_ANSWER", json.dumps({"path": "/fixture", "content": "false success"}))
    config = config_for(harness)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    result = call_agent(config, backend, role="reviewer", prompt="fixture", result_model=FileReadResult,
        workspace=tmp_path, timeout_seconds=10, phase="preflight")
    assert result.status == "invalid_output" and result.data is None
    assert result.process.returncode == 0  # CLI may exit zero despite a model-level failure.
    monkeypatch.setenv("CAIRN_TEST_ANSWER", json.dumps({"accepted": True, "data": {"description": "false fact"}}))
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    assert explore.run_explore_task(config, client, backend, project, "facts: []", intent,
        config.workers[0], TaskCancellation()) == "failed"
    assert client.concluded == []
    assert client.released


def test_dispatcher_schedules_mixed_harness_capacities():
    loop = make_pool_loop(["mixed"])
    data = loop.config.model_dump()
    data["role_pools"] = {"explore": {"claudecode": 1, "pi": 2, "codex": 1}, "reason": {"codex": 1}}
    loop.config = DispatchConfig.model_validate(data)
    loop.runtime_project_ids.clear()
    loop._dispatch_available([_summary("mixed", "active")])
    assert loop.config.pool_capacity("explore") == 4
    assert Counter(task.worker_name for task in loop.futures.values()) == {
        "pool-explore-claudecode": 1, "pool-explore-pi": 2, "pool-explore-codex": 1, "pool-reason-codex": 1,
    }
