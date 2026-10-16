from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from click.testing import CliRunner
import pytest

from cairn.cli import main
from cairn.controller.kritt_query import write_json
from cairn.controller.local import CLI
from cairn.controller.native import start_prepared
from cairn.controller.research import run_research
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from test_audit_workflow import install_cli
from test_kritt_audit_reconnect import ready, seed_review
from test_kritt_probe import probe


def test_native_prepare_cli_writes_materials_without_agent(probe, monkeypatch):
    _, config_path, view, folder = probe
    monkeypatch.setattr("cairn.controller.builder.call_agent",
                        lambda *args, **kwargs: pytest.fail("prepare must not call an Agent"))
    result = CliRunner().invoke(main, ["prepare", "--config", str(config_path), "--target", "test",
                                     "--goal", "静态阅读入口，保留部署未知", "--run-dir", str(folder),
                                     "--view", str(view)])
    assert result.exit_code == 0, result.output
    prepared = json.loads(result.stdout)
    assert prepared["status"] == "prepared" and prepared["calls"] == 0
    assert prepared["builder_session"] is None and Path(prepared["plan_schema"]).exists()
    assert prepared == json.loads((folder / "run.json").read_text())
    assert not Path(prepared["plan"]).exists() and not (folder / "cairn.db").exists()


def test_native_start_rejects_existing_database_project_without_locator(probe, monkeypatch):
    config, prepared, _, _ = ready(probe)
    _, config_path, _, folder = probe
    config_path.write_text(config.model_dump_json())
    write_json(folder / "run.json", prepared)
    with sqlite3.connect(folder / "cairn.db") as database:
        database.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        database.execute("INSERT INTO projects VALUES ('response-lost-project')")
    monkeypatch.setattr("cairn.controller.native.subprocess.Popen",
                        lambda *args, **kwargs: pytest.fail("an existing project must not be started again"))
    assert not (folder / "project.json").exists()
    with pytest.raises(ValueError, match="已创建项目"):
        start_prepared(config_path, folder)
    assert not (folder / "launch.json").exists()
    with sqlite3.connect(folder / "cairn.db") as database:
        assert database.execute("SELECT id FROM projects").fetchall() == [("response-lost-project",)]


@pytest.mark.parametrize("invalid,error", [
    ("unknown_seed", "unknown seed reference"), ("action_wrapper", "Extra inputs are not permitted"),
])
def test_native_start_validates_plan_before_spawning(probe, monkeypatch, invalid, error):
    config, prepared, plan, _ = ready(probe)
    _, config_path, _, folder = probe
    config_path.write_text(config.model_dump_json())
    write_json(folder / "run.json", prepared)
    if invalid == "unknown_seed":
        plan["intents"][0]["from"] = ["missing-seed"]
    else:
        plan["action"] = "finish"
    write_json(Path(prepared["plan"]), plan)
    monkeypatch.setattr("cairn.controller.native.subprocess.Popen",
                        lambda *args, **kwargs: pytest.fail("invalid plans must not start research"))
    with pytest.raises(ValueError, match=error):
        start_prepared(config_path, folder)
    assert not (folder / "launch.json").exists() and not (folder / "cairn.db").exists()


def test_external_preflight_rejection_can_be_revised_in_same_run_with_no_revision_budget(probe, monkeypatch):
    config, prepared, plan, _ = ready(probe)
    folder = Path(prepared["run_dir"])
    config.server = "http://127.0.0.1:0"
    config.controller.max_goal_revisions = 0
    prepared.update(status="prepared", calls=0, builder_session=None)
    issues = ["显式保留部署未知"]
    calls = install_cli(folder.parent, monkeypatch, [
        {"approved": False, "issues": issues}, {"approved": True, "issues": []}, seed_review(),
    ])
    monkeypatch.setattr("cairn.controller.research.continue_builder",
                        lambda *args, **kwargs: pytest.fail("external review must return to the native conversation"))
    rejected = run_research(config, prepared, external_builder=True)
    assert rejected["status"] == "needs_revision" and rejected["issues"] == issues
    assert not (folder / "project.json").exists()
    assert set(rejected["processes"]) == {"server"}
    assert rejected["processes"]["server"]["returncode"] is not None
    with pytest.raises(ProcessLookupError):
        os.kill(rejected["processes"]["server"]["pid"], 0)
    with sqlite3.connect(folder / "cairn.db") as database:
        assert database.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    rejection_receipt = Path(rejected["receipts"]) / "01-preflight" / "result.json"
    assert json.loads(rejection_receipt.read_text())["data"]["issues"] == issues

    plan["goal"] = "核对源码权限条件，部署状态保持未知"
    write_json(Path(prepared["plan"]), plan)
    token = TaskCancellation()
    timer = threading.Timer(25, token.cancel, args=("native test deadline",))

    def stop_after_handoff(event):
        if event["status"] == "researching":
            with closing(CairnClient(event["server"], trust_env=False)) as client:
                assert client.set_project_status(event["project_id"], "stopped").ok

    timer.start()
    try:
        accepted = run_research(config, prepared, external_builder=True, cancellation=token,
                                report=stop_after_handoff)
    finally:
        timer.cancel()
        timer.join()
    assert accepted["status"] == "stopped" and (folder / "project.json").exists()
    assert all(item["returncode"] is not None for item in accepted["processes"].values())
    assert len(list((folder / "controller").glob("prepare-*"))) == 2
    assert json.loads(rejection_receipt.read_text())["data"]["issues"] == issues
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    prompts = [json.loads(call["argv"][-1].rsplit("\n\n", 1)[1]) for call in recorded[:3]]
    assert prompts[1]["fixed_contract"]["goal"] == plan["goal"]
    assert all("-r" not in call["argv"] for call in recorded[:3])
    assert json.loads((folder / "research.json").read_text())["project_status"] == "stopped"


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(.05)
    raise AssertionError("native process state did not arrive before the deadline")


