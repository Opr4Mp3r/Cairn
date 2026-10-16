from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from cairn.controller import monitor
from cairn.controller.models import AgentCallResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.server.models import FinalAssessment

from test_audit_workflow import api
from test_audit_review import audit, assessment, completion
from test_audit_budget import clock
from test_role_pool_controller import pooled, successful


def test_control_tick_never_invokes_overseer(audit, monkeypatch):
    client, _, config, pid, _ = audit
    monkeypatch.setattr(monitor, "_invoke", lambda *args, **kwargs: pytest.fail("control tick invoked Overseer"))
    result = monitor.monitor_once(config, client, LocalBackend(config.local), pid)
    assert result["status"] == "active" and result["reviewed"] == 0


@pytest.mark.parametrize("pooled_role", [False, True])
def test_slow_overseer_does_not_block_research_or_final_review(audit, tmp_path, monkeypatch, pooled_role):
    client, _, config, pid, _ = audit
    config = pooled(config) if pooled_role else config
    config.controller.poll_interval = 1
    started, final_started, finished = threading.Event(), threading.Event(), threading.Event()
    tokens = {}
    intent_id = next(i.id for i in client.get_project(pid).intents if i.to is None)

    def invoke(*args, **kwargs):
        phase, token = kwargs["phase"], kwargs["cancellation"]
        tokens[phase] = token
        if phase == "overseer":
            started.set()
            deadline = time.monotonic() + 5
            while not token.is_cancelled and time.monotonic() < deadline:
                time.sleep(.01)
            assert token.is_cancelled, "side channel was not cancelled after final completion"
            finished.set()
            return successful({"decision": "rejected", "reason": "late side result", "hint": "must not apply"}, tmp_path / "side.json")
        assert phase == "final_review"
        assert started.is_set() and not finished.is_set()
        final_started.set()
        return successful(assessment(), tmp_path / "final.json", session="final-independent")

    monkeypatch.setattr(monitor, "_invoke", invoke)
    parent = TaskCancellation()
    with ThreadPoolExecutor(max_workers=1) as executor:
        run = executor.submit(monitor.monitor_audit, config, client, LocalBackend(config.local), pid,
                              once=True, cancellation=parent)
        assert started.wait(3)
        # The real API still admits independent research while the side call waits.
        lease = client.heartbeat(pid, intent_id, "fixture")
        assert lease.ok, lease.text
        assert client.conclude(pid, intent_id, "fixture", "research continues", lease_id=lease.data["lease_id"]).ok
        completion(audit)
        assert final_started.wait(3), "Final Review waited for slow Overseer"
        result = run.result(timeout=4)
    assert result["status"] == "completed" and finished.is_set()
    assert tokens["overseer"] is not tokens["final_review"]
    assert not parent.is_cancelled
    assert not client.get_project(pid).hints
    assert not any(r.phase == "overseer" for r in client.get_audit(pid).records)


def test_slow_overseer_does_not_block_budget_closing(audit, clock, tmp_path, monkeypatch):
    client, _, config, pid, _ = audit
    config.controller.poll_interval = 1
    started, closed = threading.Event(), threading.Event()

    def invoke(*args, **kwargs):
        if kwargs["phase"] == "overseer":
            started.set()
            token = kwargs["cancellation"]
            deadline = time.monotonic() + 5
            while not token.is_cancelled and time.monotonic() < deadline:
                time.sleep(.01)
            assert token.is_cancelled
            return successful({"decision": "approved", "reason": "late", "hint": None}, tmp_path / "side.json")
        assert kwargs["phase"] == "closing"
        closed.set()
        return successful({"kind": "no_candidate", "description": "no candidate in final graph"}, tmp_path / "closing.json")

    monkeypatch.setattr(monitor, "_invoke", invoke)
    with ThreadPoolExecutor(max_workers=1) as executor:
        run = executor.submit(monitor.monitor_audit, config, client, LocalBackend(config.local), pid, once=True)
        assert started.wait(3)
        clock(client.get_audit(pid).research_deadline)
        assert closed.wait(3), "Closing waited for slow Overseer"
        assert run.result(timeout=4)["status"] == "budget_exhausted"


