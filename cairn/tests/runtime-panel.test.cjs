const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const cytoscape = require('../src/cairn/server/static/vendor/cytoscape.min.js');

function app() {
  const folder = path.join(__dirname, '../src/cairn/server/static');
  const context = vm.createContext({ console, setTimeout, location: { origin: 'http://localhost:8000' } });
  vm.runInContext(fs.readFileSync(path.join(folder, 'runtime-panel.js'), 'utf8'), context);
  const html = fs.readFileSync(path.join(folder, 'index.html'), 'utf8');
  const code = 'function cairnApp()' + html.split('<script>\nfunction cairnApp()')[1].split('</script>')[0];
  vm.runInContext(code, context);
  const instance = vm.runInContext('cairnApp()', context);
  const loadProject = instance.loadProject;
  instance.showToast = () => {};
  instance.loadProject = async () => {};
  instance.connectionState = 'online';
  instance.databaseId = 'db';
  instance.serviceHealth = { database_id: 'db', service_id: 'owner', ready: true };
  instance.selectedProjectId = 'p';
  instance.runtimeByProject.p = { project_id: 'p', managed: true, service_ready: true,
    service_id: 'owner', database_id: 'db', state: 'running', generation: 1, available_actions: ['pause'],
    elapsed_seconds: 120, budget_total_seconds: 600, stage_remaining_seconds: 300 };
  return { instance, context, loadProject };
}

test('initial health and list requests work before any project is selected', async () => {
  const { instance, context } = app();
  instance.project = null;
  const requests = [];
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ ready: true }) };
  };
  await instance.api('GET', '/runtime/health');
  await instance.api('GET', '/projects');
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.headers['X-Cairn-Generation'], undefined);
  instance.project = { project: { id: 'p', runtime_generation: 3 } };
  await instance.api('POST', '/projects/p/hints', { content: 'hint' });
  assert.equal(requests[2].options.headers['X-Cairn-Generation'], '3');
});

test('a pending control request disables a second click', async () => {
  const { instance } = app();
  let release, calls = 0;
  instance.api = async () => { calls++; return new Promise(resolve => { release = resolve; }); };
  const pending = instance.controlRuntime('p', 'pause');
  assert.equal(instance.canRuntimeAction('p', 'pause'), false);
  await instance.controlRuntime('p', 'pause');
  assert.equal(calls, 1);
  release({ ...instance.runtimeByProject.p, state: 'pausing', available_actions: [] });
  await pending;
  assert.equal(instance.runtimeActionBusy.p, false);
  assert.equal(instance.canRuntimeAction('p', 'pause'), false);
});

test('offline task writes never call fetch, and preserve the last budget', async () => {
  const { instance, context } = app();
  instance.connectionState = 'offline';
  context.fetch = () => { throw new Error('must not fetch'); };
  await assert.rejects(instance.api('POST', '/projects/p/hints', { content: 'hint' }), /当前连接不可写/);
  assert.equal(instance.runtimeFor().stage_remaining_seconds, 300);
  assert.equal(instance.timeBudgetPercent(), 20);
  assert.equal(instance.projectCanWriteHints(), false);
  assert.equal(instance.canRuntimeAction('p', 'pause'), false);
});

test('reconnecting to a different database keeps the pinned snapshot', async () => {
  const { instance, context } = app();
  let requests = 0;
  context.fetch = async () => {
    requests++;
    return { ok: true, status: 200, json: async () => ({ database_id: 'other-db', service_id: 'other-owner', ready: true }) };
  };
  assert.equal(await instance.refreshServiceHealth(), false);
  assert.equal(instance.connectionState, 'changed');
  assert.equal(instance.databaseId, 'db');
  assert.equal(instance.runtimeFor().generation, 1);
  await assert.rejects(instance.api('POST', '/projects/p/runtime/pause', {}), /数据库身份已变化/);
  await assert.rejects(instance.fetchText('/projects/p/export'), /原数据库/);
  assert.equal(requests, 1);
});