def test_native_start_survives_cli_exit_rejects_duplicate_and_cleans_owned_processes(probe, monkeypatch):
    config, prepared, _, _ = ready(probe)
    _, config_path, _, folder = probe
    config.server = "http://127.0.0.1:0"
    config.controller.max_goal_revisions = 0
    config.runtime.interval = 1
    config.runtime.max_workers = config.runtime.max_project_workers = 1
    config.tasks.reason.timeout = config.tasks.explore.timeout = 120
    prepared.update(status="prepared", calls=0, builder_session=None)
    write_json(folder / "run.json", prepared)
    config_path.write_text(config.model_dump_json())
    started = folder / "research-agent.json"
    monkeypatch.setenv("NATIVE_TEST_AGENT_STARTED", str(started))
    binary = folder.parent / "bin" / "claude"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys, time
prompt = sys.argv[-1]
assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
if '"output_schema"' in prompt:
    context = json.loads(prompt.rsplit("\n\n", 1)[1])
    properties = context["output_schema"].get("properties", {})
    if "approved" in properties:
        result = {"approved": True, "issues": []}
    elif "reviews" in properties:
        result = {"reviews": [{"key": seed["key"], "audit": {"review_status": "verified",
            "evidence": [{"path": "module/install/control.php", "start_line": 2}],
            "conditions": ["仅源码观察"], "validation_level": "source_analysis"}}
            for seed in context["seeds"]]}
    else:
        result = {"decision": "approved", "reason": "synthetic review", "fact_audit": None, "hint": None}
    print(json.dumps(result))
else:
    marker = pathlib.Path(os.environ["NATIVE_TEST_AGENT_STARTED"])
    while not marker.with_suffix(".continue").exists():
        time.sleep(.05)
    marker.write_text(json.dumps({"pid": os.getpid()}))
    time.sleep(90)
''', encoding="utf-8")
    binary.chmod(0o755)
    command = [*CLI, "start", "--config", str(config_path), "--run-dir", str(folder)]
    state = None
    try:
        launcher = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert launcher.returncode == 0, launcher.stdout + launcher.stderr
        state = json.loads(launcher.stdout)
        assert state["status"] == "researching" and _alive(state["supervisor_pid"])
        assert set(state["processes"]) == {"server", "controller", "dispatcher"}
        assert all(_alive(item["pid"]) for item in state["processes"].values())
        started.with_suffix(".continue").touch()
        _wait_until(started.exists)
        agent = json.loads(started.read_text())["pid"]
        assert _alive(agent)
        with closing(CairnClient(state["server"], trust_env=False)) as client:
            assert client.get_project(state["project_id"]).project.status == "active"
        duplicate = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert duplicate.returncode != 0 and "正在准备或研究中" in duplicate.stderr
        assert json.loads((folder / "launch.json").read_text())["supervisor_pid"] == state["supervisor_pid"]
    finally:
        launch = folder / "launch.json"
        saved = json.loads(launch.read_text()) if launch.exists() else {}
        supervisor = saved.get("supervisor_pid")
        if supervisor and _alive(supervisor):
            os.kill(supervisor, signal.SIGTERM)
            _wait_until(lambda: not _alive(supervisor), timeout=35)
    assert state is not None
    finished = json.loads((folder / "launch.json").read_text())
    assert finished["status"] == "interrupted" and "cleanup_error" not in finished
    assert all(item["returncode"] is not None and not _alive(item["pid"])
               for item in finished["processes"].values())
    assert not _alive(agent)
    assert json.loads((folder / "result.json").read_text())["project"]["status"] == "stopped"
