from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

import pytest
import yaml

from cairn.audit_evidence import assert_repository_unchanged, source_evidence
from cairn.audit_inputs import agent_graph_yaml, agent_view
from cairn.controller.audit import AuditPlan
from cairn.controller.kritt import KrittView
from cairn.controller.kritt_query import write_json
from cairn.controller.kritt_source import verify_source
from cairn.controller.research import import_audit_plan, run_research
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from test_audit_workflow import api, install_cli
from test_kritt_probe import probe


def ready(probe):
    config, _, view_path, folder = probe
    config.controller.research_prompt_group = "audit"
    config.controller.max_goal_revisions = 1
    config.controller.timeouts.preflight = config.controller.timeouts.seed_review = 12
    config.controller.timeouts.overseer = 12
    config.controller.poll_interval = 1
    folder.mkdir()
    target = config.controller.kritt.targets["test"]
    repo = Path(target.source_root)
    extra = repo / ".venv" / "guard.py"
    extra.parent.mkdir()
    extra.write_text("# archived source, not a runtime environment\n")
    manifest_path = Path(target.source_manifest)
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][".venv/guard.py"] = hashlib.sha256(extra.read_bytes()).hexdigest()
    manifest["tracked_count"] = len(manifest["files"])
    manifest_path.write_text(json.dumps(manifest))
    view = KrittView.load(view_path)
    baseline = verify_source(repo, view, manifest_path)
    plan = {
        "title": "Kritt 静态研究", "origin": "固定仓库版本与用户静态阅读要求", "goal": "核对入口源码中的权限条件",
        "policy": {"effect": "描述源码条件", "deployment": "合成源码", "attacker_capabilities": ["仅静态阅读"],
                   "scope": ["module/install/control.php"], "cvss": {}, "evidence_requirements": ["真实源码引用"],
                   "threat_model": [{"claim": "部署状态未确认", "kind": "unknown", "source": "未提供部署环境"}]},
        "seeds": [{"key": "entry", "description": "入口源码含requireLogin", "audit": {"review_status": "unverified"}}],
        "intents": [{"from": ["entry"], "description": "继续核对权限条件"}], "hints": [],
    }
    write_json(folder / "plan.json", plan)
    write_json(folder / "source-baseline.json", baseline)
    write_json(folder / "selected-materials.json", [{"origin": "synthetic Kritt material"}])
    write_json(folder / "query-receipts.json", {"view_digest": view.digest, "queries": [], "receipts": []})
    write_json(folder / "requirements.json", {"user_goal": "静态阅读", "messages": [{"role": "user", "content": "保留部署未知"}]})
    write_json(folder / "references.json", [])
    view.save(folder / "kritt-view.json")
    write_json(folder / "kritt-context.json", {"view_path": str(folder / "kritt-view.json"), "view_digest": view.digest,
        "queries_dir": str(folder / "queries"), "max_rows": 20000, "max_bytes": 33554432,
        "response_rows": 20, "response_bytes": 65536})
    prepared = {"run_dir": str(folder), "source_root": str(repo), "source_baseline": str(folder / "source-baseline.json"),
        "plan": str(folder / "plan.json"), "materials": str(folder / "selected-materials.json"),
        "requirements": str(folder / "requirements.json"), "references": str(folder / "references.json"),
        "query_context": str(folder / "kritt-context.json"), "repo_full": view.scan["repo_full"], "scan_id": 19,
        "commit_sha": view.scan["commit_sha"], "user_goal": "静态阅读", "target": "test", "calls": 1,
        "builder_session": "a7f36cc2-4914-4cbb-8d8f-d9e11795fd08"}
    config.local = LocalConfig(workspace_root=str(folder / "work"))
    return config, prepared, plan, baseline


def seed_review():
    return {"reviews": [{"key": "entry", "audit": {"review_status": "verified",
        "evidence": [{"path": "/workspace/module/install/control.php", "start_line": 2}],
        "conditions": ["仅源码观察"], "validation_level": "source_analysis"}}]}