test('same-database restart permits control only after a fresh runtime', async () => {
  const { instance } = app();
  instance.api = async () => ({ database_id: 'db', service_id: 'new-owner', ready: true });
  assert.equal(await instance.refreshServiceHealth(), true);
  assert.equal(instance.canRuntimeAction('p', 'pause'), false);
  instance.api = async () => ({ ...instance.runtimeByProject.p, service_id: 'new-owner',
    state: 'interrupted', available_actions: ['resume'], updated_at: 'now' });
  await instance.loadRuntime('p');
  assert.equal(instance.canRuntimeAction('p', 'resume'), true);
});

test('failed controls release busy state and retain the server state', async () => {
  const { instance } = app();
  instance.api = async () => { throw new Error('cleanup in progress'); };
  await assert.rejects(instance.controlRuntime('p', 'pause'), /cleanup in progress/);
  assert.equal(instance.runtimeActionBusy.p, false);
  assert.equal(instance.runtimeFor().state, 'running');
  assert.equal(instance.runtimeActionError, 'cleanup in progress');
});

test('unknown occupancy is not converted to zero and capacities sum by harness', () => {
  const { instance } = app();
  Object.assign(instance.runtimeByProject.p, { role_pools: { explore: { claudecode: 3, grok: 2 } },
    occupancy_known: false, slots_in_use: null });
  const row = instance.rolePoolRows()[0];
  assert.equal(row.capacity, 5);
  assert.equal(row.used, null);
  assert.equal(row.harnesses[0].used, null);
});

test('legacy budget is required before sending a resume and may be zero', async () => {
  const { instance } = app();
  Object.assign(instance.runtimeByProject.p, { state: 'needs_budget', needs_budget: true, available_actions: ['resume'] });
  let sent;
  instance.api = async (_method, _url, body, headers) => {
    sent = { body, headers };
    return { ...instance.runtimeByProject.p, state: 'resuming', available_actions: [] };
  };
  await instance.controlRuntime('p', 'resume');
  assert.equal(sent, undefined);
  instance.legacyRemainingSeconds = '0';
  await instance.controlRuntime('p', 'resume');
  assert.equal(sent.body.remaining_seconds, 0);
  assert.equal(sent.headers['X-Cairn-Generation'], '1');
});

test('batch pause uses only allowed managed targets and reports each failure', async () => {
  const { instance } = app();
  instance.projects = [{ id: 'p', title: 'allowed', status: 'active' }, { id: 'q', title: 'busy', status: 'active' },
    { id: 'g', title: 'generic', status: 'active' }];
  instance.runtimeByProject.q = { ...instance.runtimeByProject.p, state: 'resuming', available_actions: [] };
  let paused;
  instance.controlRuntime = async id => { paused = id; };
  instance.setProjectStatus = async () => { throw new Error('generic failure'); };
  instance.loadProjects = async () => {};
  await instance.stopAllActiveProjects();
  assert.equal(paused, 'p');
  assert.equal(instance.batchResults.length, 2);
  assert.equal(instance.batchResults[0].status, '暂停已请求');
  assert.equal(instance.batchResults[1].status, '失败');
  assert.equal(instance.batchResults[1].error, 'generic failure');
});

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

function project(id, title = id) {
  return { project: { id, title, mode: 'audit', status: 'active' }, facts: [], intents: [], hints: [] };
}

function runtime(instance, id, generation = 1) {
  return { ...instance.runtimeByProject.p, project_id: id, generation, updated_at: id };
}