@pytest.mark.parametrize("pooled_role", [False, True])
@pytest.mark.parametrize("stop_project", [False, True])
def test_cancellation_cleans_every_side_call_without_shared_process_token(audit, tmp_path, monkeypatch, pooled_role, stop_project):
    client, _, config, pid, _ = audit
    config = pooled(config) if pooled_role else config
    if pooled_role:
        assert client.create_intent(pid, ["f001"], "second direction", "fixture").ok
    config.controller.poll_interval = 1
    capacity = 2 if pooled_role else 1
    ready = threading.Barrier(capacity + 1)
    cleaned, tokens = [], []

    class Process:
        def cancel(self, reason):
            cleaned.append(reason)

    def invoke(*args, **kwargs):
        token = kwargs["cancellation"]
        tokens.append(token)
        token.attach_process(Process())
        ready.wait(3)
        deadline = time.monotonic() + 5
        while not token.is_cancelled and time.monotonic() < deadline:
            time.sleep(.01)
        assert token.is_cancelled
        token.attach_process(None)
        return successful({"decision": "approved", "reason": "cancelled", "hint": None},
                          tmp_path / (kwargs["context"]["target"]["id"] + ".json"))

    monkeypatch.setattr(monitor, "_invoke", invoke)
    parent = TaskCancellation()
    with ThreadPoolExecutor(max_workers=1) as executor:
        run = executor.submit(monitor.monitor_audit, config, client, LocalBackend(config.local), pid, cancellation=parent)
        ready.wait(3)
        if stop_project:
            assert client.set_project_status(pid, "stopped").ok
        else:
            parent.cancel("Task paused")
        assert run.result(timeout=4)["status"] == ("stopped" if stop_project else "cancelled")
    assert len(tokens) == len({id(token) for token in tokens}) == capacity
    assert all(token is not parent for token in tokens)
    assert len(cleaned) == capacity
    assert not client.get_audit(pid).records


def test_overseer_exception_is_recorded_and_does_not_stop_project(audit, monkeypatch):
    client, _, config, pid, _ = audit
    calls = []

    def broken(*args, **kwargs):
        calls.append(kwargs["phase"])
        raise OSError("local side tool failed")

    monkeypatch.setattr(monitor, "_invoke", broken)
    backend = LocalBackend(config.local)
    for _ in range(config.controller.review.max_attempts + 1):
        assert monitor.monitor_audit(config, client, backend, pid, once=True)["status"] == "active"
    records = client.get_audit(pid).records
    assert len(calls) == len(records) == config.controller.review.max_attempts
    assert all(r.object_type == "intent" and r.attempt_status == "error" for r in records)
    assert client.create_intent(pid, ["f001"], "research remains available", "fixture").ok


def test_batch_error_is_logged_without_terminating_control(audit, monkeypatch, caplog):
    client, _, config, pid, _ = audit

    def broken(*args, **kwargs):
        raise RuntimeError("side transport failed")

    monkeypatch.setattr(monitor, "_supervise_once", broken)
    result = monitor.monitor_audit(config, client, LocalBackend(config.local), pid, once=True)
    assert result["status"] == "active"
    assert "side transport failed" in result["supervision_error"]
    assert "side transport failed" in caplog.text
    assert client.get_project(pid).project.status == "active"


def test_overseer_rechecks_conclusion_before_pooled_call(audit, monkeypatch):
    client, _, config, pid, _ = audit
    config = pooled(config)
    intent = next(i for i in client.get_project(pid).intents if i.to is None)
    original = monitor._review_items

    def conclude_after_selection(*args, **kwargs):
        for item in original(*args, **kwargs):
            lease = client.heartbeat(pid, intent.id, "fixture").data["lease_id"]
            assert client.conclude(pid, intent.id, "fixture", "completed between reads", lease_id=lease).ok
            yield item

    monkeypatch.setattr(monitor, "_review_items", conclude_after_selection)
    monkeypatch.setattr(monitor, "_invoke", lambda *args, **kwargs: pytest.fail("closed Intent was supervised"))
    result = monitor.monitor_audit(config, client, LocalBackend(config.local), pid, once=True)
    assert result["reviewed"] == 0 and result["status"] == "active"