def test_prepared_plan_uses_audit_without_another_build_and_keeps_archive_rules(probe, api, monkeypatch):
    config, prepared, _, baseline = ready(probe)
    client, _ = api
    calls = install_cli(Path(prepared["run_dir"]).parent, monkeypatch,
                        [{"approved": True, "issues": []}, seed_review()])
    result = import_audit_plan(config, client, LocalBackend(config.local), prepared)
    project = client.get_project(result["project_id"])
    assert project.project.mode == "audit" and project.project.status == "active"
    assert project.project.audit.baseline == baseline and ".venv/guard.py" in baseline["files"]
    assert [record.phase for record in client.get_audit(result["project_id"]).records] == ["preflight", "seed_review"]
    lines = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(lines) == 2 and all("--session-id" in line["argv"] for line in lines)
    assert all('"files"' not in line["argv"][-1] for line in lines)
    fact = next(f for f in project.facts if f.id == result["seed_ids"]["entry"])
    assert fact.audit.evidence[0].path == "module/install/control.php"
    context = json.loads((Path(result["workspace"]) / "input.json").read_text())
    assert set(context["materials"]) == {"kritt-materials.json", "references.json", "requirements.json", "query-receipts.json", "research-context.json"}
    assert all(Path(path).exists() for path in context["materials"].values())
    assert json.loads(Path(context["materials"]["research-context.json"]).read_text())["commit_sha"] == prepared["commit_sha"]
    assert_repository_unchanged(Path(prepared["source_root"]), baseline, Path(config.local.workspace_root))
    Path(prepared["source_root"], ".venv/guard.py").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        assert_repository_unchanged(Path(prepared["source_root"]), baseline, Path(config.local.workspace_root))


def test_preflight_revision_returns_to_original_builder_and_preserves_requirements(probe, api, monkeypatch):
    config, prepared, original, _ = ready(probe)
    revised = {**original, "action": "finish", "goal": "核对源码权限条件，部署状态保持未知"}
    calls = install_cli(Path(prepared["run_dir"]).parent, monkeypatch,
                        [{"approved": False, "issues": ["显式保留部署未知"]}, revised,
                         {"approved": True, "issues": []}, seed_review()])
    result = import_audit_plan(config, api[0], LocalBackend(config.local), prepared)
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert recorded[1]["argv"][0:2] == ["-r", prepared["builder_session"]]
    revised_input = json.loads(recorded[1]["argv"][-1].rsplit("\n\n", 1)[1])
    assert revised_input["plan"]["goal"] == original["goal"]
    assert revised_input["preflight_issues"] == ["显式保留部署未知"]
    assert Path(revised_input["requirements_path"]).exists()
    assert len({line["argv"][1] for line in recorded if line["argv"][0] == "--session-id"}) == 3
    assert api[0].get_project(result["project_id"]).project.audit.user_goal == "静态阅读"
    assert json.loads(Path(prepared["run_dir"], "work", result["project_id"], "research-context.json").read_text())["goal"] == revised["goal"]


def test_agent_inputs_strip_real_scan19_fingerprints_without_changing_source_metadata():
    manifest = Path(__file__).resolve().parents[3] / ".claude/kritt-sources/d5bfff8c91cea48018275381e0a127ed6fa5c79f/source-manifest-case-sensitive.json"
    files = json.loads(manifest.read_text())["files"] if manifest.exists() else {f"file-{n}.php": "b"*64 for n in range(20085)}
    baseline = {"files": files, "digest": "fixed", "source_manifest": {"commit_sha": "a"*40}}
    policy = {"scope": ["entry"], "threat_model": [{"baseline": {"deployment": "unchanged constraint"}}]}
    raw = {"project": {"mode": "audit", "audit": {"baseline": baseline, "policy": policy}}, "facts": [{"id": "f001", "audit": {"conditions": ["unknown"]}}]}
    for context in [{"baseline": baseline, "plan": {"goal": "source"}}, {"baseline": baseline, "target": {"id": "f001"}}, {"graph": raw, "request": {"id": "c1"}}]:
        output = agent_view(context)
        assert '"files"' not in json.dumps(output) and len(json.dumps(output)) < 2000
    graph = yaml.safe_load(agent_graph_yaml(yaml.safe_dump(raw)))
    assert "files" not in graph["project"]["audit"]["baseline"]
    assert graph["project"]["audit"]["baseline"]["git_commit"] == "a"*40
    assert graph["facts"] == raw["facts"] and raw["project"]["audit"]["baseline"]["files"] is files
    assert graph["project"]["audit"]["policy"] == policy