test('late project runtime and completion responses cannot replace the selected graph', async () => {
  const { instance, loadProject } = app();
  instance.loadProject = loadProject;
  const entered = deferred(), gate = deferred();
  instance.api = async (_method, url) => {
    if (url === '/projects/A/runtime') { entered.resolve(); return gate.promise; }
    if (url === '/projects/B/runtime') return runtime(instance, 'B');
    if (url.endsWith('/audit/completions')) return [{ id: 'B-review' }];
    return project(url.split('/')[2]);
  };
  instance.selectedProjectId = 'A';
  const old = instance.loadProject('A');
  await entered.promise;
  instance.selectedProjectId = 'B';
  await instance.loadProject('B');
  gate.resolve(runtime(instance, 'A'));
  assert.equal(await old, false);
  assert.equal(instance.project.project.id, 'B');
  assert.equal(instance.auditRequests[0].id, 'B-review');
  assert.equal(instance.lastSnapshotAt, 'B');

  const review = deferred(), reviewEntered = deferred();
  instance.api = async (_method, url) => {
    if (url === '/projects/B/audit/completions') { reviewEntered.resolve(); return review.promise; }
    if (url.endsWith('/runtime')) return runtime(instance, url.split('/')[2]);
    if (url.endsWith('/audit/completions')) return [{ id: 'C-review' }];
    return project(url.split('/')[2]);
  };
  const oldReview = instance.loadProject('B');
  await reviewEntered.promise;
  instance.selectedProjectId = 'C';
  await instance.loadProject('C');
  review.resolve([{ id: 'late-B-review' }]);
  await oldReview;
  assert.equal(instance.project.project.id, 'C');
  assert.equal(instance.auditRequests[0].id, 'C-review');
  assert.equal(instance.lastSnapshotAt, 'C');
});

test('returning to the list invalidates an unfinished graph load', async () => {
  const { instance, loadProject } = app();
  instance.loadProject = loadProject;
  instance.loadProjects = async () => {};
  instance.restoreProjectListScroll = () => {};
  const gate = deferred(), entered = deferred();
  instance.api = async (_method, url) => {
    if (url.endsWith('/runtime')) { entered.resolve(); return gate.promise; }
    return project('p');
  };
  const old = instance.loadProject('p');
  await entered.promise;
  instance.backToList(true);
  gate.resolve(runtime(instance, 'p'));
  await old;
  assert.equal(instance.view, 'list');
  assert.equal(instance.project, null);
  assert.equal(instance.selectedProjectId, '');
});

test('a newer same-project load keeps its graph and runtime when an old response arrives', async () => {
  const { instance, loadProject } = app();
  instance.loadProject = loadProject;
  const gate = deferred(), entered = deferred();
  let projects = 0, runtimes = 0;
  instance.api = async (_method, url) => {
    if (url.endsWith('/runtime')) {
      if (++runtimes === 1) { entered.resolve(); return gate.promise; }
      return runtime(instance, 'p', 2);
    }
    if (url.endsWith('/audit/completions')) return [];
    return project('p', `version-${++projects}`);
  };
  const old = instance.loadProject('p');
  await entered.promise;
  await instance.loadProject('p');
  gate.resolve(runtime(instance, 'p', 1));
  await old;
  assert.equal(instance.project.project.title, 'version-2');
  assert.equal(instance.runtimeFor().generation, 2);
});

test('a failed navigation keeps the old snapshot read-only instead of writing to the new target', async () => {
  const { instance, loadProject } = app();
  instance.loadProject = loadProject;
  instance.view = 'graph';
  instance.project = project('p');
  instance.graphSnapshotReady = true;
  instance.rememberProjectListScroll = () => {};
  instance.refreshServiceHealth = async () => true;
  instance.api = async () => { throw new Error('B unavailable'); };
  await instance.openProject('B');
  assert.equal(instance.project.project.id, 'p');
  assert.equal(instance.isOpeningProject, false);
  assert.equal(instance.graphSnapshotReady, false);
  assert.equal(instance.projectIsActive(), false);
  assert.equal(instance.projectCanWriteHints(), false);
  assert.equal(instance.canRuntimeAction('p', 'pause'), false);
});

