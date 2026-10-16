from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

import pytest

from cairn.controller.kritt import KrittView
from cairn.controller.kritt_query import collect_queries, prepare_research_query, run_query
from cairn.controller import monitor
from cairn.controller.models import AgentCallResult
from cairn.controller.research import import_audit_plan
from cairn.controller.service import ManagedService
from cairn.controller.submissions import execution_snapshot, read_prepared, save_snapshot
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.server.models import ClosingResult, FinalAssessment
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


@pytest.mark.parametrize("phase,model", [("overseer", monitor.NodeAssessment), ("closing", ClosingResult),
                                         ("final_review", FinalAssessment)])
def test_reviews_locate_research_receipts_separately_from_opening_ledger(probe, api, tmp_path, monkeypatch, phase, model):
    config, prepared, _ = configured(probe)
    view = KrittView.load(Path(prepared["run_dir"]) / "kritt-view.json")
    opening_context = Path(prepared["query_context"])
    run_query(opening_context, {"action": "get", "ref": view.ref("workflows.step_results", 20, "19-1").model_dump(mode="json")})
    collect_queries(opening_context)
    install_cli(tmp_path, monkeypatch, [{"approved": True, "issues": []}, seed_review()])
    client, _ = api
    backend = LocalBackend(config.local)
    result = import_audit_plan(config, client, backend, prepared, external_builder=True)
    project = client.get_project(result["project_id"])
    workspace = Path(result["workspace"])
    research = run_query(workspace / "kritt-context.json", {
        "action": "get", "ref": view.ref("workflows.step_results", 21, "19-1").model_dump(mode="json"),
    })
    research_receipts = json.loads(Path(research["receipts_path"]).read_text())
    receipt_id = research_receipts["receipts"][0]["receipt"]["receipt_id"]
    opening = workspace / "query-receipts.json"
    protected = {p: p.read_bytes() for p in [opening, Path(research["receipts_path"]), Path(research["materials_path"])]}
    assert receipt_id not in {r["receipt"]["receipt_id"] for r in json.loads(opening.read_text())["receipts"]}
    fact = next(f for f in project.facts if f.id == result["seed_ids"]["entry"])

    def assess(_config, _backend, *, prompt, **kwargs):
        payload = json.loads(prompt.rsplit("\n\n", 1)[1])
        locations = payload["query_receipts"]
        assert Path(locations["opening_ledger"]) == opening
        assert Path(locations["view_context"]) == workspace / "kritt-context.json"
        directory = Path(locations["research_directory"])
        matches = [r["receipt"] for p in directory.glob("q-*/receipts.json") for r in json.loads(p.read_text())["receipts"]
                   if r["receipt"]["receipt_id"] == receipt_id]
        assert len(matches) == 1 and matches[0]["parameters"]["ref"]["id"] == 21
        assert matches[0]["view_digest"] == view.digest
        assert "开题查询，不是完整研究账本" in prompt and "不运行新查询" in prompt
        if phase == "overseer":
            answer = {"decision": "approved", "reason": "research receipt located", "fact_audit": None, "hint": None}
        elif phase == "closing":
            answer = {"kind": "no_candidate", "from": [], "description": "existing receipts checked"}
        else:
            answer = {"decision": "needs_evidence", "reason": "receipt existence does not establish the goal",
                      "proof_ids": [fact.id], "checks": dict.fromkeys(["effect", "deployment", "attacker_capabilities", "scope", "cvss", "evidence"], False),
                      "cvss_vector": None}
        raw = json.dumps(answer)
        return AgentCallResult(role=kwargs["role"], worker="fixture", harness="mock", phase=phase, workspace=str(workspace),
                               session="review-session", prompt=prompt, status="success", response=raw, data=answer,
                               process=ProcessResult(0, raw, ""), elapsed_seconds=0)

    monkeypatch.setattr(monitor, "call_agent", assess)
    intent = next(intent for intent in project.intents if intent.to is None)
    context = client.get_review_input(project.project.id, "intent", intent.id)["context"] if phase == "overseer" else {
        "graph": project.model_dump(mode="json", by_alias=True), "policy": project.project.audit.policy}
    verdict, _ = monitor._invoke(config, client, backend, project, phase=phase, context=context, model=model,
                                 timeout=10, cancellation=TaskCancellation())
    assert verdict.status == "success"
    assert all(p.read_bytes() == content for p, content in protected.items())
