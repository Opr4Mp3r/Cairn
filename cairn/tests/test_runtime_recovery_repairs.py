from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from cairn.controller import local, submissions as client_module
from cairn.controller.local import CLI, stop_agent_groups
from cairn.controller.service import ManagedService
from cairn.controller.submissions import execution_snapshot
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.owned_process import renew_owner
from cairn.server import db, runtime
from test_audit_review import audit
from test_audit_workflow import api
from test_kritt_probe import probe
from test_managed_service import configured, FAKE_CLI, wait_until


@pytest.mark.parametrize('stage', ['preparing', 'resuming'])
def test_pause_before_activation_keeps_shared_heartbeat_alive(audit, stage, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(runtime, 'monotonic', lambda: now[0])
    pid = audit[3]
    with db.get_conn() as conn:
        data = runtime.initialize(conn, pid)
        data['state'] = stage
        runtime.save(conn, pid, data)
        conn.execute("INSERT INTO projects(id,title,status,created_at,mode) VALUES('other','fixture','active','fixture','audit')")
        runtime.initialize(conn, 'other')
        runtime.activate(conn, 'other')
        assert runtime.pause(conn, pid)['state'] == 'pausing'
    now[0] += 2
    with db.get_conn() as conn:
        runtime.heartbeat(conn)
        paused = runtime.load(conn, pid)
        other = runtime.load(conn, 'other')
    assert paused['state'] == 'pausing' and paused['elapsed'] == 0
    assert paused['valid_until'] is None
    assert other['state'] == 'running' and other['elapsed'] == 2


def test_pause_cleanup_and_resume_have_one_generation_boundary(audit, tmp_path, monkeypatch):
    _client, _http, config, pid, _repo = audit
    with db.get_conn() as conn:
        runtime.initialize(conn, pid)
        runtime.activate(conn, pid)
        runtime.pause(conn, pid)
        runtime.freeze(conn, pid, 'paused')
        conn.execute("INSERT INTO audit_submissions(id,run_dir,revision,fingerprint,status,prepared_json,execution_json,project_id,created_at,updated_at) VALUES('fixture',?,1,'x','paused','{}',?,?,'now','now')",
                     (str(tmp_path/'run'), config.model_dump_json(), pid))
    entered, release = threading.Event(), threading.Event()
    calls = []
    def delayed_cleanup(*_args):
        calls.append('groups')
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(local, 'stop_agent_groups', delayed_cleanup)
    service = ManagedService.__new__(ManagedService)
    service.directory = tmp_path/'service'
    completed = Future()
    completed.set_result(None)
    service.jobs = {'fixture': (completed, None)}
    service.dispatcher = SimpleNamespace(cancel_project_tasks=lambda *_: calls.append('dispatcher'))
    service.resume(pid)
    # A pausing target read by the old control iteration is now obsolete.
    service._pause_calls(pid)
    assert calls == []
    with db.get_conn() as conn:
        assert runtime.load(conn, pid)['state'] == 'resuming'
        runtime.activate(conn, pid)
        conn.execute("UPDATE projects SET status='active' WHERE id=?", (pid,))
    service.pause(pid)
    with ThreadPoolExecutor(max_workers=1) as executor:
        cleanup = executor.submit(service._pause_calls, pid)
        try:
            assert entered.wait(3)
            # The old job can publish paused while process cleanup is still busy.
            with db.get_conn() as conn:
                runtime.freeze(conn, pid, 'paused')
                conn.execute("UPDATE audit_submissions SET status='paused' WHERE project_id=?", (pid,))
            with pytest.raises(HTTPException) as rejected:
                service.resume(pid)
            assert rejected.value.status_code == 409
            with db.get_conn() as conn:
                assert runtime.load(conn, pid)['generation'] == 2
        finally:
            release.set()
        cleanup.result(timeout=3)
    service.resume(pid)
    service._pause_calls(pid)
    assert calls == ['dispatcher', 'groups']
    with db.get_conn() as conn:
        data = runtime.load(conn, pid)
        assert data['state'] == 'resuming' and data['generation'] == 3
        assert conn.execute('SELECT status FROM audit_submissions').fetchone()[0] == 'resuming'
        runtime.activate(conn, pid)


@pytest.mark.parametrize('action', ['pause', 'resume'])
@pytest.mark.parametrize('saved_project', [None, 'obsolete-local-project'])
def test_control_run_uses_authoritative_submission_project(tmp_path, monkeypatch, action, saved_project):
    folder = tmp_path/'run'
    folder.mkdir()
    locator = {'submission_id': 'stable-submission', 'database_id': 'same-db', 'service_id': 'old-instance',
               'run_dir': str(folder), 'status': 'pending', 'project_id': saved_project}
    (folder/'submission.json').write_text(json.dumps(locator))
    calls = []
    class Client:
        def __init__(self, *_args, **_kwargs): pass
        def close(self): pass
        def service_health(self):
            return {'managed': True, 'ready': True, 'database_id': 'same-db', 'service_id': 'new-instance'}
        def get_submission(self, sid):
            calls.append(('lookup', sid))
            return {**locator, 'project_id': 'registered-project', 'status': 'interrupted', 'service_id': 'new-instance'}
        def get_runtime(self, pid):
            assert pid == 'registered-project'
            return {'run_dir': str(folder)}
        def control_runtime(self, pid, command, body):
            calls.append((command, pid))
            assert body == {'database_id': 'same-db', 'service_id': 'new-instance'}
            return ApiResult(202, {'project_id': pid})
    monkeypatch.setattr(client_module, 'CairnClient', Client)
    assert client_module.control_prepared('http://127.0.0.1:8000', action, run_dir=folder)['project_id'] == 'registered-project'
    assert calls == [('lookup', 'stable-submission'), (action, 'registered-project')]
    assert json.loads((folder/'submission.json').read_text())['project_id'] == 'registered-project'


def test_control_run_rejects_other_database_before_lookup(tmp_path, monkeypatch):
    folder = tmp_path/'run'
    folder.mkdir()
    locator = {'submission_id': 'stable', 'database_id': 'original-db', 'project_id': None}
    path = folder/'submission.json'
    path.write_text(json.dumps(locator))
    class Client:
        def __init__(self, *_args, **_kwargs): pass
        def close(self): pass
        def service_health(self):
            return {'managed': True, 'ready': True, 'database_id': 'other-db', 'service_id': 'other-instance'}
        def get_submission(self, _sid): pytest.fail('must reject before lookup')
    monkeypatch.setattr(client_module, 'CairnClient', Client)
    with pytest.raises(ValueError, match='another Cairn database'):
        client_module.control_prepared('http://127.0.0.1:8000', 'resume', run_dir=folder)
    assert json.loads(path.read_text()) == locator


def _exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize('failure', ['kill', 'stop'])
def test_agents_and_descendants_exit_without_service_restart(probe, tmp_path, monkeypatch, failure):
    config, prepared, _plan = configured(probe)
    payload = config.model_dump(mode='json')
    payload['runtime']['max_running_projects'] = 2
    payload['tasks']['explore']['timeout'] = 60
    config = DispatchConfig.model_validate(payload)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        config.server = f'http://127.0.0.1:{listener.getsockname()[1]}'
    config_path = tmp_path/'service.json'
    config_path.write_text(config.model_dump_json())
    fake = FAKE_CLI.replace('import fcntl, json,', 'import signal, subprocess, fcntl, json,')
    fake = fake.replace('    release = pathlib.Path', '''    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
    release = pathlib.Path''')
    fake = fake.replace('"pid": os.getpid(), "harness":', '"pid": os.getpid(), "child_pid": child.pid, "project": pathlib.Path.cwd().name, "harness":')
    for name in ('claude', 'grok'):
        binary = tmp_path/'bin'/name
        binary.write_text(f'#!{sys.executable}\n{fake}')
        binary.chmod(0o755)
    release = tmp_path/'release'
    monkeypatch.setenv('SERVICE_TEST_RELEASE', str(release))
    original = Path(prepared['run_dir'])
    second = tmp_path/'second-run'
    shutil.copytree(original, second)
    for path in second.rglob('*.json'):
        path.write_text(path.read_text().replace(str(original), str(second)))
    registry = tmp_path/'managed-service/agent-processes'
    log = (tmp_path/'service.log').open('w')
    process = subprocess.Popen([*CLI, 'up', '--config', str(config_path), '--db-path', str(tmp_path/'managed.db')],
                               stdout=log, stderr=log, start_new_session=True)
    calls_path = release.with_suffix('.jsonl')
    try:
        with closing(CairnClient(config.server, timeout=2, trust_env=False)) as client:
            def healthy():
                try: return (h if (h := client.service_health())['ready'] else None)
                except Exception: return None
            health = wait_until(healthy)
            identity = {key: health[key] for key in ('database_id', 'service_id')}
            for folder, harness in ((original, 'claudecode'), (second, 'grok')):
                data = config.model_dump(mode='json')
                data['role_pools']['explore'] = {harness: 1}
                response = client.submit_audit({'run_dir': str(folder), 'execution': execution_snapshot(DispatchConfig.model_validate(data)).model_dump(mode='json'), **identity})
                assert response.ok, response.text
                sid = response.data['submission_id']
                wait_until(lambda: client.get_submission(sid)['status'] == 'running')
            wait_until(lambda: calls_path.exists() and len(calls_path.read_text().splitlines()) == 2)
            actors = [json.loads(line) for line in calls_path.read_text().splitlines()]
            records = [json.loads((registry/f"{actor['pid']}.json").read_text()) for actor in actors]
            targets = [pid for actor in actors for pid in (actor['pid'], actor['child_pid'])]
            targets += [record['guardian_pid'] for record in records]
            start = time.monotonic()
            os.kill(process.pid, signal.SIGKILL if failure == 'kill' else signal.SIGSTOP)
            if failure == 'kill':
                process.wait(timeout=5)
            def exited():
                if failure == 'kill':
                    return not any(_exists(pid) for pid in targets)
                # A SIGSTOPed parent cannot reap its already exited children.
                result = subprocess.run(['ps', '-o', 'pid=,stat=', '-p', ','.join(map(str, targets))], capture_output=True, text=True)
                return all(line.split()[1].startswith('Z') for line in result.stdout.splitlines() if line.split())
            wait_until(exited, seconds=16.5)
            observation = {'failure': failure, 'seconds_to_all_exit': round(time.monotonic()-start, 2),
                           'target_pids': targets, 'restarted': False}
            (tmp_path/'lease-exit.json').write_text(json.dumps(observation, indent=2))
            assert (process.poll() is None) == (failure == 'stop')
    finally:
        if process.poll() is None:
            if failure == 'stop': os.kill(process.pid, signal.SIGCONT)
            process.send_signal(signal.SIGTERM)
            try: process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stop_agent_groups(registry, {process.pid})
        release.touch()
        log.close()


def test_no_wait_run_locator_controls_original_online_project(probe, tmp_path, monkeypatch):
    config, prepared, _plan = configured(probe)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        config.server = f'http://127.0.0.1:{listener.getsockname()[1]}'
    config_path = tmp_path/'service.json'
    config_path.write_text(config.model_dump_json())
    binary = tmp_path/'bin/claude'
    binary.write_text(f'#!{sys.executable}\n{FAKE_CLI}')
    binary.chmod(0o755)
    release = tmp_path/'release'
    monkeypatch.setenv('SERVICE_TEST_RELEASE', str(release))
    folder = Path(prepared['run_dir'])
    log = (tmp_path/'service.log').open('w')
    process = subprocess.Popen([*CLI, 'up', '--config', str(config_path), '--db-path', str(tmp_path/'managed.db')],
                               stdout=log, stderr=log, start_new_session=True)
    try:
        with closing(CairnClient(config.server, timeout=2, trust_env=False)) as client:
            def healthy():
                try: return client.service_health()['ready']
                except Exception: return False
            wait_until(healthy)
            early = client_module.submit_prepared(config.server, folder, wait=False)
            assert early['project_id'] is None
            sid = early['submission_id']
            current = wait_until(lambda: (row if (row := client.get_submission(sid))['status'] == 'running' else None))
            pid = current['project_id']
            assert client_module.control_prepared(config.server, 'pause', run_dir=folder)['project_id'] == pid
            wait_until(lambda: client.get_runtime(pid)['state'] == 'paused')
            (folder/'submission.json').write_text(json.dumps(early))
            resumed = client_module.control_prepared(config.server, 'resume', run_dir=folder)
            assert resumed['project_id'] == pid
            wait_until(lambda: client.get_runtime(pid)['state'] == 'running')
            assert client.get_runtime(pid)['generation'] == 2
            assert len(client.list_projects()) == 1
            assert json.loads((folder/'submission.json').read_text())['project_id'] == pid
            assert client_module.control_prepared(config.server, 'pause', run_dir=folder)['project_id'] == pid
            wait_until(lambda: client.get_runtime(pid)['state'] == 'paused')
    finally:
        release.touch()
        process.send_signal(signal.SIGTERM)
        try: process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