test('a slow openProject cannot navigate or initialize a graph after a newer selection', async () => {
  const { instance, context, loadProject } = app();
  instance.loadProject = loadProject;
  instance.rememberProjectListScroll = () => {};
  instance.clampPanelWidth = value => value;
  const callbacks = [], initialized = [];
  instance.$nextTick = callback => callbacks.push(callback);
  instance.initGraph = () => initialized.push(instance.project.project.id);
  const health = deferred();
  let requests = 0;
  instance.refreshServiceHealth = async () => ++requests === 1 ? health.promise : true;
  instance.api = async (_method, url) => ({ ...project(url.split('/')[2]), project: {
    id: url.split('/')[2], mode: 'generic', status: 'active' } });
  const old = instance.openProject('A');
  await instance.openProject('B');
  health.resolve(true);
  await old;
  callbacks.forEach(callback => callback());
  assert.equal(instance.selectedProjectId, 'B');
  assert.equal(instance.project.project.id, 'B');
  assert.equal(context.location.hash, '/projects/B');
  assert.deepEqual(initialized, ['B']);
  assert.equal(instance.isOpeningProject, false);
});

test('an in-flight list response cannot cancel a foreground project open', async () => {
  const { instance, loadProject } = app();
  instance.loadProject = loadProject;
  instance.selectedProjectId = '';
  instance.project = null;
  instance.rememberProjectListScroll = () => {};
  instance.clampPanelWidth = value => value;
  const callbacks = [], initialized = [];
  instance.$nextTick = callback => callbacks.push(callback);
  instance.initGraph = () => initialized.push(instance.project.project.id);
  instance.refreshServiceHealth = async () => true;
  const list = deferred(), foreground = deferred(), entered = deferred();
  let runtimeCalls = 0;
  instance.api = async (_method, url) => {
    if (url === '/projects') return list.promise;
    if (url.endsWith('/runtime')) {
      if (++runtimeCalls === 1) { entered.resolve(); return foreground.promise; }
      return runtime(instance, 'p', 2);
    }
    if (url.endsWith('/audit/completions')) return [];
    return project('p');
  };
  const oldList = instance.loadProjects();
  const open = instance.openProject('p');
  await entered.promise;
  list.resolve([project('p').project]);
  await oldList;
  foreground.resolve(runtime(instance, 'p'));
  await open;
  callbacks.forEach(callback => callback());
  assert.equal(runtimeCalls, 1);
  assert.equal(instance.view, 'graph');
  assert.equal(instance.project.project.id, 'p');
  assert.deepEqual(initialized, ['p']);
});

test('a list runtime started before navigation cannot overwrite its newer runtime', async () => {
  const { instance, loadProject } = app();
  instance.loadProject = loadProject;
  instance.selectedProjectId = '';
  instance.rememberProjectListScroll = () => {};
  instance.$nextTick = () => {};
  instance.refreshServiceHealth = async () => true;
  const gate = deferred(), entered = deferred();
  let runtimeCalls = 0;
  instance.api = async (_method, url) => {
    if (url === '/projects') return [project('p').project];
    if (url.endsWith('/runtime')) {
      if (++runtimeCalls === 1) { entered.resolve(); return gate.promise; }
      return runtime(instance, 'p', 2);
    }
    if (url.endsWith('/audit/completions')) return [];
    return project('p');
  };
  const list = instance.loadProjects();
  await entered.promise;
  await instance.openProject('p');
  gate.resolve(runtime(instance, 'p', 1));
  await list;
  assert.equal(instance.view, 'graph');
  assert.equal(instance.runtimeFor().generation, 2);
  assert.equal(instance.project.project.id, 'p');
});

test('late log errors remain with their project and do not replace a successful new panel', async () => {
  const { instance } = app();
  const gate = deferred();
  instance.api = async (_method, url) => url.includes('/p/') ? gate.promise :
    { files: [], selected: null, text: 'B receipt', note: '' };
  const old = instance.loadRuntimeLogs();
  instance.selectedProjectId = 'B';
  await instance.loadRuntimeLogs();
  gate.resolve(Promise.reject(new Error('A receipt unavailable')));
  await old;
  assert.equal(instance.runtimeLogs.text, 'B receipt');
  assert.equal(instance.runtimeLogsError, '');
  assert.equal(instance.connectionError, '');
});

