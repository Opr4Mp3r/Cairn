"""Opt-in Claude Code examples over synthetic, non-security state transitions."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time

import pytest
import requests
import yaml

from cairn.audit_evidence import assert_source_unchanged, source_evidence
from cairn.controller.builder import prepare_builder
from cairn.controller.kritt import FORMATS, KrittView
from cairn.controller.kritt_query import collect_queries, run_query, write_json
from cairn.controller.local import CLI
from cairn.controller.research import save_results
from cairn.controller.submissions import submit_prepared
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.server.models import ClosingResult


ROOT = Path(__file__).resolve().parents[2]
GOAL = (
    "静态研究本地合成 sample.py 的状态衔接：按先创建一个 Display、调用 publish('after')、"
    "再调用该同一对象 render() 的固定顺序，确认输出能否从 before 变成 after。"
    "只有证明不重建 Display、不重启、不修改源码即可得到 after 才满足 Goal；"
    "若有阻断保留具体反证，不以证明不可能完成。结合固定 Kritt 流程和源码，保留对象与生效时序。"
)


def prepare_example(folder: Path, *, cached: bool, server: str) -> tuple[DispatchConfig, dict]:
    repo = folder / "source"
    repo.mkdir(parents=True)
    read = 'self._initial["text"]' if cached else 'STORE["text"]'
    source = (
        'STORE = {"text": "before"}\n\n'
        'def publish(value):\n    STORE["text"] = value\n\n'
        'class Display:\n    def __init__(self):\n        self._initial = dict(STORE)\n\n'
        f'    def render(self):\n        return {read}\n'
    )
    (repo / "sample.py").write_text(source)
    commit = hashlib.sha1(source.encode()).hexdigest()
    manifest = folder / "source-manifest.json"
    write_json(manifest, {
        "format_version": 1, "repo_full": "examples/state-composition", "commit_sha": commit,
        "git_tree": "b" * 40, "archive_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "tracked_count": 1, "files": {"sample.py": hashlib.sha256(source.encode()).hexdigest()},
        "path_aliases": [],
    })
    entry = {
        "entrypoint_kind": "local_function", "entrypoint_label": "发布与展示样例",
        "entrypoint_handler": "publish / Display.render", "entrypoint_file_path": "sample.py",
        "entrypoint_actor": "按固定顺序调用的本地样例调用方", "entrypoint_input": "text",
        "entrypoint_reachability": "仅合成本地程序，不代表实际产品或网络可达性", "entrypoint_line": 3,
    }
    flows = [{
        "flow_summary": summary, "flow_terminal_action": action, "flow_file_path": "sample.py",
        "flow_input_to_sink": detail, "flow_security_relevance": "正常功能观察，不是漏洞报告",
        "flow_line": line, "flow_trace": [{"file_path": "sample.py", "line": line}],
    } for summary, action, detail, line in [
        ("发布值写入共享配置", "publish", "输入 value 写到 STORE，读取端是否使用该状态尚需研究", 3),
        ("展示读取已初始化对象的状态", "Display.render", "读取对象与生效时序应按实际源码核对", 10),
    ]]

    def row(identity, step, answer, parent=None):
        return {"id": identity, "scan_id": 1, "workflow_id": 1, "step_id": step,
                "partition_key": f"1-{step-1}", "prev_id": parent or 0,
                "prev_table": "workflows.step_results" if parent else None,
                "repeat_run": 1, "json_answer": answer}

    def job(identity, step, answers, parent=None):
        return {"id": identity, "scan_id": 1, "workflow_id": 1, "step_id": step, "kind": "step",
                "prev_id": parent or 0, "prev_table": "workflows.step_results" if parent else None,
                "repeat_run": 1, "status": "completed", "checked_out_commit": commit,
                "stub": False, "output_json": {"results": answers}}

    view = KrittView({
        "scan": {"id": 1, "workflow_id": 1, "repo_full": "examples/state-composition",
                 "commit_sha": commit, "status": "completed"},
        "workflow": {"id": 1, "step_ids": [1, 2, 3]},
        "steps": [{"id": n, "name": layer, "depth": n-1, "output_format": FORMATS[layer],
                   "output_table": "workflows.vulnerabilities" if layer == "finding" else "workflows.step_results",
                   "multi_output": True, "consume_all_previous": False}
                  for n, layer in enumerate(FORMATS, 1)],
        "step_results": [row(10, 1, entry), row(20, 2, flows[0], 10), row(21, 2, flows[1], 10)],
        "vulnerabilities": [], "post_process_metadata": [], "enrichments": [], "post_scripts": [],
        "step_metadata": [job(1, 1, [entry]), job(2, 2, flows, 10), job(3, 3, [], 20), job(4, 3, [], 21)],
    })
    view_path = folder / "view.json"
    view.save(view_path)
    data = yaml.safe_load((ROOT / "dispatch.service.example.yaml").read_text())
    data["server"] = server
    data["runtime"]["interval"] = data["controller"]["poll_interval"] = 1
    data["local"] = {"workspace_root": str(folder / "work")}
    data["controller"]["budget"] = {"research_seconds": 420, "drain_seconds": 60, "final_review_seconds": 480}
    data["controller"]["kritt"]["targets"] = {"sample": {
        "repo_full": "examples/state-composition", "source_root": str(repo), "source_manifest": str(manifest),
    }}
    config = DispatchConfig.model_validate(data)
    prepared = prepare_builder(config, target_name="sample", user_goal=GOAL, run_dir=folder / "run", view_path=view_path)
    # get20 also exposes the reading flow inside its generation metadata.
    # The example exercises retrieval, not discovery of previously withheld information.
    run_query(Path(prepared["query_context"]), {
        "action": "get", "ref": view.ref("workflows.step_results", 20, "1-1").model_dump(mode="json"),
    })
    collect_queries(Path(prepared["query_context"]))
    write_json(Path(prepared["plan"]), {
        "title": "合成状态衔接研究", "origin": "本地自有合成源码和同版本 Kritt 格式材料；只作静态研究",
        "goal": GOAL,
        "policy": {
            "effect": "固定顺序下，同一既有 Display.render() 返回 after",
            "deployment": "只按 sample.py 原始定义；Display 必须在 publish('after') 之前创建",
            "attacker_capabilities": ["本地调用方可创建一次 Display 并调用 publish 与 render；不更改源码"],
            "scope": ["sample.py；不重建对象、不重启、不扩展到实际产品或网络攻击"], "cvss": {},
            "evidence_requirements": ["真实源码行号、共享对象及前后状态、生效时序；条件必须保持"],
            "threat_model": [{"kind": "declared", "claim": "自有合成状态观察，不涉及漏洞定级", "source": "样例定义"}],
        },
        "seeds": [{"key": "publish", "description": "publish 将 value 写入 STORE；对既有读取对象的影响尚未确认"}],
        "intents": [{"from": ["publish"], "description":
            "先用固定 Kritt 查询入口寻找展示读取端的流程并读取完整材料，回到源码核对对象、状态和时序，"
            "研究发布与读取能否衔接。只保留源码支持的观察；不把写入共享配置等同于读取必然更新。"}],
        "hints": [],
    })
    write_json(folder / "config.json", config.model_dump(mode="json"))
    return config, prepared


def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.5)
    raise AssertionError("isolated example did not reach its expected state before the deadline")


def assert_blocked_example(project, audit, repo: Path) -> list[str]:
    """Check the new blocker against this self-authored fixture and its closing record."""
    assert project.project.status == "budget_exhausted"
    assert not any(r.phase == "final_review" and r.decision == "approved" for r in audit.records)
    closing = audit.closing
    assert closing.status == "success" and closing.result is not None and closing.result.kind == "no_candidate"
    assert any(r.phase == "closing" and r.attempt_status == "success" and r.session == closing.session
               and ClosingResult.model_validate(r.result) == closing.result for r in audit.records), "no successful closing evidence"

    # This executes only the self-authored sample, never an audited application.
    namespace = {}
    path = repo / "sample.py"
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    display = namespace["Display"]()
    assert display.render() == "before"
    namespace["publish"]("after")
    assert namespace["STORE"]["text"] == "after" and display.render() == "before"
    assert display._initial is not namespace["STORE"]

    seeds = set(project.project.audit.seed_ids.values())
    produced = {intent.to for intent in project.intents if intent.to and intent.concluded_at}
    blockers = []
    for fact in project.facts:
        data = fact.audit
        if fact.id in {"origin", "goal", *seeds} or fact.id not in produced or data is None:
            continue
        if data.review_status == "refuted" or not data.conditions or fact.id not in closing.result.description:
            continue
        try:
            checked = source_evidence(repo, data.evidence, project.project.audit.baseline)
        except (OSError, ValueError):
            continue
        if any(ref.sha256 != project.project.audit.baseline["files"].get(ref.path) for ref in checked):
            continue
        covered = {line for ref in checked if ref.path == "sample.py"
                   for line in range(ref.start_line or 0, (ref.end_line or ref.start_line or 0) + 1)}
        if {4, 8, 11} <= covered:  # write, independent snapshot, and actual reader
            blockers.append(fact.id)
    assert blockers, "no newly produced blocking observation with decisive source evidence and closing reference"
    return blockers


@pytest.mark.skipif(os.environ.get("CAIRN_COMPOSITION_REAL_CLAUDE") != "1", reason="explicit opt-in: calls the host Claude Code")
@pytest.mark.parametrize("cached", [False, True], ids=["live-state", "startup-snapshot"])
def test_real_claude_compositional_research(tmp_path, monkeypatch, cached):
    assert shutil.which("claude"), "configure Claude Code on this host before opting in"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config, prepared = prepare_example(tmp_path, cached=cached, server=server)
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "cairn/src"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    baseline = json.loads(Path(prepared["source_baseline"]).read_text())
    protected = {name: Path(prepared[name]).read_bytes() for name in ("view", "materials", "source_baseline", "plan")}
    with (tmp_path / "service.log").open("w") as log:
        process = subprocess.Popen([*CLI, "up", "--config", str(tmp_path / "config.json"),
                                    "--db-path", str(tmp_path / "cairn.db")],
                                   cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        try:
            with closing(CairnClient(server, timeout=5, trust_env=False)) as client:
                def ready():
                    assert process.poll() is None, (tmp_path / "service.log").read_text()
                    try:
                        return client.service_health().get("ready")
                    except requests.RequestException:
                        return False
                wait_for(ready, 30)
                submitted = submit_prepared(server, Path(prepared["run_dir"]), config_path=tmp_path / "config.json", wait=False)
                sid = submitted["submission_id"]
                previous = None
                project_id = None

                def finished():
                    nonlocal previous, project_id
                    current = client.get_submission(sid)
                    project_id = current.get("project_id")
                    state = current["status"]
                    if project_id:
                        project = client.get_project(project_id)
                        state = f"{state}/{project.project.status}/facts={len(project.facts)}"
                        if project.project.status in ("completed", "budget_exhausted"):
                            return project
                    if state != previous:
                        print(f"{tmp_path.name}: {state}", flush=True)
                        if project_id:
                            save_results(client, project_id, Path(prepared["run_dir"]))
                        previous = state
                    assert current["status"] not in ("error", "needs_revision", "blocked", "interrupted"), current
                    return None

                project = wait_for(finished, 1500)
                save_results(client, project.project.id, Path(prepared["run_dir"]))
                workspace = Path(project.project.audit.workspace_path)
                audit = client.get_audit(project.project.id)
                queries = [json.loads(path.read_text()) for path in (workspace / "research/kritt-queries").glob("q-*/result.json")]
                assert any(q["status"] == "success" and q["action"] == "get" and q["ref"]["id"] == 21 for q in queries)
                assert not any(record.phase == "overseer" and record.object_type == "fact" for record in audit.records)
                receipts = [json.loads(path.read_text()) for path in (workspace / "research").rglob("*_execute-*.json")]
                navigation = {Path(path) for receipt in receipts
                              for path in re.findall(r"^Graph navigation: (.+)$", receipt["prompt"], re.MULTILINE)}
                assert navigation
                for index in navigation:
                    assert index.is_file()
                    saved = tmp_path / "graph-snapshots" / index.parent.name
                    saved.mkdir(parents=True)
                    shutil.copyfile(index, saved / index.name)
                    shutil.copyfile(index.with_name("graph.yaml"), saved / "graph.yaml")
                assert list(workspace.rglob("reason-memory-*.json"))
                blocking_fact_ids = []
                if cached:
                    blocking_fact_ids = assert_blocked_example(project, audit, Path(prepared["source_root"]))
                else:
                    assert project.project.status == "completed"
                    assert any(record.phase == "final_review" and record.decision == "approved" for record in audit.records)
                assert_source_unchanged(Path(prepared["source_root"]), baseline)
                assert protected == {name: Path(prepared[name]).read_bytes() for name in protected}
                write_json(tmp_path / "observation.json", {
                    "cached": cached, "status": project.project.status, "project_id": project.project.id,
                    "facts": len(project.facts), "queries": len(queries), "source_and_inputs_unchanged": True,
                    "real_harness": "claudecode", "workspace": str(workspace),
                    "blocking_fact_ids": blocking_fact_ids,
                    "scope": "synthetic static state composition; not a real-world security finding",
                })
        finally:
            process.send_signal(signal.SIGTERM) if process.poll() is None else None
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
