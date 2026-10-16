from __future__ import annotations

from concurrent.futures import Future
import json

import pytest

from cairn.controller import monitor
from cairn.controller.models import AgentCallResult
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import bootstrap, explore, reason

from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent, make_project
from test_audit_workflow import api, configuration, create, install_cli, plan, repository, review
from test_audit_review import audit, assessment, completion
from test_audit_budget import clock
from test_scheduler_logic import _loop


@pytest.mark.parametrize('operation,args', [
    ('heartbeat', ('p', 'i', 'worker')),
    ('conclude', ('p', 'i', 'worker', 'fact')),
    ('complete', ('p', ['f'], 'done', 'worker')),
    ('create_intent', ('p', ['f'], 'next', 'worker')),
])
def test_client_adds_producer_only_when_supplied(monkeypatch, operation, args):
    client, bodies = CairnClient('http://unused'), []
    def capture(_method, _path, json):
        bodies.append(json)
        return ApiResult(200, {})
    monkeypatch.setattr(client, '_request_json', capture)
    getattr(client, operation)(*args)
    getattr(client, operation)(*args, harness='pi')
    assert 'harness' not in bodies[0]
    assert bodies[1] == {**bodies[0], 'harness': 'pi'}


@pytest.mark.parametrize('role,fallback', [
    ('explore', False), ('explore', True), ('bootstrap', False), ('bootstrap', True),
    ('reason_intent', False), ('reason_complete', False),
])
def test_research_producer_comes_from_selected_worker_not_model(monkeypatch, role, fallback):
    config = make_config()
    worker = config.workers[0].model_copy(update={'name': 'arbitrary-name', 'type': 'pi'})
    intent = make_intent()
    project = make_project(intents=[] if role.startswith('reason') else [intent])
    client, backend, driver = FakeClient(project), FakeContainerManager(), FakeDriver()
    forged = {'harness': 'model-invented', 'worker': 'model-invented'}
    if role == 'explore':
        data = {'description': 'observed', **forged}
    elif role == 'bootstrap':
        data = {'fact': {'description': 'observed', **forged}}
        if not fallback:
            data['complete'] = {'description': 'done', **forged}
    elif role == 'reason_intent':
        data = {'intents': [{'from': ['f001'], 'description': 'next', **forged}]}
    else:
        data = {'complete': {'from': ['f001'], 'description': 'done', **forged}}
    answer = ProcessResult(0, json.dumps({'accepted': True, 'data': data, **forged}), '')
    outputs = iter([ProcessResult(0, 'incomplete; finish in conclusion', ''), answer] if fallback else [answer])
    module = reason if role.startswith('reason') else explore if role == 'explore' else bootstrap
    monkeypatch.setattr(module, 'get_driver', lambda *_a, **_kw: driver)
    monkeypatch.setattr(module.HeartbeatLease, 'for_reason' if role.startswith('reason') else 'for_intent',
                        lambda *_a, **_kw: FakeLease())
    monkeypatch.setattr(module, '_run_process' if role == 'explore' else 'run_worker_process', lambda *_a, **_kw: next(outputs))
    if role.startswith('reason'):
        result = reason.run_reason_task(config, client, backend, project, 'graph', worker, TaskCancellation())
        expected = ['create_intent' if role == 'reason_intent' else 'complete']
    elif role == 'explore':
        result = explore.run_explore_task(config, client, backend, project, 'graph', intent, worker, TaskCancellation())
        expected = ['conclude']
    else:
        result = bootstrap.run_bootstrap_task(config, client, backend, project, intent, worker, TaskCancellation())
        expected = ['conclude'] if fallback else ['conclude', 'complete']
    assert result == 'success'
    assert client.provenance_calls == [(operation, 'arbitrary-name', 'pi') for operation in expected]


@pytest.mark.parametrize('phase', ['bootstrap', 'explore'])
def test_scheduler_claim_records_actual_selected_harness(phase):
    config = make_config()
    config.workers[0].type = 'codex'
    project = make_project(intents=[make_intent()])
    client = FakeClient(project)
    loop = _loop()
    loop.config, loop.client, loop.futures = config, client, {}
    loop.container_manager = FakeContainerManager()
    loop.executor = type('Executor', (), {'submit': lambda self, *args: Future()})()
    if phase == 'explore':
        dispatched = loop._dispatch_explore(project, 'graph', project.intents[0])
    else:
        dispatched = loop._dispatch_bootstrap(project, project.intents[0])
    assert dispatched
    assert client.provenance_calls == [('heartbeat', 'test-worker', 'codex')]