test('a newer log selection wins over an older response in the same project', async () => {
  const { instance } = app();
  const gate = deferred();
  instance.api = async (_method, url) => url.endsWith('old') ? gate.promise :
    { files: [], selected: 'new', text: 'new receipt', note: '' };
  const old = instance.loadRuntimeLogs('old');
  await instance.loadRuntimeLogs('new');
  gate.resolve({ files: [], selected: 'old', text: 'old receipt', note: '' });
  await old;
  assert.equal(instance.runtimeLogs.selected, 'new');
});

test('offline stops canvas loops, and stale callbacks cannot restart them after reconnect', async () => {
  const { instance, loadProject } = app();
  function element(data) {
    const scratch = new Map();
    return { calls: [], stopped: [], removed: [], inside: () => true, data: key => data[key], style: () => {},
      scratch(key, value) { if (arguments.length === 2) scratch.set(key, value); return scratch.get(key); },
      removeScratch: key => scratch.delete(key), removeStyle(name) { this.removed.push(name); },
      stop(clear, jump) { this.stopped.push([clear, jump]); },
      animate(properties, options) { this.calls.push({ properties, options }); } };
  }
  const node = element({ nodeType: 'in_progress' }), edge = element({ status: 'in_progress' });
  instance.cy = { nodes: () => [node], edges: () => [edge] };
  instance.graphSnapshotReady = true;
  instance.pulseActive();
  const oldNode = node.calls[0], oldEdge = edge.calls[0];
  instance.connectionFailure(Object.assign(new Error('offline'), { networkFailure: true }));
  oldNode.options.complete(); oldEdge.options.complete();
  assert.equal(node.calls.length, 1);
  assert.equal(edge.calls.length, 1);
  assert.deepEqual(node.stopped, [[true, false]]);
  assert.deepEqual(edge.stopped, [[true, false]]);
  instance.setConnectionState('online');
  instance.pulseActive();
  assert.equal(node.calls.length, 1);
  instance.loadProject = loadProject;
  instance.api = async (_method, url) => url.endsWith('/runtime') ? runtime(instance, 'p') :
    url.endsWith('/audit/completions') ? [] : project('p');
  await instance.loadProject('p');
  instance.pulseActive();
  oldNode.options.complete(); oldEdge.options.complete();
  assert.equal(node.calls.length, 2);
  assert.equal(edge.calls.length, 2);
  assert.ok(node.scratch('_pulseActive'));
  assert.ok(edge.scratch('_flowActive'));
  instance.setConnectionState('changed');
  assert.equal(node.stopped.length, 2);
  assert.equal(edge.stopped.length, 2);
});

test('historical replay is independent of live task state and stays frozen offline', () => {
  const { instance } = app();
  instance.replay.active = true;
  instance.graphSnapshotReady = true;
  for (const state of ['paused', 'interrupted', 'finished', 'blocked']) {
    instance.runtimeByProject.p.state = state;
    assert.equal(instance.canAnimateGraph(), true);
  }
  instance.connectionState = 'offline';
  assert.equal(instance.canAnimateGraph(), false);
  instance.connectionState = 'online';
  instance.graphSnapshotReady = false;
  assert.equal(instance.canAnimateGraph(), true);
  instance.replay.active = false;
  assert.equal(instance.canAnimateGraph(), false);
});

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

test('real Cytoscape restarts both node and edge animations after release and reclaim', async () => {
  const { instance } = app();
  instance.graphSnapshotReady = true;
  instance.cy = cytoscape({ headless: true, styleEnabled: true, elements: [
    { data: { id: 'origin', nodeType: 'origin' } }, { data: { id: 'intent', nodeType: 'in_progress' } },
    { data: { id: 'edge', source: 'origin', target: 'intent', status: 'in_progress' } },
  ], style: [{ selector: 'edge', style: { 'line-style': 'dashed' } }] });
  const node = instance.cy.$id('intent'), edge = instance.cy.$id('edge');
  try {
    instance.pulseActive();
    await wait(50);
    assert.equal(node.animated(), true);
    assert.equal(edge.animated(), true);
    node.data('nodeType', 'unclaimed'); edge.data('status', 'unclaimed');
    instance.pulseActive();
    await wait(1100);
    assert.equal(Boolean(node.scratch('_pulseActive')), false);
    assert.equal(Boolean(edge.scratch('_flowActive')), false);
    node.data('nodeType', 'in_progress'); edge.data('status', 'in_progress');
    instance.pulseActive();
    await wait(50);
    assert.equal(node.animated(), true);
    assert.equal(edge.animated(), true);
  } finally { instance.cy.elements().stop(true, false); instance.cy.destroy(); }
});

