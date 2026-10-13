from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from cairn.controller import local
from cairn.server import services, audit_review
from cairn.server.routers import audits, projects, intents
from test_audit_workflow import api
from test_audit_review import audit

ROOT = Path(__file__).resolve().parents[2]


def test_r1_parallel_cancellation_does_not_outlive_launcher(tmp_path):
    fixture = ROOT / "cairn/tests/fixtures/owned-dispatcher.py"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "cairn/src"), "PYTHONDONTWRITEBYTECODE": "1"}
    actors = []
    with (tmp_path / "dispatcher.log").open("w") as log:
        parent = subprocess.Popen([sys.executable, "-B", str(fixture), str(tmp_path), "cooperative"],
                                  env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "ready.json").exists():
                assert parent.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)
            actors = json.loads((tmp_path / "ready.json").read_text())["actors"]
            start = time.monotonic()
            local.stop_processes({"dispatcher": parent})
            elapsed = time.monotonic() - start
            first = [Path(actor["heartbeat"]).read_text() for actor in actors]
            time.sleep(0.2)
            assert all(Path(actor["heartbeat"]).read_text() == value for actor, value in zip(actors, first))
            assert parent.returncode == 0 and elapsed < 12
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.wait()
            for actor in actors:
                try:
                    os.killpg(actor["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_r1_forced_parent_exit_reclaims_registered_groups_only(tmp_path, monkeypatch):
    from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV

    registry = tmp_path / "agent-processes"
    registry.mkdir()
    fixture = ROOT / "cairn/tests/fixtures/owned-dispatcher.py"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "cairn/src"), "PYTHONDONTWRITEBYTECODE": "1",
           REGISTRY_ENV: str(registry)}
    monkeypatch.setattr(local, "SHUTDOWN_SECONDS", 0.2)
    monkeypatch.setattr(local, "AGENT_SHUTDOWN_SECONDS", 0.2)
    outsider = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"], start_new_session=True)
    actors = []
    with (tmp_path / "dispatcher.log").open("w") as log:
        parent = subprocess.Popen([sys.executable, "-B", str(fixture), str(tmp_path), "force"],
                                  env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "ready.json").exists():
                assert parent.poll() is None, (tmp_path / "dispatcher.log").read_text()
                assert time.monotonic() < deadline
                time.sleep(0.02)
            actors = json.loads((tmp_path / "ready.json").read_text())["actors"]
            assert len(list(registry.glob("*.json"))) == 5
            # 不属于启动器的 owner 与旧 PID 身份记录，均不能使对照进程收到信号。
            (registry / "outside-owner.json").write_text(json.dumps({"pid": outsider.pid, "parent_pid": os.getpid(), "identity": "other owner"}))
            (registry / "old-identity.json").write_text(json.dumps({"pid": outsider.pid, "parent_pid": parent.pid, "identity": "old process identity"}))
            start = time.monotonic()
            local.stop_processes({"dispatcher": parent}, registry)
            assert time.monotonic() - start < 8
            assert parent.returncode == -signal.SIGKILL
            first = [Path(actor["heartbeat"]).read_text() for actor in actors]
            time.sleep(0.2)
            assert all(Path(actor["heartbeat"]).read_text() == value for actor, value in zip(actors, first))
            for actor in actors:
                with pytest.raises(ProcessLookupError):
                    os.killpg(actor["pid"], 0)
            assert outsider.poll() is None
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.wait()
            for actor in actors:
                try:
                    os.killpg(actor["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            outsider.terminate()
            outsider.wait()


@pytest.fixture
def ui_fixture(audit, monkeypatch):
    client, http, _config, pid, _repo = audit
    before = client.get_project(pid).model_dump(mode="json", by_alias=True)
    response = client.complete(pid, ["f001"], "source evidence available", "claude-main", "research-session")
    assert response.status_code == 202
    pending = client.get_project(pid).model_dump(mode="json", by_alias=True)
    metadata = client.get_audit(pid)
    now = (datetime.fromisoformat(metadata.final_deadline.replace("Z", "+00:00")) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for module in (services, audit_review, audits, projects, intents):
        monkeypatch.setattr(module, "utcnow", lambda: now)
    assert client.audit_budget(pid, finalize=True).ok
    ended = client.get_project(pid).model_dump(mode="json", by_alias=True)
    assert ended["project"]["status"] == "budget_exhausted" and ended["project"]["audit"]["completion_pending"]
    generic = http.post("/projects", json={"title": "generic fixture", "origin": "start", "goal": "finish"}).json()
    generic_response = http.post(f"/projects/{generic['project']['id']}/complete", json={"from": ["origin"], "description": "finished", "worker": "human"})
    assert generic_response.status_code == 200
    return {"audit_before": before, "audit_pending": pending, "audit_ended": ended,
            "audit_response": response.data, "generic_before": generic,
            "generic_completed": http.get(f"/projects/{generic['project']['id']}").json(),
            "generic_response": generic_response.json()}


def run_ui(fixture, checks):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for original UI async handlers")
    html = (ROOT / "cairn/src/cairn/server/static/index.html").read_text()
    code = "function cairnApp()" + html.split("<script>\nfunction cairnApp()", 1)[1].split("</script>", 1)[0]
    runtime_ui = (ROOT / "cairn/src/cairn/server/static/runtime-panel.js").read_text()
    online = """
const originalApp = cairnApp;
cairnApp = () => {
  const app = originalApp();
  app.connectionState = 'online';
  app.graphSnapshotReady = true;
  app.refreshServiceHealth = async () => true;
  app.loadRuntime = async () => ({});
  return app;
};
"""
    source = "const assert=require('node:assert/strict');const location={hash:''};const fixture=" + json.dumps(fixture) + ";\n" + runtime_ui + code + online + checks
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_r2_terminal_audit_precedes_pending_requests(ui_fixture):
    run_ui(ui_fixture, """
const app=cairnApp();
assert.equal(app.auditPhaseLabel(fixture.audit_pending.project),'待独立验收');
for (const status of ['completed','budget_exhausted']) {
  const target=structuredClone(fixture.audit_ended.project);target.status=status;
  for(const request of ['pending','stale','review_error']) {
    target.audit.completion_pending=true;
    assert.equal(app.auditPhaseLabel(target),'已结束',request);
  }
}
assert.equal(app.auditPhaseLabel({status:'stopped',audit:{phase:'preparing'}}),'准备中');
assert.equal(app.auditPhaseLabel({status:'stopped',audit:{phase:'researching',completion_pending:true}}),'已停止');
""")


def test_r3_delayed_completion_keeps_operation_target(ui_fixture):
    run_ui(ui_fixture, """
(async()=>{
for(const direction of ['audit-generic','generic-audit','audit-stays','generic-stays']) {
  const app=cairnApp();
  const generic=direction.startsWith('generic');
  const initial=generic?fixture.generic_before:fixture.audit_before;
  const other=generic?fixture.audit_before:fixture.generic_before;
  const response=generic?fixture.generic_response:fixture.audit_response;
  app.project=structuredClone(initial);app.selectedProjectId=initial.project.id;
  app.view='graph';app.selectedFacts=generic?['origin']:['f001'];
  app.completeForm={description:'candidate'};app.showCompleteModal=true;
  app.$nextTick=()=>{};app.rememberProjectListScroll=()=>{};
  app.updateGraph=()=>{};const toasts=[];app.showToast=m=>toasts.push(m);
  let release;const blocked=new Promise(r=>release=r);const calls=[];
  app.api=async(method,url,body)=>{
    calls.push({method,url,body});
    if(method==='POST')return blocked;
    if(url==='/projects')return [];
    if(url.endsWith('/audit/completions'))return generic?[]:[fixture.audit_response];
    if(url===`/projects/${other.project.id}`)return structuredClone(other);
    if(url===`/projects/${initial.project.id}`)return structuredClone(generic?fixture.generic_completed:fixture.audit_pending);
    throw Error(url);
  };
  const pending=app.completeProject();
  const navigate=!direction.endsWith('stays');
  if(navigate){
    app.showCompleteModal=false;await app.openProject(other.project.id);
    app.selectedFacts=generic?['f001']:['origin'];
    app.openCompleteProject();
    app.completeForm.description='other project draft';
  }
  release(structuredClone(response));await pending;
  const message=toasts.at(-1);
  assert.equal(calls[0].url,`/projects/${initial.project.id}/complete`);
  if(!generic){assert.match(message,/完成申请：pending/);assert.doesNotMatch(message,/Project completed/);}
  else assert.match(message,/Project completed/);
  if(navigate){
    assert.equal(app.selectedProjectId,other.project.id);
    assert.equal(app.project.project.id,other.project.id);
    assert.equal(app.project.project.status,other.project.status);
    assert.equal(app.showCompleteModal,true);
    assert.equal(app.completeForm.description,'other project draft');
    assert.match(message,new RegExp(initial.project.id));
  }
}
})().catch(e=>{console.error(e);process.exitCode=1});
""")
