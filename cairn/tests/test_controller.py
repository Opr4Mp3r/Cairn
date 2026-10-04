from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time

from click.testing import CliRunner
from pydantic import ValidationError
import pytest

from cairn.cli import main
from cairn.controller.app import call_agent, file_probe_prompt
from cairn.controller.models import FileReadResult
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.runtime import execution
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime import local_process
from cairn.dispatcher.tasks import bootstrap, common, explore, reason
from cairn.dispatcher.workers import registry
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver

from conftest import make_config


def config_payload():
    payload = make_config().model_dump()
    payload["runtime"]["execution"] = "local"
    payload["runtime"]["worker_healthcheck"] = "disabled"
    payload["workers"][0]["type"] = "claudecode"
    payload["controller"] = {"roles": {"builder": "test-worker", "reviewer": "test-worker"}}
    return payload


def fake_cli(tmp_path, monkeypatch, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])


def print_json(payload):
    return "printf '%s\\n' " + shlex.quote(json.dumps(payload))


def call(config, tmp_path, **kwargs):
    return call_agent(
        config, LocalBackend(LocalConfig()), workspace=tmp_path,
        role=kwargs.pop("role", "builder"), prompt="read a file", result_model=FileReadResult,
        timeout_seconds=kwargs.pop("timeout_seconds", 10), phase="test_probe", **kwargs,
    )


def test_controller_and_research_share_execution_and_fresh_sessions(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, print_json({"path": "/source.txt", "content": "observed"}))
    config = DispatchConfig.model_validate(config_payload())
    runner = execution.run_worker_process
    assert common.run_worker_process is runner
    assert all(task.run_worker_process is runner for task in (bootstrap, reason, explore))
    invocations = []

    def spy(*args, **kwargs):
        invocations.append(args[3])
        return runner(*args, **kwargs)

    monkeypatch.setattr(execution, "run_worker_process", spy)
    first = call(config, tmp_path)
    second = call(config, tmp_path, role="reviewer")
    assert first.status == second.status == "success"
    assert first.session and second.session and first.session != second.session
    assert first.data == {"path": "/source.txt", "content": "observed"}
    assert len(invocations) == 2
    assert first.session in invocations[0] and second.session in invocations[1]
    assert first.process.stdout and first.response


@pytest.mark.parametrize("body,status", [
    ("echo not-json", "invalid_output"),
    (print_json({"path": 42, "content": "value"}), "invalid_output"),
    (print_json({"path": "/source.txt", "content": "value", "extra": True}), "invalid_output"),
    (print_json({"path": "/source.txt", "content": "value"}) + "\nexit 7", "failed"),
])
def test_controller_invalid_answer_or_process_failure_is_not_success(tmp_path, monkeypatch, body, status):
    fake_cli(tmp_path, monkeypatch, body)
    result = call(DispatchConfig.model_validate(config_payload()), tmp_path)
    assert result.status == status
    assert result.data is None and result.error
    assert result.process.stdout


def test_controller_timeout_kills_cli(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, "sleep 30")
    started = time.monotonic()
    result = call(DispatchConfig.model_validate(config_payload()), tmp_path, timeout_seconds=1)
    assert result.status == "timed_out" and result.process.timed_out
    assert result.data is None
    assert time.monotonic() - started < 10