test('real historical intent_running frame animates even when its live project is finished', async () => {
  const { instance } = app();
  const source = { project: { id: 'p', title: 'Recorded work', mode: 'audit', status: 'completed', reason: null },
    facts: [{ id: 'origin', description: 'Start' }, { id: 'goal', description: 'Goal' }, { id: 'f1', description: 'Result' }],
    intents: [{ id: 'i1', from: ['origin'], to: 'f1', description: 'Explore', creator: 'reasoner', worker: 'worker',
      created_at: '2026-10-13T00:00:00Z', concluded_at: '2026-10-13T00:01:00Z' }], hints: [] };
  const frames = instance.buildReplayFrames(source, [{ type: 'project_created' },
    { type: 'intent_declared', intentId: 'i1', order: '1' }, { type: 'intent_concluded', intentId: 'i1' }]);
  instance.runtimeByProject.p.state = 'finished';
  instance.replay = { ...instance.replay, active: true, playing: true, frames };
  instance.followReplayTimelineTail = () => {};
  instance.applyReplayFrame(frames.findIndex(frame => frame.event.type === 'intent_running'));
  const { nodes, edges } = instance.buildElements();
  instance.cy = cytoscape({ headless: true, styleEnabled: true,
    elements: JSON.parse(JSON.stringify([...nodes, ...edges])),
    style: JSON.parse(JSON.stringify(instance.graphStyles())) });
  try {
    instance.pulseActive();
    await wait(50);
    assert.ok(instance.cy.nodes('[nodeType="in_progress"]').some(n => n.animated()));
    assert.ok(instance.cy.edges('[status="in_progress"]').some(e => e.animated()));
    instance.connectionFailure(Object.assign(new Error('offline'), { networkFailure: true }));
    assert.equal(instance.cy.elements().some(e => e.animated()), false);
    instance.api = async () => ({ database_id: 'db', service_id: 'owner', ready: true });
    await instance.refreshServiceHealth();
    await wait(50);
    assert.ok(instance.cy.edges('[status="in_progress"]').some(e => e.animated()));
  } finally { instance.cy.elements().stop(true, false); instance.cy.destroy(); }
});

function provenanceProject(instance, id = 'p') {
  instance.selectedProjectId = id;
  instance.project = { project: { id, mode: 'generic', reason: null },
    facts: [{ id: 'origin' }, { id: 'goal' }, { id: 'f1' }, { id: 'seed' }],
    intents: [{ id: 'i1', creator: 'planner', worker: 'runner', to: 'f1' }], hints: [{ id: 'h1' }] };
  instance.provenanceProjectId = id;
  instance.provenance = {
    facts: { origin: { kind: 'input', producer: null }, goal: { kind: 'input', producer: null },
      f1: { kind: 'research', producer: { worker: 'runner', harness: 'codex' } },
      seed: { kind: 'import', producer: null } },
    intents: { i1: { kind: 'research', creator: { worker: 'planner', harness: 'grok' },
      executor: { worker: 'runner', harness: 'codex' } } },
    hints: { h1: { kind: 'hint', producer: { worker: 'observer', harness: 'pi' } } },
  };
}

