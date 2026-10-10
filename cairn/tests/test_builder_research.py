import json
import os
from pathlib import Path
import sqlite3
import threading

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest
import yaml

from cairn.cli import main
from cairn.controller.research import import_plan, run_research
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.server import db
from cairn.server.app import app
from test_builder import prepared
from test_kritt_probe import probe


def test_import_uses_generic_interfaces_and_maps_seed_ids(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "app.py").write_text("def entry(): return 'ok'\n")
    run = tmp_path / "run"
    run.mkdir()
    plan = {"title": "研究", "origin": "已有材料", "goal": "沿输入研究",
            "seeds": [{"key": "entry", "description": "入口材料"}, {"key": "flow", "description": "流程材料"}],
            "intents": [{"from": ["flow", "entry"], "description": "继续读取源码"}], "hints": ["保留条件"]}
    (run / "plan.json").write_text(json.dumps(plan))
    (run / "selected-materials.json").write_text('[{"condition":"未安装"}]')
    ready = {"run_dir": str(run), "source_root": str(repo), "plan": str(run / "plan.json"),
             "materials": str(run / "selected-materials.json"), "repo_full": "example/project",
             "scan_id": 19, "commit_sha": "a" * 40, "user_goal": "用户原始需求"}
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as http:
        client = CairnClient("http://testserver")

        def request(method, path, json):
            response = http.request(method, path, json=json)
            return ApiResult(response.status_code, response.json(), response.text)

        monkeypatch.setattr(client, "_request_json", request)
        result = import_plan(client, LocalBackend(LocalConfig(workspace_root=str(run / "work"))), ready)
        project = http.get(f"/projects/{result['project_id']}").json()
    assert project["project"].get("mode", "generic") == "generic" and not project["project"]["bootstrap_enabled"]
    assert project["project"]["status"] == "active" and "audit" not in project["project"]
    assert result["seed_ids"] == {"entry": "f001", "flow": "f002"}
    imports = [i for i in project["intents"] if i["creator"] == "builder-import"]
    assert [i["to"] for i in imports] == ["f001", "f002"]
    research = next(i for i in project["intents"] if i["id"] == result["intent_ids"][0])
    assert research["from"] == ["f002", "f001"] and research["worker"] is None and research["to"] is None
    workspace = Path(result["workspace"])
    assert (workspace / "repo").resolve() == repo
    assert json.loads((workspace / "kritt-materials.json").read_text())[0]["condition"] == "未安装"
    assert json.loads((workspace / "research-context.json").read_text())["user_goal"] == ready["user_goal"]
    assert "repo/" in project["hints"][0]["content"]