def test_controller_cancellation_stops_running_cli(tmp_path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    fake_cli(tmp_path, monkeypatch, "echo $$ > " + shlex.quote(str(pid_file)) + "\nsleep 30")
    config = DispatchConfig.model_validate(config_payload())
    cancellation = TaskCancellation()
    results = []
    thread = threading.Thread(target=lambda: results.append(call(config, tmp_path, cancellation=cancellation)))
    thread.start()
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    cancellation.cancel("test cancellation")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results[0].status == "cancelled" and results[0].data is None
    assert results[0].process.cancel_reason == "test cancellation"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_cancelled_call_does_not_start_cli(tmp_path, monkeypatch):
    started = tmp_path / "started"
    fake_cli(tmp_path, monkeypatch, "touch " + shlex.quote(str(started)))
    cancellation = TaskCancellation()
    cancellation.cancel("cancelled before invocation")
    result = call(DispatchConfig.model_validate(config_payload()), tmp_path, cancellation=cancellation)
    assert result.status == "cancelled" and not started.exists()


def test_probe_startup_interrupt_records_cancelled_without_communicating(tmp_path, monkeypatch):
    source = tmp_path / "target.txt"
    source.write_text("actual\n")
    fake_cli(tmp_path, monkeypatch, "exit 0")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_payload()))
    output = tmp_path / "outputs"
    cleanup = []
    attachments = []
    communicate_calls = []
    cancel = local_process.LocalProcess.cancel
    attach = TaskCancellation.attach_process

    def interrupted_start(*_args, **_kwargs):
        raise KeyboardInterrupt("interrupted before Popen returned")

    def record_cancel(process, reason):
        cleanup.append((process._process, reason))
        cancel(process, reason)

    def record_attach(cancellation, process):
        attachments.append(process)
        attach(cancellation, process)

    def unexpected_communicate(*_args, **_kwargs):
        communicate_calls.append(True)
        raise AssertionError("unstarted process must not communicate")

    monkeypatch.setattr(local_process.subprocess, "Popen", interrupted_start)
    monkeypatch.setattr(local_process.LocalProcess, "cancel", record_cancel)
    monkeypatch.setattr(local_process.LocalProcess, "communicate", unexpected_communicate)
    monkeypatch.setattr(TaskCancellation, "attach_process", record_attach)
    result = CliRunner().invoke(main, ["audit-probe", "--config", str(config_path), "--file", str(source), "--output", str(output)])
    assert result.exit_code != 0 and not isinstance(result.exception, AssertionError)
    receipts = list(output.glob("*/result.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "cancelled" and receipt["data"] is None
    assert receipt["process"]["cancelled"] and receipt["session"]
    assert receipt["process"]["cancel_reason"] == "interrupted before Popen returned"
    assert cleanup == [(None, "interrupted before Popen returned")]
    assert attachments == [None] and not communicate_calls


def test_startup_interrupt_cleans_partial_resources_and_detaches_hooks(tmp_path):
    class InterruptedProcess:
        allocated = False
        cancel_reason = None

        def start(self):
            self.allocated = True
            raise KeyboardInterrupt("partial startup interrupted")

        def cancel(self, reason):
            self.allocated = False
            self.cancel_reason = reason

        def communicate(self, timeout):
            raise AssertionError("start did not succeed")

    process = InterruptedProcess()

    class Backend:
        def build_exec_process(self, *_args, **_kwargs):
            return process

    class Hook:
        is_cancelled = False
        attached = process

        def attach_process(self, value):
            self.attached = value

    lease = Hook()
    cancellation = Hook()
    result = execution.run_worker_process(
        Backend(), str(tmp_path), make_config().workers[0], ["unused"],
        phase="startup_interrupt", timeout_seconds=10, lease=lease, cancellation=cancellation,
    )
    assert result.cancelled and result.cancel_reason == "partial startup interrupted"
    assert process.cancel_reason == result.cancel_reason and not process.allocated
    assert lease.attached is None and cancellation.attached is None


def test_controller_uses_shared_registry_without_harness_whitelist(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, print_json({"path": "/source.txt", "content": "observed"}))
    monkeypatch.setitem(registry.LOCAL_DRIVERS, "mock", ClaudeCodeDriver())
    payload = config_payload()
    payload["workers"][0]["type"] = "mock"
    assert call(DispatchConfig.model_validate(payload), tmp_path).status == "success"


def test_controller_rejects_missing_role_and_unsupported_registration(monkeypatch):
    payload = config_payload()
    payload["controller"]["roles"]["reviewer"] = "missing"
    with pytest.raises(ValidationError, match="references unknown worker"):
        DispatchConfig.model_validate(payload)
    monkeypatch.delitem(registry.LOCAL_DRIVERS, "claudecode")
    with pytest.raises(ValidationError, match="not registered for local"):
        DispatchConfig.model_validate(config_payload())
    with pytest.raises(ValueError, match="not registered"):
        registry.get_driver("missing", "local")
    with pytest.raises(ValueError, match="unsupported execution mode"):
        registry.get_driver("claudecode", "remote")


def test_old_configuration_still_loads_without_controller():
    assert make_config().controller is None
    root = Path(__file__).resolve().parents[2]
    config = DispatchConfig.load(root / "dispatch.controller.example.yaml")
    assert config.controller.roles.builder == config.controller.roles.reviewer == "claude-main"
    assert config.controller.research_prompt_group is None


def test_probe_keeps_input_and_raw_output_and_verifies_file_content(tmp_path, monkeypatch):
    source = tmp_path / "target.txt"
    source.write_text("secret-marker-only-in-file\n")
    fake_cli(tmp_path, monkeypatch, print_json({"path": str(source), "content": source.read_text()}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_payload()))
    output = tmp_path / "outputs"
    result = CliRunner().invoke(main, ["audit-probe", "--config", str(config_path), "--file", str(source), "--output", str(output)])
    assert result.exit_code == 0, result.output
    receipt = next(output.glob("*/result.json"))
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "success" and payload["session"]
    assert payload["data"]["content"] == source.read_text()
    assert "secret-marker-only-in-file" not in payload["prompt"]
    assert json.loads(receipt.with_name("input.json").read_text())["sha256"]
    assert payload["process"]["stdout"] and payload["response"]
    assert "secret-marker-only-in-file" not in file_probe_prompt(source)


def test_probe_rejects_schema_valid_but_fabricated_file_contents(tmp_path, monkeypatch):
    source = tmp_path / "target.txt"
    source.write_text("actual\n")
    fake_cli(tmp_path, monkeypatch, print_json({"path": str(source), "content": "fabricated\n"}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_payload()))
    output = tmp_path / "outputs"
    result = CliRunner().invoke(main, ["audit-probe", "--config", str(config_path), "--file", str(source), "--output", str(output)])
    assert result.exit_code != 0
    receipt = json.loads(next(output.glob("*/result.json")).read_text())
    assert receipt["status"] == "invalid_output" and receipt["data"] is None


def test_probe_records_invalid_path_and_preserves_raw_output(tmp_path, monkeypatch):
    source = tmp_path / "target.txt"
    source.write_text("actual\n")
    payload = {"path": "/\x00", "content": "actual\n"}
    fake_cli(tmp_path, monkeypatch, print_json(payload))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_payload()))
    output = tmp_path / "outputs"
    result = CliRunner().invoke(main, ["audit-probe", "--config", str(config_path), "--file", str(source), "--output", str(output)])
    assert result.exit_code != 0
    receipts = list(output.glob("*/result.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "invalid_output" and receipt["data"] is None
    assert receipt["process"]["returncode"] == 0
    assert json.loads(receipt["process"]["stdout"]) == payload


@pytest.mark.parametrize("root_kind", ["absolute", "relative", "home", "output_home"])
def test_probe_normalizes_workspace_root(tmp_path, monkeypatch, root_kind):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    controlled_home = tmp_path / "user-directory"
    expanduser = os.path.expanduser

    def controlled_expanduser(path):
        if path == "~":
            return str(controlled_home)
        if path.startswith("~/"):
            return str(controlled_home / path[2:])
        return expanduser(path)

    monkeypatch.setattr(os.path, "expanduser", controlled_expanduser)
    source = tmp_path / "target.txt"
    source.write_text("actual\n")
    fake_cli(tmp_path, monkeypatch, print_json({"path": str(source), "content": source.read_text()}))
    payload = config_payload()
    args = []
    if root_kind == "absolute":
        root = str(tmp_path / "absolute-root")
        expected = Path(root)
    elif root_kind == "relative":
        root = "relative-root"
        expected = config_dir / root
    else:
        root = "~/cp1-probes"
        expected = controlled_home / "cp1-probes"
        if root_kind == "output_home":
            args = ["--output", root]
    payload["local"] = {"workspace_root": root}
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(payload))
    result = CliRunner().invoke(main, ["audit-probe", "--config", str(config_path), "--file", str(source), *args])
    assert result.exit_code == 0, result.output
    receipts = list(expected.glob("*/result.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["workspace"] == str(receipts[0].parent)
    assert not (config_dir / "~").exists() and not (cwd / "~").exists()


def test_probe_records_failure_if_target_disappears_during_call(tmp_path, monkeypatch):
    source = tmp_path / "target.txt"
    source.write_text("actual\n")
    fake_cli(tmp_path, monkeypatch, "rm " + shlex.quote(str(source)) + "\n" + print_json({"path": str(source), "content": "actual\n"}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_payload()))
    output = tmp_path / "outputs"
    result = CliRunner().invoke(main, ["audit-probe", "--config", str(config_path), "--file", str(source), "--output", str(output)])
    assert result.exit_code != 0
    receipt = json.loads(next(output.glob("*/result.json")).read_text())
    assert receipt["status"] == "invalid_output" and receipt["process"]["stdout"]


def test_probe_sigterm_cleans_up_cli_process(tmp_path, monkeypatch):
    source = tmp_path / "target.txt"
    source.write_text("actual\n")
    pid_file = tmp_path / "agent.pid"
    child_pid_file = tmp_path / "agent-child.pid"
    fake_cli(tmp_path, monkeypatch,
        "printf 'partial-output-before-cancellation\\n'\n"
        "printf 'partial-error-before-cancellation\\n' >&2\n"
        "sleep 30 &\necho $! > " + shlex.quote(str(child_pid_file)) +
        "\necho $$ > " + shlex.quote(str(pid_file)) + "\nwait",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_payload()))
    output = tmp_path / "outputs"
    command = [str(Path(sys.executable).parent / "cairn"), "audit-probe", "--config", str(config_path), "--file", str(source), "--output", str(output)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
        assert process.returncode != 0
        receipt = json.loads(next(output.glob("*/result.json")).read_text())
        assert receipt["status"] == "cancelled"
        assert receipt["process"]["cancelled"] and receipt["data"] is None
        assert "partial-output-before-cancellation\n" in receipt["process"]["stdout"]
        assert "partial-error-before-cancellation\n" in receipt["process"]["stderr"]
        for path in (pid_file, child_pid_file):
            pid = int(path.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError(f"CLI process {pid} survived cancellation")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
