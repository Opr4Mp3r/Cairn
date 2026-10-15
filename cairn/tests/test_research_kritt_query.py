from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

import pytest

from cairn.controller.kritt import KrittView
from cairn.controller.kritt_query import prepare_research_query, run_query
from cairn.controller.research import import_audit_plan
from cairn.controller.service import ManagedService
from cairn.controller.submissions import execution_snapshot, read_prepared, save_snapshot
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.tasks.audit import research_prompt
from test_audit_workflow import api, install_cli
from test_kritt_audit_reconnect import seed_review
from test_kritt_probe import probe
from test_kritt_query import query_context
from test_managed_service import configured


@pytest.mark.parametrize("keep_input", [True, False])
def test_activation_and_recovery_deliver_submitted_queries_without_changing_progress(probe, api, tmp_path, monkeypatch, keep_input):
    config, prepared, _ = configured(probe)
    _, _, artifacts = read_prepared(Path(prepared["run_dir"]))
    submitted = save_snapshot(tmp_path / "submitted", prepared, artifacts, execution_snapshot(config))
    Path(prepared["run_dir"], "kritt-view.json").write_text("changed Builder input")
    install_cli(tmp_path, monkeypatch, [{"approved": True, "issues": []}, seed_review()])
    client, _ = api
    result = import_audit_plan(config, client, LocalBackend(config.local), submitted, external_builder=True)
    workspace = Path(result["workspace"])
    input_path = workspace / "input.json"
    initial = json.loads(input_path.read_text())
    assert initial["kritt_query"]["available"]
    query_path = workspace / "kritt-context.json"
    query = json.loads(query_path.read_text())
    assert Path(query["view_path"]) == Path(submitted["query_context"]).parent / "kritt-view.json"
    assert Path(query["queries_dir"]) == workspace / "research" / "kritt-queries"
    assert run_query(query_path, {"action": "describe"})["scan_id"] == prepared["scan_id"]

    project = client.get_project(result["project_id"])
    protected = [Path(submitted[key]) for key in ("query_context", "materials")]
    protected += [Path(query["view_path"]), Path(submitted["query_context"]).parent / "query-receipts.json"]
    fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    query_path.unlink()
    if keep_input:
        initial.pop("kritt_query")
        input_path.write_text(json.dumps(initial))
    else:
        input_path.unlink()
    service = ManagedService.__new__(ManagedService)
    service._restore_workspace(config, submitted, project)
    restored = json.loads(input_path.read_text())
    for name in ("reason.md", "explore.md"):
        prompt = research_prompt(config, project, name, {
            "graph_yaml": "/graph.yaml", "intent_id": "i001", "intent_description": "read source",
            "fact_ids": "[]", "open_intents": "[]", "max_intents": "2", "intent_sources": "[]",
        })
        audit_context = json.loads(prompt.split("## 固定审计上下文\n", 1)[1])
        assert audit_context["kritt_query"] == restored["kritt_query"]
    command = shlex.split(restored["kritt_query"]["command"])

    def search(_):
        called = subprocess.run(command, input='{"action":"search","kind":"flow"}',
                                text=True, capture_output=True, cwd=workspace, timeout=10)
        assert called.returncode == 0, called.stdout + called.stderr
        return json.loads(called.stdout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(search, range(2)))
    assert outputs[0]["query_id"] != outputs[1]["query_id"]
    assert all(Path(out["receipts_path"]).is_relative_to(workspace / "research") for out in outputs)
    fetched = run_query(query_path, {"action": "get", "ref": outputs[0]["records"][0]["ref"]})
    assert fetched["material_count"] > 0 and Path(fetched["materials_path"]).is_file()
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in fingerprints.items())
    assert client.get_project(result["project_id"]).model_dump() == project.model_dump()


def test_research_query_keeps_fixed_view_digest(query_context, tmp_path):
    workspace = tmp_path / "research"
    workspace.mkdir()
    original = query_context.read_bytes()
    access = prepare_research_query(workspace, str(query_context))
    assert access["available"]
    local = workspace / "kritt-context.json"
    context = json.loads(local.read_text())
    view = KrittView.load(context["view_path"])
    # A valid different view must not silently replace this project's input.
    KrittView({**view.data, "scan": {**view.scan, "repo_full": "other/repository"}}).save(context["view_path"])
    with pytest.raises(ValueError, match="fixed Kritt view"):
        run_query(local, {"action": "describe"})
    assert query_context.read_bytes() == original


@pytest.mark.parametrize("missing", [None, "missing-context.json"])
def test_missing_kritt_materials_leave_source_research_available(tmp_path, missing):
    access = prepare_research_query(tmp_path, None if missing is None else str(tmp_path / missing))
    assert access["available"] is False and "源码" in access["reason"]
    assert not (tmp_path / "kritt-context.json").exists()


def test_audit_examples_use_existing_reason_timeout():
    root = Path(__file__).resolve().parents[2]
    for name in ("dispatch.service.example.yaml", "dispatch.builder.example.yaml", "dispatch.audit.example.yaml"):
        assert DispatchConfig.load(root / name).tasks.reason.timeout == 1800
