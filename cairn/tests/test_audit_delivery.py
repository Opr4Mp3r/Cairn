from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    shutil.copytree(ROOT / "cairn/tests/fixtures/audit-target", repo)
    binary = tmp_path / "bin"
    binary.mkdir()
    cli = binary / "claude"
    cli.write_text(f"#!{sys.executable}\n" + (ROOT / "cairn/tests/fixtures/audit-delivery-cli.py").read_text())
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "cairn/src"))
    monkeypatch.setenv("CP6_CALLS", str(tmp_path / "calls.jsonl"))
    config = yaml.safe_load((ROOT / "dispatch.audit.example.yaml").read_text())
    config["runtime"]["interval"] = 1
    config["tasks"]["explore"] = {"timeout": 20, "conclude_timeout": 5}
    config["controller"]["poll_interval"] = 1
    config["controller"]["timeouts"] = dict.fromkeys(["build", "preflight", "seed_review", "overseer", "closing", "final_review"], 15)
    config["controller"]["budget"] = {"research_seconds": 6, "drain_seconds": 2, "final_review_seconds": 30}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    run = tmp_path / "run"
    command = [sys.executable, "-m", "cairn.controller.local", "--config", str(path), "--repo", str(repo),
               "--goal", "trace public fixture source", "--run-dir", str(run), "--port", "0"]
    return command, run, tmp_path / "calls.jsonl"


def assert_reaped(pids):
    deadline = time.monotonic() + 5
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.remove(pid)
        time.sleep(0.05)
    assert not remaining, f"processes survived cleanup: {remaining}"


def diagnostics(run):
    return "\n".join(path.name + ": " + path.read_text() for path in run.glob("*.log"))


@pytest.mark.parametrize("outcome,expected", [("success", "completed"), ("budget", "budget_exhausted")])
def test_three_process_delivery_finishes_and_reaps_children(delivery, monkeypatch, outcome, expected):
    command, run, calls = delivery
    monkeypatch.setenv("CP6_OUTCOME", outcome)
    process = subprocess.run(command, capture_output=True, text=True, timeout=65)
    assert process.returncode == 0, process.stdout + process.stderr + diagnostics(run)
    summary = json.loads((run / "summary.json").read_text())
    project = json.loads((run / "project.json").read_text())
    audit = json.loads((run / "audit.json").read_text())
    assert summary["status"] == project["project"]["status"] == expected
    assert set(summary["processes"]) == {"server", "controller", "dispatcher"}
    assert (run / "graph.yaml").is_file() and (run / "cairn.db").is_file()
    records = audit["records"]
    phases = {record["phase"] for record in records}
    assert {"build", "preflight", "overseer", "final_review" if outcome == "success" else "closing"} <= phases
    assert "seed_review" not in phases  # This fixture has no initial seeds to review.
    assert any(fact.get("audit", {}).get("evidence") for fact in project["facts"])
    sessions = [record["session"] for record in records]
    assert len(set(sessions)) == len(sessions)
    assert_reaped([item["pid"] for item in summary["processes"].values()]
                  + [item["pid"] for item in map(json.loads, calls.read_text().splitlines())])


def test_launcher_failure_reaps_owned_processes(delivery, monkeypatch):
    command, run, calls = delivery
    monkeypatch.setenv("CP6_FAIL", "build")
    process = subprocess.run(command, capture_output=True, text=True, timeout=35)
    assert process.returncode != 0
    summary = json.loads((run / "summary.json").read_text())
    assert summary["status"] == "failed" and "Controller exited" in summary["error"]
    assert_reaped([item["pid"] for item in summary["processes"].values()]
                  + [item["pid"] for item in map(json.loads, calls.read_text().splitlines())])


def test_port_conflict_leaves_existing_listener_untouched(delivery):
    command, run, _calls = delivery
    with socket.socket() as existing:
        existing.bind(("127.0.0.1", 0))
        existing.listen()
        command[-1] = str(existing.getsockname()[1])
        process = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert process.returncode != 0
        assert json.loads((run / "summary.json").read_text())["processes"] == {}
        with socket.create_connection(existing.getsockname(), timeout=1):
            accepted, _ = existing.accept()
            accepted.close()


def test_interrupt_cancels_agent_process_group(delivery, monkeypatch):
    command, run, calls = delivery
    monkeypatch.setenv("CP6_BLOCK", "explore")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    descendants = []
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if calls.exists():
                records = list(map(json.loads, calls.read_text().splitlines()))
                descendants = [record for record in records if "child_pid" in record]
                if descendants:
                    break
            assert process.poll() is None, diagnostics(run)
            time.sleep(0.1)
        assert descendants, "research Agent did not start"
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=35)
        summary = json.loads((run / "summary.json").read_text())
        assert summary["status"] == "interrupted"
        assert_reaped([record[key] for record in descendants for key in ("pid", "child_pid")]
                      + [item["pid"] for item in summary["processes"].values()])
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=35)


def test_ui_audit_state_matches_server_semantics():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the existing UI state helpers")
    html = (ROOT / "cairn/src/cairn/server/static/index.html").read_text()
    code = html.split("<script>\nfunction cairnApp()", 1)[1].split("</script>", 1)[0]
    checks = """
const assert = require('node:assert/strict');
const app = cairnApp();
const audit = {status:'active',mode:'audit',audit:{phase:'researching',completion_pending:true}};
app.project = {project:audit};
assert.equal(app.auditPhaseLabel(), '待独立验收');
assert.equal(app.auditPhaseLabel({status:'stopped',audit:{phase:'researching'}}), '已停止');
assert.equal(app.projectIsActive(), false);
assert.match(app.completionResultLabel({status:'pending'}), /完成申请/);
assert.equal(app.canToggleProject({status:'stopped',audit:{phase:'preparing'}}), false);
assert.equal(app.canToggleProject({status:'draining',audit:{phase:'finalizing'}}), true);
assert.equal(app.canToggleProject({status:'budget_exhausted'}), false);
assert.equal(app.canReopenProject({status:'completed',mode:'audit'}), false);
assert.equal(app.canReopenProject({status:'completed'}), true);
app.project = {project:{status:'active'}};
assert.equal(app.projectIsActive(), true);
assert.equal(app.completionResultLabel({}), 'Project completed');
app.project.project.status = 'budget_exhausted';
assert.equal(app.projectCanWriteHints(), true);
"""
    subprocess.run([node, "-e", "function cairnApp()" + code + checks], check=True, capture_output=True, text=True)
    assert "证据状态" in html and "auditRequests" in html


def test_shutdown_cancels_task_before_future_registration():
    from cairn.dispatcher.scheduler.loop import DispatcherLoop

    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.futures = {}
    cancellation = loop._new_cancellation()
    loop.cancel_running_tasks("test shutdown between submit and registration")
    assert cancellation.is_cancelled
    cancelled = []

    class LateProcess:
        def cancel(self, reason):
            cancelled.append(reason)

    cancellation.attach_process(LateProcess())
    assert cancelled == ["test shutdown between submit and registration"]
    assert loop._new_cancellation().is_cancelled


def test_run_output_cannot_modify_source_baseline(delivery):
    from cairn.controller.local import run_local

    command, _run, _calls = delivery
    config = Path(command[command.index("--config") + 1])
    repo = Path(command[command.index("--repo") + 1])
    output = repo / "new-audit-run"
    with pytest.raises(ValueError, match="outside the audited repository"):
        run_local(config, repo, "read source", output, port=0)
    assert not output.exists()