def test_overseer_does_not_scan_source_or_stop_project(audit, monkeypatch):
    client, _, config, pid, repo = audit
    project = client.get_project(pid)
    intent = next(i for i in project.intents if i.to is None)
    context = client.get_review_input(pid, "intent", intent.id)["context"]
    (repo / "app.py").write_text("changed source irrelevant to direction check\n")
    monkeypatch.setattr(monitor, "research_workspace", lambda *args: pytest.fail("Overseer invoked stopping source guard"))
    prompts = []

    def call(config, backend, **kwargs):
        prompts.append(kwargs["prompt"])
        data = {"decision": "needs_evidence", "reason": "direction missing an initial condition", "fact_audit": None, "hint": None}
        return AgentCallResult(role="reviewer", worker="fixture", phase="overseer", workspace=str(kwargs["workspace"]),
                               session="side-independent", prompt=kwargs["prompt"], status="success", process=ProcessResult(0, "", ""),
                               response=json.dumps(data), data=data, elapsed_seconds=0)

    monkeypatch.setattr(monitor, "call_agent", call)
    result, path = monitor._invoke(config, client, LocalBackend(config.local), project, phase="overseer", context=context,
                                  model=monitor.NodeAssessment, timeout=15, cancellation=TaskCancellation())
    assert result.status == "success" and path.is_file()
    payload = json.loads(path.with_name("input.json").read_text())
    assert payload["target"]["id"] == intent.id
    assert "reference_check" not in payload and "unsupported_premise_ids" not in payload
    assert client.get_project(pid).project.status == "active"


@pytest.mark.parametrize("phase", ["closing", "final_review"])
def test_terminal_roles_still_use_source_guard(audit, monkeypatch, phase):
    client, _, config, pid, _ = audit

    def guard(*args):
        raise ValueError("fixed source changed")

    monkeypatch.setattr(monitor, "research_workspace", guard)
    monkeypatch.setattr(monitor, "call_agent", lambda *args, **kwargs: pytest.fail("Agent called before source guard"))
    with pytest.raises(ValueError, match="fixed source changed"):
        monitor._invoke(config, client, LocalBackend(config.local), client.get_project(pid), phase=phase,
                        context={}, model=FinalAssessment, timeout=15, cancellation=TaskCancellation())


def test_existing_completion_precedes_budget_closing(audit, clock, tmp_path, monkeypatch):
    client, _, config, pid, _ = audit
    completion(audit)
    clock(client.get_audit(pid).research_deadline)
    calls = []

    def invoke(*args, **kwargs):
        calls.append(kwargs["phase"])
        assert kwargs["phase"] == "final_review", "existing proof was held behind Closing"
        return successful(assessment(), tmp_path / "final.json", session="final-independent")

    monkeypatch.setattr(monitor, "_invoke", invoke)
    result = monitor.monitor_once(config, client, LocalBackend(config.local), pid)
    assert result["status"] == "completed" and calls == ["final_review"]


def test_cancelled_side_batch_does_not_disable_later_supervision(audit, monkeypatch):
    client, _, config, pid, _ = audit
    config.controller.poll_interval = 1
    parent, tokens = TaskCancellation(), []

    def supervise(_config, _client, _backend, _project_id, token):
        assert not token.is_cancelled
        tokens.append(token)
        if len(tokens) == 1:
            token.cancel("side tool interrupted")
        else:
            parent.cancel("test finished")
        return 0

    monkeypatch.setattr(monitor, "_supervise_once", supervise)
    result = monitor.monitor_audit(config, client, LocalBackend(config.local), pid, cancellation=parent)
    assert result["status"] == "cancelled"
    assert len(tokens) == 2 and tokens[0] is not tokens[1]
    assert client.get_project(pid).project.status == "active"
