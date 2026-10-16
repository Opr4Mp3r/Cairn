from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from cairn.audit_evidence import repository_baseline
from cairn.controller import audit as controller_audit, monitor
from cairn.controller import app as controller_app
from cairn.controller.models import AgentCallResult
from cairn.controller.pool import run_pooled
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult

from test_audit_workflow import api, configuration, repository, plan
from test_audit_review import audit


def pooled(config):
    data = config.model_dump()
    data["role_pools"] = {role: {"claudecode": 1} for role in (
        "explore", "reason", "preflight", "seed_review", "overseer", "closing", "final_review",
    )}
    data["role_pools"]["seed_review"] = {"claudecode": 2, "grok": 1}
    data["role_pools"]["overseer"] = {"claudecode": 1, "grok": 1}
    return DispatchConfig.model_validate(data)


@pytest.mark.parametrize("build_pool", [False, True])
def test_agent_selection_preserves_harness_session_and_role_pools(tmp_path, monkeypatch, build_pool):
    data = pooled(configuration(tmp_path)).model_dump()
    data["role_pools"]["preflight"] = {"claudecode": 1, "grok": 1}
    data["role_pools"]["closing"] = {"grok": 1}
    if build_pool:
        data["role_pools"]["build"] = {"claudecode": 1}
    config = DispatchConfig.model_validate(data)
    backend, calls = LocalBackend(config.local), []

    def execute(backend, workspace, worker, argv, **kwargs):
        calls.append((worker, argv))
        return ProcessResult(0, '{"approved": true, "issues": []}', "")

    monkeypatch.setattr(controller_app.shutil, "which", lambda binary: "/fixture/" + binary)
    monkeypatch.setattr(controller_app.execution, "run_worker_process", execute)

    def call(phase, *, session=None, role="reviewer"):
        return controller_app.call_agent(config, backend, role=role, prompt="fixture",
            result_model=controller_audit.PreflightResult, workspace=tmp_path,
            timeout_seconds=10, phase=phase, session=session)

    first, second = call("preflight"), call("preflight")
    repaired = call("preflight_format_repair", session=second.session)
    next_call, closing_result = call("preflight"), call("closing", role="builder")
    built = call("builder", role="builder")
    results = [first, second, repaired, next_call, closing_result, built]
    assert all(result.status == "success" for result in results)
    assert [result.harness for result in results] == [
        "claudecode", "grok", "grok", "claudecode", "grok", "claudecode" if build_pool else "grok",
    ]
    assert repaired.worker == second.worker and repaired.session == second.session
    assert first.session != second.session != next_call.session
    assert calls[2][1][calls[2][1].index("--resume") + 1] == second.session
    assert closing_result.worker == "pool-closing-grok"
    assert built.worker == ("pool-build-claudecode" if build_pool else "pool-closing-grok")
    assert all(worker.env == {} for worker, _ in calls)
    assert all("--model" not in argv for _, argv in calls)


def test_pool_bounds_each_harness_and_refills_slots(tmp_path):
    config = pooled(configuration(tmp_path))
    lock, ready = threading.Lock(), threading.Event()
    active, peak, calls = Counter(), Counter(), []

    def invoke(item, worker, child):
        with lock:
            calls.append(worker.type)
            active[worker.type] += 1
            for key, count in active.items():
                peak[key] = max(peak[key], count)
            if sum(active.values()) == 3:
                ready.set()
        try:
            assert ready.wait(2), "pool did not fill all three slots"
            time.sleep(.01)
            return item * 2
        finally:
            with lock:
                active[worker.type] -= 1

    results = dict(run_pooled(config, "seed_review", range(9), invoke, TaskCancellation()))
    assert results == {item: item * 2 for item in range(9)}
    assert peak == {"claudecode": 2, "grok": 1}
    assert Counter(calls[:3]) == {"claudecode": 2, "grok": 1}
    assert sum(active.values()) == 0


@pytest.mark.parametrize("stop", ["close", "error", "cancel"])
def test_pool_cleans_up_inflight_calls_before_returning(tmp_path, stop):
    config = pooled(configuration(tmp_path))
    cancellation, barrier = TaskCancellation(), threading.Barrier(2)
    children, finished = {}, []

    def invoke(item, worker, child):
        children[item] = child
        try:
            barrier.wait(2)
            if item == 0:
                if stop == "error":
                    raise RuntimeError("fixture Agent failed")
                if stop == "cancel":
                    cancellation.cancel("fixture user stop")
                return item
            deadline = time.monotonic() + 2
            while not child.is_cancelled and time.monotonic() < deadline:
                time.sleep(.005)
            assert child.is_cancelled, "pending Agent was not cancelled"
            return item
        finally:
            finished.append(item)

    with closing(run_pooled(config, "overseer", [0, 1], invoke, cancellation)) as results:
        if stop == "close":
            assert next(results) == (0, 0)
        else:
            with pytest.raises(RuntimeError if stop == "error" else KeyboardInterrupt):
                list(results)
    assert children[1].is_cancelled
    assert sorted(finished) == [0, 1]


