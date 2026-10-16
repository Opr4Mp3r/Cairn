from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import zipfile

from click.testing import CliRunner
import pytest
import requests

from cairn.cli import main
from cairn.controller import submissions
from cairn.controller.local import CLI, stop_agent_groups
from cairn.dispatcher.protocol.client import CairnClient
from test_kritt_probe import probe
from test_managed_service import configured, wait_until

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/cairn-builder"
WRAPPER = SKILL / "scripts/cairn.py"


@pytest.mark.parametrize("state", ["pending", "needs_revision", "error", "deleted", "paused"])
def test_status_resolves_saved_submission_without_writing_or_controlling(tmp_path, monkeypatch, state):
    health = {"managed": True, "ready": False, "database_id": "original-db", "service_id": "new-owner"}
    path = tmp_path / "submission.json"
    path.write_text(json.dumps({"submission_id": "stable", "project_id": None,
                               "database_id": "original-db", "service_id": "old-owner"}))
    original = path.read_bytes()
    current = {**health, "submission_id": "stable", "run_dir": str(tmp_path), "status": state,
               "project_id": "real-project" if state in ("deleted", "paused") else None,
               "issues": ["preserve the static scope"] if state == "needs_revision" else []}
    calls = []
    class Client:
        def __init__(self, server, **kwargs): assert kwargs == {"trust_env": False}
        def close(self): pass
        def service_health(self): return health
        def get_submission(self, sid):
            calls.append(("submission", sid))
            return current
        def get_runtime(self, pid):
            assert state == "paused"
            calls.append(("runtime", pid))
            return {**health, "project_id": pid, "run_dir": str(tmp_path), "state": state}
        def control_runtime(self, *args): pytest.fail("status must not control tasks")
    monkeypatch.setattr(submissions, "CairnClient", Client)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("status must not start a service"))
    result = CliRunner().invoke(main, ["status", "--server", "http://127.0.0.1:8000", "--run-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["submission"] == current and not data["health"]["ready"]
    assert path.read_bytes() == original
    assert calls[0] == ("submission", "stable")
    assert bool(data["runtime"]) == (state == "paused")


def test_status_rejects_database_switch_before_resolving_saved_submission(tmp_path, monkeypatch):
    (tmp_path / "submission.json").write_text(json.dumps({"submission_id": "stable", "database_id": "original"}))
    class Client:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def service_health(self): return {"database_id": "other", "service_id": "other-owner"}
        def get_submission(self, sid): pytest.fail("must reject another database before lookup")
    monkeypatch.setattr(submissions, "CairnClient", Client)
    with pytest.raises(ValueError, match="another Cairn database"):
        submissions.inspect_prepared("http://127.0.0.1:8000", run_dir=tmp_path)


def test_status_service_only_and_offline_never_start_processes(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("status must not start a process"))
    class Client:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def service_health(self): return {"managed": False, "ready": False, "database_id": None, "service_id": None}
    monkeypatch.setattr(submissions, "CairnClient", Client)
    assert submissions.inspect_prepared("http://127.0.0.1:8000")["runtime"] is None
    monkeypatch.setattr(Client, "service_health", lambda self: (_ for _ in ()).throw(requests.ConnectionError()))
    result = CliRunner().invoke(main, ["status", "--server", "http://127.0.0.1:8000"])
    assert result.exit_code == 1 and "manually" in result.output


def test_status_rejects_service_switch_between_health_and_runtime(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def service_health(self): return {"database_id": "same", "service_id": "first"}
        def get_runtime(self, pid): return {"database_id": "same", "service_id": "second"}
    monkeypatch.setattr(submissions, "CairnClient", Client)
    with pytest.raises(ValueError, match="instance changed"):
        submissions.inspect_prepared("http://127.0.0.1:8000", project_id="p")


def test_skill_wrapper_is_portable_and_does_not_offer_service_management(tmp_path):
    help_result = subprocess.run([sys.executable, str(WRAPPER), "status", "--help"], cwd=tmp_path,
                                 capture_output=True, text=True, timeout=10)
    assert help_result.returncode == 0 and "--run-dir" in help_result.stdout
    for command in ("up", "serve", "dispatch", "start", "builder", "audit"):
        result = subprocess.run([sys.executable, str(WRAPPER), command, "--help"], cwd=tmp_path,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode != 0 and "用户手动管理" in result.stderr
    assert not list(tmp_path.iterdir())


def test_skill_package_matches_its_tracked_sources():
    with zipfile.ZipFile(ROOT / ".claude/artifacts/cairn-builder.skill") as package:
        sources = [path for path in SKILL.rglob("*") if path.is_file()]
        assert set(package.namelist()) == {str(path.relative_to(SKILL.parent)) for path in sources}
        for path in sources:
            assert package.read(str(path.relative_to(SKILL.parent))) == path.read_bytes()


SERVICE_CLI = r'''
import fcntl, json, os, pathlib, sys, time
if "--help" in sys.argv:
    raise SystemExit(0)
assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
prompt = sys.argv[-1]
context = {}
decoder = json.JSONDecoder()
for offset, char in enumerate(prompt):
    if char != "{": continue
    try: value, _ = decoder.raw_decode(prompt[offset:])
    except ValueError: continue
    if isinstance(value, dict) and "repo_path" in value and "fixed_contract" in value:
        context = value
assert context, "fixture expects the existing controller/research context"
schema = context.get("output_schema", {}).get("title")
phase = {"PreflightResult":"preflight", "SeedReviewResult":"seed_review", "NodeAssessment":"overseer",
         "FinalAssessment":"final_review", "ClosingResult":"closing"}.get(schema)
if phase is None:
    phase = "explore" if "执行一个代码审计探索" in prompt else "reason"
with pathlib.Path(os.environ["CP4_CALLS"]).open("a") as log:
    fcntl.flock(log, fcntl.LOCK_EX)
    log.write(json.dumps({"phase":phase,"pid":os.getpid(),"session":sys.argv[sys.argv.index("--session-id")+1]})+"\n")
evidence = {"path":"module/install/control.php","start_line":2,"end_line":2}
if phase in ("preflight", "seed_review", "explore"):
    assert "requireLogin" in (pathlib.Path(context["repo_path"])/evidence["path"]).read_text()
if phase == "preflight":
    issues = ["keep the static deployment uncertainty"] if context["fixed_contract"]["goal"] == "revise-me" else []
    answer = {"approved":not issues,"issues":issues}
elif phase == "seed_review":
    answer = {"reviews":[{"key":seed["key"],"audit":{"review_status":"verified","evidence":[evidence],
        "conditions":["synthetic static source"],"validation_level":"source_analysis"}} for seed in context["seeds"]]}
elif phase == "explore":
    release = pathlib.Path(os.environ["CP4_RELEASE"])
    while not release.exists(): time.sleep(.02)
    answer = {"accepted":True,"data":{"description":"CP4 independent static observation: requireLogin appears on line 2",
        "audit":{"review_status":"unverified","evidence":[evidence],"conditions":["synthetic static source"],
                 "premise_ids":[],"validation_level":"source_analysis"}}}
elif phase == "overseer":
    target = context["target"]
    answer = {"decision":"approved","reason":"synthetic source reviewed","hint":None,
              "fact_audit":{**target["audit"],"review_status":"verified"} if "audit" in target else None}
elif phase == "final_review":
    answer = {"decision":"approved","reason":"synthetic fixed-policy source judgement","proof_ids":context["request"]["from"],
              "checks":dict.fromkeys(["effect","deployment","attacker_capabilities","scope","cvss","evidence"],True),"cvss_vector":None}
elif phase == "closing":
    answer = {"kind":"no_candidate","from":[],"description":"the fixture waits for an explicit research candidate"}
else:
    proof = context.get("candidate_fact_ids", [])[1:]
    answer = {"accepted":True,"data":{"complete":{"from":proof[:1],"description":"synthetic static goal supported"}}} if proof else {"accepted":False,"reason":"wait for independent exploration"}
print(json.dumps(answer))
'''


def test_skill_client_revision_pause_resume_and_independent_completion(probe, tmp_path, monkeypatch):
    config, prepared, plan = configured(probe)
    config.controller.budget.research_seconds = 90
    config.controller.budget.drain_seconds = 15
    config.controller.budget.final_review_seconds = 20
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config.server = f"http://127.0.0.1:{sock.getsockname()[1]}"
    config_path = tmp_path / "service.json"
    config_path.write_text(config.model_dump_json())
    folder = Path(prepared["run_dir"])
    plan["goal"] = "revise-me"
    Path(prepared["plan"]).write_text(json.dumps(plan))
    calls, release = tmp_path / "calls.jsonl", tmp_path / "release"
    binary = tmp_path / "bin/claude"
    binary.write_text(f"#!{sys.executable}\n"+SERVICE_CLI)
    binary.chmod(0o755)
    monkeypatch.setenv("CP4_CALLS", str(calls))
    monkeypatch.setenv("CP4_RELEASE", str(release))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    caller = tmp_path / "caller"
    caller.mkdir()
    log = (tmp_path / "service.log").open("w")
    service = subprocess.Popen([*CLI, "up", "--config", str(config_path), "--db-path", str(tmp_path/"managed.db")],
                               cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
    def records(): return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    def execute(action, *extra):
        result = subprocess.run([sys.executable,str(WRAPPER),action,"--server",config.server,
                                 "--run-dir",str(folder),*extra],cwd=caller,capture_output=True,text=True,timeout=30)
        assert result.returncode == 0, result.stdout+result.stderr+(tmp_path/"service.log").read_text()
        return json.loads(result.stdout)
    try:
        with closing(CairnClient(config.server, timeout=2, trust_env=False)) as client:
            def ready():
                try: return (h if (h := client.service_health())["ready"] else None)
                except requests.RequestException: return None
            health = wait_until(ready)
            rejected = execute("submit", "--config", str(config_path))
            assert rejected["status"] == "needs_revision" and not client.list_projects()
            assert execute("status")["submission"]["issues"] == rejected["issues"]
            plan["goal"] = "independently describe the fixed source condition; deployment remains unknown"
            Path(prepared["plan"]).write_text(json.dumps(plan))
            submitted = execute("submit", "--config", str(config_path))
            assert submitted["submission_id"] == rejected["submission_id"] and submitted["revision"] == 2
            pid = submitted["project_id"]
            wait_until(lambda: any(row["phase"] == "explore" for row in records()))
            execute("pause")
            paused = wait_until(lambda: (r if (r := client.get_runtime(pid))["state"] == "paused" else None))
            assert execute("status")["runtime"]["state"] == "paused"
            execute("resume")
            wait_until(lambda: len([row for row in records() if row["phase"] == "explore"]) >= 2)
            assert client.get_runtime(pid)["generation"] == 2
            assert client.get_runtime(pid)["elapsed_seconds"] >= paused["elapsed_seconds"]
            release.touch()
            wait_until(lambda: client.get_project(pid).project.status == "completed", seconds=35)
            wait_until(lambda: (folder/"research.json").exists() and json.loads((folder/"research.json").read_text()).get("status") == "completed")
            state = execute("status")
            assert state["runtime"]["state"] == "finished"
            assert state["submission"]["status"] == "completed"
            assert len(client.list_projects()) == 1
            assert client.service_health()["service_id"] == health["service_id"] and service.poll() is None
            saved = json.loads((folder/"research.json").read_text())
            assert set(saved["results"]) == {"project","graph","timeline","audit","completions"}
            assert json.loads((folder/"result.json").read_text())["project"]["status"] == "completed"
            completion = json.loads((folder/"completions.json").read_text())[0]
            assert completion["status"] == "approved"
            audit_records = json.loads((folder/"audit.json").read_text())["records"]
            phases = {row["phase"] for row in audit_records}
            assert {"preflight","seed_review","final_review"} <= phases
            assert all(row["object_type"] == "intent" for row in audit_records if row["phase"] == "overseer")
            explored = [row for row in records() if row["phase"] == "explore"]
            assert explored[0]["session"] != explored[1]["session"]
            events = [row["event"] for row in state["runtime"]["history"]]
            assert {"pausing","paused","resuming","finished"} <= set(events)
            assert not (folder/"launch.json").exists() and not (folder/"cairn.db").exists()
    finally:
        if service.poll() is None:
            service.send_signal(signal.SIGTERM)
            try: service.wait(timeout=20)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=5)
        stop_agent_groups(tmp_path/"managed-service/agent-processes", {service.pid})
        release.touch()
        log.close()