@pytest.mark.parametrize("stop_after", ["reviewed_fact", "draining", "completed", "closing_completed", "budget_exhausted"])
def test_owned_audit_dispatcher_and_monitor_write_and_review_a_new_fact(probe, monkeypatch, stop_after):
    config, prepared, _, _ = ready(probe)
    config.server = "http://127.0.0.1:0"
    config.runtime.interval = 1
    config.runtime.max_workers = config.runtime.max_project_workers = 1
    config.tasks.reason.timeout = config.tasks.explore.timeout = 12
    if stop_after == "draining":
        config.controller.budget.research_seconds = 2
        config.controller.budget.drain_seconds = 20
        monkeypatch.setenv("RECONNECT_EXPLORE_DELAY", "3")
    if stop_after in {"closing_completed", "budget_exhausted"}:
        config.controller.budget.research_seconds = 4
        config.controller.budget.drain_seconds = 20
        config.controller.budget.final_review_seconds = 30
        monkeypatch.setenv("RECONNECT_EXPLORE_DELAY", "5")
    monkeypatch.setenv("RECONNECT_OUTCOME", stop_after)
    if stop_after == "completed":
        monkeypatch.setenv("RECONNECT_COMPLETE", "1")
        from cairn.controller import research
        original_client = research.CairnClient

        def client_with_one_stale_read(*args, **kwargs):
            client = original_client(*args, **kwargs)
            get = client.get_project
            stale = False

            def read(project_id):
                nonlocal stale
                project = get(project_id)
                if not stale and project.project.status == "completed":
                    stale = True
                    time.sleep(.3)  # Let the monitor exit after this now-stale snapshot.
                    project = project.model_copy(deep=True)
                    project.project.status = "active"
                return project

            client.get_project = read
            return client

        monkeypatch.setattr(research, "CairnClient", client_with_one_stale_read)
    folder = Path(prepared["run_dir"])
    binary = folder.parent / "bin" / "claude"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, re, sys, time, yaml
prompt = sys.argv[-1]
assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
if '"output_schema"' in prompt:
    context = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert '"files"' not in json.dumps(context)
    props = context["output_schema"].get("properties", {})
    if "approved" in props:
        result = {"approved": True, "issues": []}
    elif "reviews" in props:
        result = {"reviews": [{"key": seed["key"], "audit": {"evidence": [{"path": "module/install/control.php", "start_line": 2}], "conditions": ["仅源码观察"], "review_status": "verified", "validation_level": "source_analysis"}} for seed in context["plan"]["seeds"]]}
    elif "target" in context:
        target = context["target"]
        fact = target.get("audit")
        result = {"decision": "approved", "reason": "checked source", "fact_audit": None, "hint": None}
        if fact is not None:
            result["fact_audit"] = {**fact, "review_status": "verified", "validation_level": "source_analysis", "review_notes": "matched source"}
            if os.environ.get("RECONNECT_OUTCOME") == "budget_exhausted":
                result.update(decision="needs_evidence", reason="insufficient source proof")
                result["fact_audit"]["review_status"] = "unverified"
    elif "request" in context:
        result = {"decision": "approved", "reason": "static source checked", "proof_ids": context["request"]["from"],
                  "checks": {key: True for key in ["effect", "deployment", "attacker_capabilities", "scope", "cvss", "evidence"]}, "cvss_vector": None}
    elif "kind" in props:
        seeds = set(context["graph"]["project"]["audit"]["seed_ids"].values())
        facts = [fact["id"] for fact in context["graph"]["facts"]
                 if fact["id"] not in {"origin", "goal", *seeds}
                 and fact.get("audit", {}).get("review_status") == "verified"]
        candidate = os.environ.get("RECONNECT_OUTCOME") == "closing_completed" and bool(facts)
        result = {"kind": "candidate" if candidate else "no_candidate", "from": facts[:1] if candidate else [],
                  "description": "assess only the final graph"}
    else:
        raise AssertionError("No closing/final review should be needed in this stopped test")