test('provenance separates Intent proposal, Fact production, Hint, and imported input', () => {
  const { instance } = app();
  provenanceProject(instance);
  const intentRows = instance.provenanceRows('intents', 'i1');
  assert.equal(intentRows[0].label, 'Intent 提出来源');
  assert.match(intentRows[0].value, /grok.*planner/);
  assert.match(intentRows[1].value, /codex.*runner/);
  assert.match(instance.provenanceRows('facts', 'f1')[0].value, /codex.*runner/);
  assert.match(instance.provenanceRows('hints', 'h1')[0].value, /pi.*observer/);
  assert.equal(instance.provenanceRows('facts', 'origin')[0].value, '项目输入（非研究产出）');
  assert.equal(instance.provenanceRows('facts', 'seed')[0].value, '开题导入（原作者未记录）');
  instance.provenance.intents.i1.kind = 'completion';
  assert.equal(instance.provenanceRows('intents', 'i1')[1].label, '验收 / 完成写入');
  instance.provenance.intents.i1.creator = { worker: 'audit-controller', harness: null };
  instance.provenance.intents.i1.executor = { worker: 'final-review-grok', harness: 'grok' };
  instance.project.intents[0].worker = 'final-review-grok';
  assert.equal(instance.provenanceRows('intents', 'i1')[0].label, '完成节点创建方');
  assert.equal(instance.provenanceRows('intents', 'i1')[0].value, 'Harness 未记录 · Worker audit-controller');
  assert.equal(instance.provenanceRows('intents', 'i1')[1].value, 'grok · Worker final-review-grok');
  instance.provenance.intents.i1.executor = { worker: 'runner', harness: 'codex' };
  instance.project.intents[0].worker = 'runner';
  assert.equal(instance.project.facts[2].provenance, undefined);
  instance.provenance.intents.i1.kind = 'import';
  assert.match(instance.provenanceRows('intents', 'i1')[0].value, /开题导入/);
  assert.match(instance.provenanceRows('intents', 'i1')[1].value, /codex.*runner/);
  instance.provenance.intents.i1.executor = null;
  assert.equal(instance.provenanceRows('intents', 'i1').length, 1);
});

test('provenance never infers a harness from worker names and preserves literal text', () => {
  const { instance } = app();
  provenanceProject(instance);
  instance.provenance.facts.f1.producer = { worker: 'pool-explore-codex', harness: null };
  assert.equal(instance.provenanceRows('facts', 'f1')[0].value, 'Harness 未记录 · Worker pool-explore-codex');
  instance.provenance.facts.f1.producer = { worker: '<img src=x onerror=alert(1)>', harness: 'custom' };
  assert.match(instance.provenanceRows('facts', 'f1')[0].value, /<img src=x onerror=alert\(1\)>/);
  const html = fs.readFileSync(path.join(__dirname, '../src/cairn/server/static/index.html'), 'utf8');
  assert.ok(html.includes('x-text="`${row.label}：${row.value}`"'));
  assert.equal(html.includes('x-html="`${row.label}：${row.value}`"'), false);
  instance.provenance = null;
  assert.equal(instance.provenanceRows('facts', 'f1')[0].value, 'Harness 未记录');
});

test('replay only exposes provenance for visible nodes and the reached execution phase', () => {
  const { instance } = app();
  provenanceProject(instance);
  const source = instance.cloneData(instance.project);
  Object.assign(source.intents[0], { from: ['origin'], description: 'Research', created_at: '2026-10-16T00:00:00Z',
    concluded_at: '2026-10-16T00:01:00Z' });
  const frames = instance.buildReplayFrames(source, [{ type: 'project_created' },
    { type: 'intent_declared', intentId: 'i1', order: '1' }, { type: 'intent_concluded', intentId: 'i1' }]);
  instance.replay.active = true;
  instance.project = frames[0].project;
  assert.equal(instance.provenanceRows('intents', 'i1').length, 0);
  assert.equal(instance.provenanceRows('facts', 'f1').length, 0);
  assert.equal(instance.provenanceRows('hints', 'h1').length, 0);
  instance.project = frames.find(frame => frame.event.type === 'intent_declared').project;
  assert.equal(instance.provenanceRows('intents', 'i1').length, 1);
  assert.match(instance.provenanceRows('intents', 'i1')[0].value, /grok/);
  instance.project = frames.find(frame => frame.event.type === 'intent_running').project;
  assert.equal(instance.provenanceRows('intents', 'i1').length, 2);
  assert.equal(instance.provenanceRows('facts', 'f1').length, 0);
  instance.project = frames.find(frame => frame.event.type === 'intent_concluded').project;
  assert.match(instance.provenanceRows('facts', 'f1')[0].value, /codex/);
});

