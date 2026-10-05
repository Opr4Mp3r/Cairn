"""Own Python process only: real CLI handler and shared execution, fake Agent process."""
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import os
import signal
import uuid

from click.testing import CliRunner
from fastapi.testclient import TestClient
from cairn.audit_evidence import repository_baseline
from cairn.cli import main
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.output_parser import extract_json_object
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import CreateAuditRequest

root = Path.cwd()
run = Path(__file__).resolve().parent / ("controller-real-sigterm-" + uuid.uuid4().hex)
run.mkdir()
repo, work = run / "source", run / "work"
repo.mkdir()
(repo / "app.py").write_text("def entry():\n    return 'fixture'\n")
config = DispatchConfig.load(root / "dispatch.audit.example.yaml")
config.local.workspace_root = str(work)
config.controller.timeouts.overseer = 15
config_path = run / "config.json"
config_path.write_text(config.model_dump_json())
baseline = repository_baseline(repo, work)
db._db_path = None
db.configure(run / "review.db")
calls, signal_events = [], []


class FakeProcess:
    def __init__(self, command, workspace):
        self.command, self.workspace = command, workspace
        self.reason = None
        self.index = len(calls)
        self.communications = 0

    def start(self):
        calls.append({"argv": self.command, "workspace": self.workspace})

    def cancel(self, reason):
        self.reason = reason

    def kill(self):
        self.cancel("kill")

    def communicate(self, timeout):
        self.communications += 1
        if self.index == 0 and self.reason is None:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler) and handler.__name__ == "terminate", "Refuse SIGTERM unless the actual CLI handler is installed"
            signal_events.append({"target_pid": os.getpid(), "mechanism": "signal.raise_signal(SIGTERM)",
                                  "handler": handler.__qualname__})
            signal.raise_signal(signal.SIGTERM)
            raise AssertionError("Actual CLI SIGTERM handler should raise KeyboardInterrupt")
        if self.reason:
            return ProcessResult(-15, "partial-before-SIGTERM", "", cancelled=True, cancel_reason=self.reason)
        payload = extract_json_object(self.command[-1])
        assert "audit" not in payload["target"], "The second call must be the Intent review"
        response = json.dumps({"decision": "approved", "reason": "normal fixture direction", "fact_audit": None, "hint": None})
        return ProcessResult(0, response, "")


def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5):
    return FakeProcess(command, handle)


with TestClient(app) as http:
    client = CairnClient("http://testserver")
    client._session = lambda: http
    body = CreateAuditRequest(
        title="own-process SIGTERM", origin="fixture", goal="source review", user_goal="source review",
        repo_path=str(repo), workspace_root=str(work), baseline=baseline, policy={"effect": "source review", "cvss": {}},
        seeds=[{"key": "entry", "description": "fixture entry", "audit": {"review_status": "verified",
            "evidence": [{"path": "app.py", "start_line": 1, "end_line": 2, "sha256": baseline["files"]["app.py"]}],
            "conditions": ["fixture"], "validation_level": "source_analysis"}}],
        intents=[{"from": ["entry"], "description": "inspect source"}],
    )
    created = client.create_audit(body)
    assert created.ok, created.text
    pid = created.data["project"]["id"]
    meta = client.get_audit(pid)
    workspace = work / pid
    workspace.mkdir(parents=True)
    (workspace / "repo").symlink_to(repo, target_is_directory=True)
    (workspace / "input.json").write_text(json.dumps({"project_id": pid, "repo_path": meta.repo_path, "policy_digest": meta.policy_digest}))
    assert client.start_audit(pid).ok
    with patch("cairn.dispatcher.protocol.client.CairnClient", lambda _url: client), patch.object(LocalBackend, "build_exec_process", build_process):
        result = CliRunner().invoke(main, ["audit", "--project", pid, "--once", "--config", str(config_path)])
    records = client.get_audit(pid).records
    observation = {
        "os_sigterm_tested": True, "signal_target": "only this newly launched isolated test Python process",
        "pid": os.getpid(), "signal_events": signal_events,
        "cli_agent_invoked": False, "model_invoked": False, "tcp_port_bound": False,
        "actual_path": ["CLI audit --project --once via CliRunner", "actual CLI SIGTERM handler", "shared runtime execution",
                        "actual call_agent and registered Claude Driver", "original monitor_once", "real TestClient Server and SQLite"],
        "exit_code": result.exit_code, "exception": repr(result.exception), "output": result.output,
        "agent_process_plans_after_signal": len(calls), "calls": calls,
        "records": [{"phase": r.phase, "object_type": r.object_type, "attempt_status": r.attempt_status, "error": r.error} for r in records],
        "bug_reproduced": bool(signal_events) and len(calls) == 2 and result.exit_code == 0,
        "source_hashes": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
            "cairn/src/cairn/controller/monitor.py", "cairn/src/cairn/cli.py", "cairn/src/cairn/dispatcher/runtime/execution.py")},
    }
output = run / "observation.json"
output.write_text(json.dumps(observation, ensure_ascii=False, indent=2) + "\n")
print(str(output))
print(json.dumps({key: observation[key] for key in ("os_sigterm_tested", "signal_events", "exit_code", "output", "agent_process_plans_after_signal", "records", "bug_reproduced")}, ensure_ascii=False))