else:
    graph_path = re.search(r"/tmp/cairn-prompts/[^\s]+/graph.yaml", prompt).group()
    graph = yaml.safe_load(pathlib.Path(graph_path).read_text())
    assert graph["project"]["mode"] == "audit" and "files" not in graph["project"]["audit"]["baseline"]
    if "审计研究判断" in prompt:
        seeds = set(graph["project"]["audit"]["seed_ids"].values())
        proved = [fact for fact in graph["facts"] if fact["id"] not in {"origin", "goal", *seeds} and fact.get("audit", {}).get("review_status") == "verified"]
        if proved and os.environ.get("RECONNECT_COMPLETE"):
            data = {"complete": {"from": [proved[0]["id"]], "description": "静态源码已观察"}}
        else:
            data = {"intents": [{"from": [graph["facts"][-1]["id"]], "description": "继续检查静态条件"}]}
    else:
        time.sleep(float(os.environ.get("RECONNECT_EXPLORE_DELAY", "0")))
        text = pathlib.Path("repo/module/install/control.php").read_text()
        assert "requireLogin" in text
        materials = json.loads(pathlib.Path("kritt-materials.json").read_text())
        assert materials
        data = {"description": "新研究确认源码调用requireLogin", "audit": {"evidence": [{"path": "module/install/control.php", "start_line": 2}], "conditions": ["仅源码观察"], "premise_ids": [], "review_status": "unverified", "validation_level": "source_analysis"}}
    result = {"accepted": True, "data": data}