test('a slow optional provenance read does not block graph loading', async () => {
  const { instance, loadProject } = app();
  instance.loadProject = loadProject;
  const gate = deferred(), entered = deferred();
  instance.api = async (_method, url) => {
    if (url.endsWith('/provenance')) { entered.resolve(); return gate.promise; }
    return { project: { id: 'p', mode: 'generic' }, facts: [{ id: 'f1' }], intents: [], hints: [] };
  };
  assert.equal(await instance.loadProject('p'), true);
  await entered.promise;
  assert.equal(instance.graphSnapshotReady, true);
  assert.equal(instance.project.project.id, 'p');
  assert.equal(instance.provenance, null);
  gate.resolve({ facts: {} });
  await Promise.resolve();
});

test('late provenance and failures cannot replace a different project source', async () => {
  const { instance } = app();
  provenanceProject(instance, 'A');
  const gate = deferred(), entered = deferred();
  instance.api = async (_method, url) => {
    if (url.includes('/A/')) { entered.resolve(); return gate.promise; }
    throw new Error('unavailable');
  };
  const old = instance.loadProvenance('A');
  await entered.promise;
  instance.selectedProjectId = 'B';
  instance.project = { project: { id: 'B' }, facts: [{ id: 'f1' }], intents: [], hints: [] };
  await instance.loadProvenance('B');
  assert.equal(instance.provenance, null);
  assert.equal(instance.provenanceRows('facts', 'f1')[0].value, '来源读取失败');
  gate.resolve({ facts: { f1: { kind: 'research', producer: { harness: 'old', worker: 'A' } } } });
  await old;
  assert.equal(instance.provenance, null);
  assert.equal(instance.provenanceError, 'unavailable');
});

test('newer same-project provenance wins and offline keeps only that project snapshot', async () => {
  const { instance } = app();
  provenanceProject(instance);
  const gate = deferred(), entered = deferred();
  let calls = 0;
  instance.api = async () => {
    if (++calls === 1) { entered.resolve(); return gate.promise; }
    return { facts: { f1: { kind: 'research', producer: { harness: 'pi', worker: 'new' } } } };
  };
  const old = instance.loadProvenance('p');
  await entered.promise;
  await instance.loadProvenance('p');
  gate.resolve({ facts: { f1: { kind: 'research', producer: { harness: 'codex', worker: 'old' } } } });
  await old;
  assert.match(instance.provenanceRows('facts', 'f1')[0].value, /pi.*new/);
  instance.connectionState = 'offline';
  instance.api = async () => { throw new Error('must not request offline'); };
  await instance.loadProvenance('p');
  assert.match(instance.provenanceRows('facts', 'f1')[0].value, /pi.*new/);
  instance.selectedProjectId = 'another';
  assert.equal(instance.provenanceRows('facts', 'f1').length, 0);
});


test('a stale execution source does not attribute a newly claimed worker to the prior harness', () => {
  const { instance } = app();
  provenanceProject(instance);
  instance.project.intents[0].worker = 'new-worker';
  instance.project.intents[0].to = null;
  assert.equal(instance.provenanceRows('intents', 'i1')[1].value, 'Harness 未记录 · Worker new-worker');
});

test('provenance responses from before a service change cannot replace its last snapshot', async () => {
  const { instance } = app();
  provenanceProject(instance);
  const gate = deferred(), entered = deferred();
  instance.api = async () => { entered.resolve(); return gate.promise; };
  const pending = instance.loadProvenance('p');
  await entered.promise;
  instance.serviceHealth.service_id = 'new-service';
  gate.resolve({ facts: { f1: { kind: 'research', producer: { harness: 'wrong', worker: 'late' } } } });
  await pending;
  assert.match(instance.provenanceRows('facts', 'f1')[0].value, /codex.*runner/);
});
