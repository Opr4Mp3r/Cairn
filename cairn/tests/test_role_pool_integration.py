"""Exercise pool dispatch against a real Server and two local CLI subprocesses."""
from collections import Counter
from concurrent.futures import wait
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import yaml

from cairn.controller.local import CLI, start_server, stop_processes
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.models import ReasonCheckpoint
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.tasks import common
from cairn.server.models import CreateProjectRequest


FAKE_CLI = r'''
import fcntl, json, os, pathlib, re, sys, time
import yaml

if "--help" in sys.argv:
    raise SystemExit(0)
assert "--model" not in sys.argv and "-m" not in sys.argv
session = sys.argv[sys.argv.index("--session-id") + 1]
harness = "claudecode" if pathlib.Path(sys.argv[0]).name == "claude" else "grok"
prompt = sys.argv[-1]
intent = re.search(r"## Current Intent\n```\n([^\n]+)\n```", prompt).group(1)
description = re.search(r"## Current Intent Description\n```\n([^\n]+)\n```", prompt).group(1)
graph_path = re.search(r"The graph YAML snapshot is stored in this task file:\n\n([^\n]+)", prompt).group(1)
graph = yaml.safe_load(pathlib.Path(graph_path).read_text())
record = {"harness": harness, "session": session, "intent": intent,
          "project": pathlib.Path.cwd().name, "cwd": os.getcwd()}
assert graph["project"]["title"] == "角色池并发"
assert description in {row["description"] for row in graph["intents"]}
root = pathlib.Path(os.environ["CAIRN_POOL_TEST_DIR"])

def event(kind):
    with (root / "calls.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (root / "calls.jsonl").open("a") as log:
            log.write(json.dumps({"event": kind, **record}) + "\n")

event("start")
deadline = time.monotonic() + 15
while not (root / "release").exists():
    assert time.monotonic() < deadline, "Explore pool did not fill before test deadline"
    time.sleep(0.02)
event("finish")
answer = json.dumps({"accepted": True, "data": {"description": json.dumps(record)}})
print(json.dumps({"text": answer}) if harness == "grok" else answer)
'''


def test_same_project_explore_fills_three_claude_and_two_grok_slots(tmp_path, monkeypatch):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("claude", "grok"):
        binary = binaries / name
        binary.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CAIRN_POOL_TEST_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))
    config = DispatchConfig.model_validate({
        "server": "http://127.0.0.1:0",
        "runtime": {"execution": "local", "worker_healthcheck": "disabled",
                    "max_running_projects": 1, "interval": 1, "healthcheck_timeout": 5,
                    "prompt_group": "default"},
        "local": {"workspace_root": str(tmp_path / "work")},
        "tasks": {"bootstrap": {"timeout": 20, "conclude_timeout": 5},
                  "reason": {"timeout": 20}, "explore": {"timeout": 20, "conclude_timeout": 5}},
        "role_pools": {"explore": {"claudecode": 3, "grok": 2},
                       **{role: {"claudecode": 1} for role in
                          ("reason", "preflight", "seed_review", "overseer", "closing", "final_review")}},
    })
    processes, logs = {}, []
    loop = None

    def spawn(name, args, **kwargs):
        log = (tmp_path / f"{name}.log").open("w")
        logs.append(log)
        processes[name] = subprocess.Popen(CLI + args, stdout=log, stderr=log,
                                           start_new_session=True, **kwargs)
        return processes[name]

    def records():
        path = tmp_path / "calls.jsonl"
        with (tmp_path / "calls.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    try:
        effective = start_server(config, tmp_path, spawn)
        loop = DispatcherLoop(effective)
        loop.run_startup_healthchecks()
        response = loop.client.create_project(CreateProjectRequest(
            title="角色池并发", origin="固定输入", goal="每个方向写回自己的结果", bootstrap_enabled=False,
        ))
        assert response.ok, response.text
        project_id = response.data["project"]["id"]
        intent_ids = set()
        for index in range(10):
            response = loop.client.create_intent(project_id, ["origin"], f"独立方向 {index}", "test")
            assert response.ok, response.text
            intent_ids.add(response.data["id"])

        for wave in range(2):
            release = tmp_path / "release"
            release.unlink(missing_ok=True)
            project = loop.client.get_project(project_id)
            loop.reason_checkpoints[project_id] = ReasonCheckpoint(
                fact_count=len(project.facts), hint_count=len(project.hints),
                open_intent_count=sum(intent.to is None for intent in project.intents),
            )
            loop._dispatch_available(loop.client.list_projects())
            deadline = time.monotonic() + 10
            while len([row for row in records() if row["event"] == "start"]) < (wave + 1) * 5:
                assert time.monotonic() < deadline, (records(), [f.result() for f in loop.futures if f.done()])
                loop._dispatch_available(loop.client.list_projects())
                time.sleep(0.02)
            started = [row for row in records() if row["event"] == "start"]
            assert Counter(row["harness"] for row in started[wave * 5:]) == {"claudecode": 3, "grok": 2}
            assert len(loop.futures) == 5
            loop._dispatch_available(loop.client.list_projects())
            time.sleep(0.1)
            assert len([row for row in records() if row["event"] == "start"]) == (wave + 1) * 5
            assert len(loop.futures) == 5
            release.touch()
            done, pending = wait(loop.futures, timeout=10)
            assert not pending
            assert {future.result() for future in done} == {"success"}
            loop._reap_futures()

        history = records()
        active, peak = Counter(), Counter()
        peak_total = 0
        for row in history:
            active[row["harness"]] += 1 if row["event"] == "start" else -1
            peak[row["harness"]] = max(peak[row["harness"]], active[row["harness"]])
            peak_total = max(peak_total, sum(active.values()))
            assert active["claudecode"] <= 3 and active["grok"] <= 2
        assert peak == {"claudecode": 3, "grok": 2} and peak_total == 5
        assert not any(active.values())
        started = [row for row in history if row["event"] == "start"]
        assert len(started) == len({row["session"] for row in started}) == 10
        assert {row["intent"] for row in started} == intent_ids
        assert {row["project"] for row in started} == {project_id}
        assert {row["cwd"] for row in started} == {str(tmp_path / "work" / project_id)}
        project = loop.client.get_project(project_id)
        facts = {fact.id: fact for fact in project.facts}
        by_intent = {row["intent"]: {key: value for key, value in row.items() if key != "event"} for row in started}
        harness_by_worker = {worker.name: worker.type for worker in loop.config.pool_workers("explore")}
        assert len(project.facts) == 12
        for intent in project.intents:
            assert json.loads(facts[intent.to].description) == by_intent[intent.id]
            assert harness_by_worker[intent.worker] == by_intent[intent.id]["harness"]
        graph = yaml.safe_load(loop.client.export_project(project_id))
        assert graph["project"]["title"] == "角色池并发" and len(graph["facts"]) == 12
        assert {intent["to"] for intent in graph["intents"]} == set(facts) - {"origin", "goal"}
    finally:
        (tmp_path / "release").touch()
        if loop is not None:
            loop.cancel_running_tasks("integration test finished")
            loop.close()
        stop_processes(processes)
        for log in logs:
            log.close()