print(json.dumps(result))
''')
    binary.chmod(0o755)
    token = TaskCancellation()
    observed, failures = [], []
    watcher = None

    def watch(server, project_id):
        client = CairnClient(server, trust_env=False)
        try:
            until = time.monotonic() + 25
            while time.monotonic() < until:
                project = client.get_project(project_id)
                seeds = set(project.project.audit.seed_ids.values())
                new = [fact for fact in project.facts if fact.id not in {"origin", "goal", *seeds}]
                if stop_after == "draining" and project.project.status == "draining":
                    observed.append("draining")
                    assert client.set_project_status(project_id, "stopped").ok
                    return
                if stop_after == "reviewed_fact" and any(fact.audit.review_status == "verified" for fact in new):
                    observed.extend(new)
                    assert client.set_project_status(project_id, "stopped").ok
                    return
                time.sleep(.05)
            token.cancel("test did not observe an independently reviewed fact")
        except BaseException as exc:
            failures.append(str(exc))
            token.cancel("watch failed")
        finally:
            client.close()

    def report(event):
        nonlocal watcher
        if stop_after in {"reviewed_fact", "draining"} and watcher is None and event.get("project_id"):
            watcher = threading.Thread(target=watch, args=(event["server"], event["project_id"]))
            watcher.start()

    timer = threading.Timer(35, token.cancel, args=("test deadline",))
    timer.start()
    try:
        result = run_research(config, prepared, cancellation=token, report=report)
    finally:
        timer.cancel();timer.join()
        if watcher is not None:
            watcher.join(timeout=5)
    expected = "completed" if stop_after in {"completed", "closing_completed"} else (
        "budget_exhausted" if stop_after == "budget_exhausted" else "stopped")
    assert result["status"] == result["project_status"] == expected and not failures
    assert set(result["processes"]) == {"server", "dispatcher", "controller"}
    assert all(item["returncode"] is not None for item in result["processes"].values())
    stored = json.loads((folder / "result.json").read_text())
    assert stored["project"]["mode"] == "audit" and stored["project"]["status"] == result["status"]
    if stop_after == "reviewed_fact":
        assert any(fact["id"] == observed[0].id for fact in stored["facts"])
    elif stop_after == "draining":
        assert observed == ["draining"]
    audit = json.loads((folder / "audit.json").read_text())
    completions = json.loads((folder / "completions.json").read_text())
    assert audit["phase"] == result["audit_phase"]
    assert audit["termination_reason"] == result["termination_reason"]
    assert set(result["results"]) == {"project", "graph", "timeline", "audit", "completions"}
    if expected == "completed":
        approved = next(request for request in completions if request["status"] == "approved")
        assert approved["review_session"] != approved["source_session"]
        assert any(record["phase"] == "final_review" and record["decision"] == "approved" for record in audit["records"])
        assert any(intent["to"] == "goal" for intent in stored["intents"])
        if stop_after == "closing_completed":
            assert audit["closing"]["request_id"] == approved["id"]
            assert audit["closing"]["result"]["kind"] == "candidate"
    else:
        assert not any(request["status"] == "approved" for request in completions)
        assert not any(intent["to"] == "goal" for intent in stored["intents"])
    if stop_after in {"closing_completed", "budget_exhausted"}:
        seeds = set(audit["seed_ids"].values())
        assert any(fact["id"] not in {"origin", "goal", *seeds} for fact in stored["facts"])
        assert audit["closing"]["status"] == "success"
    for process in result["processes"].values():
        with pytest.raises(ProcessLookupError):
            os.kill(process["pid"], 0)
    assert list((folder / "work" / result["project_id"] / "controller-reviews").glob("overseer-*/result.json"))


def test_monitor_exit_stops_owned_audit_and_reaps_services(probe, monkeypatch):
    from cairn.controller import research
    config, prepared, _, _ = ready(probe)
    config.server = "http://127.0.0.1:0"
    folder = Path(prepared["run_dir"])
    install_cli(folder.parent, monkeypatch, [{"approved": True, "issues": []}, seed_review()])
    shim = folder / "cli.py"
    shim.write_text("import sys\nif sys.argv[1] == 'audit': raise SystemExit(7)\nfrom cairn.cli import main\nmain(args=sys.argv[1:])\n")
    monkeypatch.setattr(research, "CLI", [sys.executable, str(shim)])
    with pytest.raises(RuntimeError, match="monitor exited"):
        run_research(config, prepared)
    state = json.loads((folder / "research.json").read_text())
    assert state["status"] == "failed" and state["processes"]["controller"]["returncode"] == 7
    assert all(item["returncode"] is not None for item in state["processes"].values())
    assert json.loads((folder / "result.json").read_text())["project"]["status"] == "stopped"
    assert state["project_status"] == "stopped"
    assert json.loads((folder / "audit.json").read_text())["project_id"] == state["project_id"]
    assert json.loads((folder / "completions.json").read_text()) == []


def test_overseer_input_identifies_unsupported_premises_without_changing_server_context(probe, api, monkeypatch):
    from cairn.controller import monitor
    from cairn.controller.models import AgentCallResult
    from cairn.dispatcher.runtime.process import ProcessResult
    config, prepared, _, _ = ready(probe)
    client = api[0]
    install_cli(Path(prepared["run_dir"]).parent, monkeypatch, [{"approved": True, "issues": []}, seed_review()])
    result = import_audit_plan(config, client, LocalBackend(config.local), prepared)
    project = client.get_project(result["project_id"])
    captured = []

    def call(*args, **kwargs):
        captured.append(json.loads(kwargs["prompt"].rsplit("\n\n", 1)[1]))
        data = {"decision": "needs_evidence", "reason": "必要前提未确认", "fact_audit": {"review_status": "unverified"}}
        return AgentCallResult(role="reviewer", worker="test-worker", phase="overseer", workspace=str(kwargs["workspace"]),
            session="independent-review", prompt=kwargs["prompt"], status="success", process=ProcessResult(0, "", ""),
            response=json.dumps(data), data=data, elapsed_seconds=.01)

    monkeypatch.setattr(monitor, "call_agent", call)
    context = {"target": {"id": "f100", "audit": {"evidence": [], "conditions": [], "premise_ids": ["f002"]}},
               "premises": {"f002": {"description": "pending", "audit": {"review_status": "unverified"}}},
               "baseline": project.project.audit.baseline, "policy": project.project.audit.policy}
    called, _ = monitor._invoke(config, client, LocalBackend(config.local), project, phase="overseer", context=context,
                               model=monitor.NodeAssessment, timeout=10, cancellation=TaskCancellation())
    assert called.status == "success" and captured[0]["unsupported_premise_ids"] == ["f002"]
    assert "files" not in captured[0]["baseline"] and "files" in context["baseline"]
    assert captured[0]["target"]["audit"]["premise_ids"] == ["f002"]