@pytest.mark.parametrize("ending", ["completed", "stopped"])
def test_legacy_generic_research_still_imports_and_writes_new_fact(prepared, monkeypatch, ending):
    config, path, view, run = prepared
    config.server = "http://127.0.0.1:0"
    config.runtime.interval = 1
    config.runtime.worker_healthcheck = "disabled"
    config.tasks.reason.timeout = 10
    config.tasks.explore.timeout = 15
    path.write_text(json.dumps(config.model_dump(mode="json")))
    binary = path.parent / "bin" / "claude"
    calls = path.parent / "research-calls.jsonl"
    monkeypatch.setenv("RESEARCH_TEST_CALLS", str(calls))
    monkeypatch.setenv("RESEARCH_TEST_ENDING", ending)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))

    def bounded_research(*args, **kwargs):
        report = kwargs["report"]

        def show_progress(event):
            report(event)
            if ending == "stopped" and event.get("fact_count", 0) >= 2:
                client = CairnClient(event["server"], trust_env=False)
                try:
                    assert client.set_project_status(event["project_id"], "stopped").ok
                finally:
                    client.close()

        kwargs["report"] = show_progress
        timer = threading.Timer(25, kwargs["cancellation"].cancel, args=("test deadline",))
        timer.start()
        try:
            return run_research(*args, **kwargs)
        finally:
            timer.cancel()
            timer.join()

    prefix = r'''
import json, os, pathlib, re, sys
prompt = sys.argv[-1]
if '"output_schema"' not in prompt:
    import yaml
    assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
    graph_path = re.search(r"/tmp/cairn-prompts/[^\s]+/graph.yaml", prompt).group()
    graph = yaml.safe_load(pathlib.Path(graph_path).read_text())
    assert graph["project"].get("mode", "generic") == "generic"
    assert any(i["creator"] == "builder-import" and i["to"] is not None for i in graph["intents"])
    observed = [f for f in graph["facts"] if f["description"].startswith("新探索读取：")]
    reason = "You need to judge two things:" in prompt
    if reason:
        data = {"complete": {"from": [observed[0]["id"]], "description": "完成源码读取"}} if observed and os.environ["RESEARCH_TEST_ENDING"] == "completed" else {}
    else:
        source = pathlib.Path("repo/module/install/control.php").read_text()
        materials = json.loads(pathlib.Path("kritt-materials.json").read_text())
        assert materials and source
        data = {"description": "新探索读取：" + source[:80]}
    with pathlib.Path(os.environ["RESEARCH_TEST_CALLS"]).open("a") as f:
        f.write(json.dumps({"phase": "reason" if reason else "explore", "pid": os.getpid(), "cwd": os.getcwd()}) + "\n")
    print(json.dumps({"accepted": True, "data": data}))
    raise SystemExit
'''
    original = binary.read_text()
    binary.write_text(original.split("\n", 1)[0] + "\n" + prefix + original.split("\n", 1)[1])
    run.mkdir()
    plan = {"title": "旧generic研究", "origin": "已有材料", "goal": "读取源码",
            "seeds": [{"key": "entry", "description": "入口材料"}],
            "intents": [{"from": ["entry"], "description": "继续读取源码"}], "hints": []}
    (run / "plan.json").write_text(json.dumps(plan))
    (run / "selected-materials.json").write_text('[{"condition":"待核对"}]')
    target = config.controller.kritt.targets["test"]
    ready = {"run_dir": str(run), "source_root": target.source_root, "plan": str(run / "plan.json"),
             "materials": str(run / "selected-materials.json"), "repo_full": target.repo_full,
             "scan_id": 19, "commit_sha": "a" * 40}
    events = []
    result = bounded_research(config, ready, cancellation=TaskCancellation(), report=events.append)
    events.append(result)
    summary = json.loads((run / "research.json").read_text())
    locator = json.loads((run / "project.json").read_text())
    assert summary["status"] == ending and summary["project_id"] == locator["project_id"]
    assert set(summary["processes"]) == {"server", "dispatcher"}
    assert events[0]["status"] == "researching"
    assert events[0]["project_url"] == f"{summary['server']}/#/projects/{summary['project_id']}"
    assert events[0]["fact_count"] == 1 and events[0]["open_intent_count"] == 1
    assert events[-1]["status"] == ending and events[-1]["fact_count"] >= 2
    assert events[-1]["results"] == summary["results"]
    assert "export_error" not in summary
    snapshot = json.loads(Path(summary["results"]["project"]).read_text())
    graph = yaml.safe_load(Path(summary["results"]["graph"]).read_text())
    timeline = Path(summary["results"]["timeline"]).read_text()
    assert snapshot["project"]["id"] == locator["project_id"] and snapshot["project"]["status"] == ending
    assert any(fact["description"].startswith("新探索读取：") for fact in graph["facts"])
    assert "INTENT CONCLUDED" in timeline and "新探索读取：" in timeline
    assert ("PROJECT COMPLETED" in timeline) == (ending == "completed")
    with sqlite3.connect(run / "cairn.db") as sql:
        facts = sql.execute("SELECT id, description FROM facts WHERE project_id = ?", (summary["project_id"],)).fetchall()
        assert any(description.startswith("新探索读取：") and fid not in locator["seed_ids"].values() for fid, description in facts)
        assert sql.execute("SELECT mode FROM projects").fetchone()[0] == "generic"
    executed = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any(record["phase"] == "explore" for record in executed)
    for pid in [row["pid"] for row in executed] + [row["pid"] for row in summary["processes"].values()]:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