def test_seed_batches_limit_outputs_keep_global_references_and_apply_once(api, tmp_path, monkeypatch):
    client, _ = api
    config, repo = pooled(configuration(tmp_path)), repository(tmp_path)
    root = Path(config.local.workspace_root)
    baseline = repository_baseline(repo, root)
    payload = plan()
    keys = ["entry", "one", "two", "three", "four", "five"]
    payload["seeds"] = [{"key": key, "description": key, "audit": {"review_status": "unverified"}} for key in keys]
    audit_plan = controller_audit.AuditPlan.model_validate(payload)
    barrier, calls, applied = threading.Barrier(3), [], []
    original_apply = controller_audit.apply_seed_reviews

    def apply(*args):
        applied.append([review.key for review in args[1].reviews])
        return original_apply(*args)

    def call_agent(config, backend, **kwargs):
        context = json.loads(kwargs["prompt"].rsplit("\n\n", 1)[1])
        if kwargs["phase"] == "preflight":
            data = {"approved": True, "issues": []}
        else:
            calls.append(context)
            barrier.wait(2)
            assert set(context["valid_fact_references"]) == {"origin", *keys}
            assert "plan" not in context
            assert {seed["key"] for seed in context["seeds"]} == set(context["review_keys"])
            assert {seed["key"] for seed in json.loads(Path(context["plan_reference"]).read_text())["seeds"]} == set(keys)
            definitions = context["output_schema"]["$defs"]
            assert definitions["SeedAssessment"]["properties"]["key"]["enum"] == context["review_keys"]
            assert set(definitions["AuditFactData"]["properties"]["premise_ids"]["items"]["enum"]) == {"origin", *keys}
            data = {"reviews": [{"key": key, "audit": {"review_status": "unverified",
                "premise_ids": ["origin"] if key == "entry" else ["entry"]}} for key in context["review_keys"]]}
        data = kwargs["result_model"].model_validate(data).model_dump(mode="json")
        worker = kwargs.get("worker") or config.pool_workers("preflight")[0]
        return AgentCallResult(role=kwargs["role"], worker=worker.name, harness=worker.type, phase=kwargs["phase"],
            workspace=str(kwargs["workspace"]), session="session-" + Path(kwargs["workspace"]).name,
            prompt=kwargs["prompt"], status="success", process=ProcessResult(0, "", ""),
            data=data, response=json.dumps(data), elapsed_seconds=0)

    monkeypatch.setattr(controller_audit, "call_agent", call_agent)
    monkeypatch.setattr(controller_audit, "apply_seed_reviews", apply)
    result = controller_audit.prepare_audit(config, client, LocalBackend(config.local), plan=audit_plan,
        context={"repo_path": str(repo), "workspace_root": str(root), "baseline": baseline, "user_goal": "trace fixture"},
        run_dir=root / "controller" / "pooled")
    assert result["status"] == "active"
    assert len(calls) == 3 and len(applied) == 1
    assert sorted(applied[0]) == sorted(keys)
    records = [record for record in client.get_audit(result["project_id"]).records if record.phase == "seed_review"]
    assert len(records) == 3 and len({record.artifacts[0] for record in records}) == 3


def add_fact(audit, *, sources, name):
    client, _, _, pid, _ = audit
    created = client.create_intent(pid, sources, name, "fixture")
    assert created.ok, created.text
    claimed = client.heartbeat(pid, created.data["id"], "fixture")
    assert claimed.ok, claimed.text
    data = client.get_project(pid).facts[2].audit.model_copy(update={"review_status": "unverified", "premise_ids": sources})
    result = client.conclude(pid, created.data["id"], "fixture", name, audit=data, lease_id=claimed.data["lease_id"])
    assert result.ok, result.text
    return result.data["fact"]["id"]


def successful(data, artifact, session="independent"):
    artifact.write_text("{}")
    return SimpleNamespace(status="success", data=data, session=session, error=None), artifact


def test_overseer_runs_only_open_intents_in_configured_parallel_slots(audit, tmp_path, monkeypatch):
    client, _, config, pid, _ = audit
    config = pooled(config)
    add_fact(audit, sources=["f001"], name="existing observation")
    created = client.create_intent(pid, ["f001"], "another research question", "fixture")
    assert created.ok
    targets = {intent.id for intent in client.get_project(pid).intents if intent.to is None}
    barrier, observed, harnesses = threading.Barrier(2), [], []

    def invoke(*args, **kwargs):
        assert kwargs["phase"] == "overseer"
        target = kwargs["context"]["target"]
        assert target["id"] in targets and target["to_fact_id"] is None
        assert "audit" not in target
        observed.append(target["id"])
        harnesses.append(kwargs["worker"].type)
        barrier.wait(3)
        data = {"decision": "approved", "reason": "direction remains in scope", "fact_audit": None, "hint": None}
        return successful(data, tmp_path / (target["id"] + ".json"))

    monkeypatch.setattr(monitor, "_invoke", invoke)
    before = client.get_project(pid).facts
    result = monitor.monitor_audit(config, client, LocalBackend(config.local), pid, once=True)
    assert result["status"] == "active" and result["reviewed"] == 2
    assert set(observed) == targets
    assert set(harnesses) == {"claudecode", "grok"}
    assert client.get_project(pid).facts == before


def test_overseer_discards_result_after_intent_concludes(audit, tmp_path, monkeypatch):
    client, _, config, pid, _ = audit
    config = pooled(config)
    intent = next(intent for intent in client.get_project(pid).intents if intent.to is None)

    def invoke(*args, **kwargs):
        assert kwargs["context"]["target"]["id"] == intent.id
        lease = client.heartbeat(pid, intent.id, "fixture").data["lease_id"]
        assert client.conclude(pid, intent.id, "fixture", "observation produced", lease_id=lease).ok
        return successful({"decision": "rejected", "reason": "old direction", "fact_audit": None, "hint": "late hint"},
                          tmp_path / "late.json")

    monkeypatch.setattr(monitor, "_invoke", invoke)
    result = monitor.monitor_audit(config, client, LocalBackend(config.local), pid, once=True)
    assert result["status"] == "active" and result["reviewed"] == 0
    assert not client.get_audit(pid).records
    assert not client.get_project(pid).hints