def agent_result(tmp_path, *, status, data, phase):
    artifact = tmp_path / f'{phase}-{status}.json'
    artifact.write_text('{}')
    return AgentCallResult(
        role='reviewer', worker='actual-review-worker', harness='minimax', phase=phase,
        workspace=str(tmp_path), session=f'{phase}-session', prompt='fixture', status=status,
        data=data if status == 'success' else None, error=None if status == 'success' else 'temporary failure',
        process=ProcessResult(0 if status == 'success' else 1, '', ''), elapsed_seconds=.01,
    ), artifact


@pytest.mark.parametrize('status', ['success', 'failed'])
def test_overseer_receipts_record_runner_identity_on_success_and_error(audit, tmp_path, monkeypatch, status):
    client, _, config, pid, _ = audit
    monkeypatch.setattr(monitor, '_invoke', lambda *a, **kw: agent_result(
        tmp_path, status=status, phase='overseer',
        data={'decision': 'needs_evidence', 'reason': 'stay within scope', 'hint': 'Check boundary first'},
    ))
    assert monitor._supervise_once(config, client, LocalBackend(config.local), pid, TaskCancellation()) == 1
    record = next(r for r in client.get_audit(pid).records if r.phase == 'overseer')
    assert (record.worker, record.harness) == ('actual-review-worker', 'minimax')
    assert record.attempt_status == ('success' if status == 'success' else 'error')


def test_overseer_exception_does_not_invent_identity(audit, monkeypatch):
    client, _, config, pid, _ = audit
    def fail(*args, **kwargs):
        raise RuntimeError('no result from runner')
    monkeypatch.setattr(monitor, '_invoke', fail)
    assert monitor._supervise_once(config, client, LocalBackend(config.local), pid, TaskCancellation()) == 1
    record = next(r for r in client.get_audit(pid).records if r.phase == 'overseer')
    assert record.worker is None and record.harness is None


def test_preparation_receipts_preserve_runner_identity(api, tmp_path, monkeypatch):
    client, _ = api
    config, repo = configuration(tmp_path), repository(tmp_path)
    install_cli(tmp_path, monkeypatch, [plan(), {'approved': True, 'issues': []}, review()])
    result = create(config, client, repo)
    records = client.get_audit(result['project_id']).records
    assert {r.phase for r in records} == {'build', 'preflight', 'seed_review'}
    assert all((r.worker, r.harness) == ('claude-main', 'claudecode') for r in records)


def test_final_review_receipt_preserves_actual_identity(audit, tmp_path, monkeypatch):
    client, _, config, pid, _ = audit
    completion(audit)
    monkeypatch.setattr(monitor, '_invoke', lambda *a, **kw: agent_result(
        tmp_path, status='success', phase='final_review', data=assessment(),
    ))
    monitor.monitor_once(config, client, LocalBackend(config.local), pid)
    record = next(r for r in client.get_audit(pid).records if r.phase == 'final_review')
    assert (record.worker, record.harness) == ('actual-review-worker', 'minimax')


def test_closing_receipt_preserves_actual_identity(audit, clock, tmp_path, monkeypatch):
    client, _, config, pid, _ = audit
    clock(client.get_audit(pid).research_deadline)
    monkeypatch.setattr(monitor, '_invoke', lambda *a, **kw: agent_result(
        tmp_path, status='success', phase='closing', data={'kind': 'no_candidate', 'description': 'nothing proved'},
    ))
    monitor.monitor_once(config, client, LocalBackend(config.local), pid)
    record = next(r for r in client.get_audit(pid).records if r.phase == 'closing')
    assert (record.worker, record.harness) == ('actual-review-worker', 'minimax')


def test_retried_exploration_uses_successful_worker_identity(monkeypatch):
    config, intent = make_config(), make_intent()
    project = make_project(intents=[intent])
    client, backend = FakeClient(project), FakeContainerManager()
    driver = FakeDriver()
    monkeypatch.setattr(explore, 'get_driver', lambda *_a, **_kw: driver)
    monkeypatch.setattr(explore.HeartbeatLease, 'for_intent', lambda *_a, **_kw: FakeLease())
    outputs = iter([
        ProcessResult(1, '', 'CLI failed before any usable result'),
        ProcessResult(0, json.dumps({'accepted': True, 'data': {'description': 'confirmed', 'harness': 'wrong'}}), ''),
    ])
    monkeypatch.setattr(explore, '_run_process', lambda *_a, **_kw: next(outputs))
    failed = config.workers[0].model_copy(update={'name': 'first-worker', 'type': 'codex'})
    retried = config.workers[0].model_copy(update={'name': 'second-worker', 'type': 'pi'})
    assert explore.run_explore_task(config, client, backend, project, 'graph', intent, failed, TaskCancellation()) == 'failed'
    assert client.provenance_calls == []
    assert explore.run_explore_task(config, client, backend, project, 'graph', intent, retried, TaskCancellation()) == 'success'
    assert client.provenance_calls == [('conclude', 'second-worker', 'pi')]
