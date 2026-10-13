function cairnRuntimePanel() {
  return {
    connectionState: 'connecting',
    connectionError: '',
    serviceHealth: null,
    databaseId: null,
    lastSnapshotAt: null,
    runtimeByProject: {},
    runtimeActionBusy: {},
    runtimeActionError: '',
    runtimeLogs: { files: [], selected: null, text: '', note: '' },
    runtimeLogsError: '',
    legacyRemainingSeconds: '',
    batchResults: [],
    pollBusy: false,
    mobilePanelOpen: false,
    projectNavigation: 0,
    projectLoadRequest: 0,
    runtimeRequests: {},
    runtimeLogsRequest: 0,
    isOpeningProject: false,
    graphSnapshotReady: false,

    setConnectionState(state) {
      this.connectionState = state;
      if (state !== 'online') {
        this.graphSnapshotReady = false;
        this.stopRunningAnimations();
      }
    },

    async refreshServiceHealth() {
      try {
        const health = await this.api('GET', '/runtime/health');
        if (this.databaseId && health.database_id !== this.databaseId) {
          this.setConnectionState('changed');
          return false;
        }
        if (health.database_id) this.databaseId = health.database_id;
        if (this.serviceHealth?.service_id !== health.service_id) {
          this.graphSnapshotReady = false;
          this.stopRunningAnimations();
        }
        this.serviceHealth = health;
        this.setConnectionState('online');
        this.connectionError = '';
        if (this.replay.active) this.pulseActive();
        return true;
      } catch (e) {
        this.connectionError = e.message;
        this.setConnectionState('offline');
        return false;
      }
    },

    connectionFailure(error) {
      if (!error?.networkFailure) return;
      this.connectionError = error.message || String(error);
      this.setConnectionState('offline');
    },

    async retryConnection() {
      if (this.pollBusy) return;
      this.pollBusy = true;
      try {
        if (!await this.refreshServiceHealth()) return;
        if (this.view === 'graph' && this.selectedProjectId) {
          await this.loadProject(this.selectedProjectId);
          this.updateGraph();
        } else {
          await this.loadProjects();
        }
      } finally { this.pollBusy = false; }
    },

    async loadRuntime(projectId) {
      const request = (this.runtimeRequests[projectId] || 0) + 1;
      this.runtimeRequests[projectId] = request;
      const database = this.databaseId, service = this.serviceHealth?.service_id;
      const data = await this.api('GET', `/projects/${projectId}/runtime`);
      if (request !== this.runtimeRequests[projectId] || database !== this.databaseId ||
          service !== this.serviceHealth?.service_id) return null;
      if ((this.databaseId && data.database_id !== this.databaseId) ||
          data.service_id !== this.serviceHealth?.service_id) {
        this.setConnectionState(data.database_id !== this.databaseId ? 'changed' : 'connecting');
        throw new Error('服务或数据库身份已变化，请重试连接。');
      }
      this.runtimeByProject[projectId] = data;
      if (projectId === this.selectedProjectId) this.lastSnapshotAt = data.updated_at;
      return data;
    },

    runtimeFor(projectId = this.selectedProjectId) {
      return this.runtimeByProject[projectId] || null;
    },

    runtimeStateLabel(data = this.runtimeFor()) {
      if (!data) return '读取运行状态';
      if (data.project_status === 'completed') return '目标已达成';
      if (data.project_status === 'budget_exhausted') return '预算耗尽';
      return { running: '运行中', preparing: '开题准备', pausing: '正在暂停', paused: '已暂停',
        resuming: '正在恢复', interrupted: '运行中断', blocked: '恢复受阻', finished: '已结束',
        needs_budget: '待确认旧余额', created: '已创建', legacy_budget_migrated: '旧余额已迁移' }[data.state] || data.state;
    },

    runtimeStateClass(data = this.runtimeFor()) {
      if (!data) return 'bg-slate-100 text-slate-500';
      if (['interrupted', 'blocked'].includes(data.state)) return 'bg-rose-50 text-rose-700';
      if (['paused', 'pausing', 'needs_budget'].includes(data.state)) return 'bg-amber-50 text-amber-700';
      if (['running', 'preparing', 'resuming'].includes(data.state)) return 'bg-teal-50 text-teal-700';
      return 'bg-slate-100 text-slate-600';
    },

    runtimePhaseLabel(phase = this.runtimeFor()?.audit_phase) {
      return { preparing: '开题准备', researching: '研究', draining: '在途收尾',
        finalizing: '独立结案', closed: '已结束' }[phase] || phase || '未知';
    },

    runtimeActionLabel(projectId = this.selectedProjectId) {
      const r = this.runtimeFor(projectId);
      if (this.runtimeActionBusy[projectId]) return '提交操作中…';
      if (r?.state === 'pausing') return '正在收敛调用…';
      if (r?.state === 'resuming') return '正在恢复任务…';
      return ['running', 'preparing'].includes(r?.state) ? '暂停研究' : '恢复研究';
    },

    canRuntimeAction(projectId, action) {
      const r = this.runtimeFor(projectId);
      return this.connectionState === 'online' && !this.runtimeActionBusy[projectId] &&
        !this.isOpeningProject &&
        (this.view !== 'graph' || this.graphSnapshotReady) &&
        r?.service_ready && r.service_id === this.serviceHealth?.service_id &&
        r.database_id === this.databaseId && r.available_actions.includes(action);
    },

    async controlRuntime(projectId, action) {
      if (!this.canRuntimeAction(projectId, action)) return;
      const r = this.runtimeFor(projectId);
      const body = { database_id: r.database_id, service_id: r.service_id };
      if (action === 'resume' && r.needs_budget) {
        if (projectId !== this.selectedProjectId) { await this.openProject(projectId); this.sideTab = 'runtime'; return; }
        const value = Number(this.legacyRemainingSeconds);
        if (!String(this.legacyRemainingSeconds).trim() || !Number.isFinite(value) || value < 0) {
          this.sideTab = 'runtime';
          this.runtimeActionError = '请填写旧运行在当前阶段的剩余秒数。';
          return;
        }
        body.remaining_seconds = value;
      }
      this.runtimeActionBusy[projectId] = true;
      this.runtimeActionError = '';
      try {
        const headers = r.generation == null ? {} : { 'X-Cairn-Generation': String(r.generation) };
        const data = await this.api('POST', `/projects/${projectId}/runtime/${action}`, body, headers);
        this.runtimeByProject[projectId] = data;
        const summary = this.projects.find(p => p.id === projectId);
        if (summary) summary.status = data.project_status;
        if (this.selectedProjectId === projectId) await this.loadProject(projectId);
        this.showToast(action === 'pause' ? '已请求暂停，正在收敛调用。' : '已请求恢复原任务。');
        return data;
      } catch (e) {
        this.connectionFailure(e);
        if (projectId === this.selectedProjectId) this.runtimeActionError = e.message;
        throw e;
      } finally { this.runtimeActionBusy[projectId] = false; }
    },

    formatSeconds(value) {
      if (value == null || !Number.isFinite(value)) return '未知';
      const seconds = Math.max(0, Math.ceil(value));
      const h = Math.floor(seconds / 3600);
      const m = Math.floor(seconds % 3600 / 60);
      const s = String(seconds % 60).padStart(2, '0');
      return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    },

    timeBudgetPercent() {
      const r = this.runtimeFor();
      if (r?.elapsed_seconds == null || !r.budget_total_seconds) return 0;
      return Math.min(100, Math.max(0, r.elapsed_seconds / r.budget_total_seconds * 100));
    },

    rolePoolRows() {
      const r = this.runtimeFor();
      const labels = { explore: 'Explore', reason: 'Reason', bootstrap: '开题探索', preflight: '开题预审',
        seed_review: '种子复核', overseer: '旁路监督', closing: '结案判断', final_review: '独立终审', build: '任务构建' };
      return Object.entries(r?.role_pools || {}).map(([role, harnesses]) => ({ role, label: labels[role] || role,
        capacity: Object.values(harnesses).reduce((a, b) => a+b, 0),
        used: r.occupancy_known ? Object.values(r.slots_in_use?.[role] || {}).reduce((a, b) => a+b, 0) : null,
        harnesses: Object.entries(harnesses).map(([harness, capacity]) => ({ harness, capacity,
          used: r.occupancy_known ? r.slots_in_use?.[role]?.[harness] || 0 : null })) }));
    },

    async openRuntimePanel() {
      this.sideTab = 'runtime';
      this.mobilePanelOpen = true;
      await this.loadRuntimeLogs();
    },

    async loadRuntimeLogs(logId = null) {
      if (!this.selectedProjectId || this.connectionState !== 'online') return;
      const id = this.selectedProjectId;
      const navigation = this.projectNavigation, request = ++this.runtimeLogsRequest;
      const current = () => id === this.selectedProjectId && navigation === this.projectNavigation &&
        request === this.runtimeLogsRequest;
      this.runtimeLogsError = '';
      try {
        const suffix = logId ? `?log=${encodeURIComponent(logId)}` : '';
        const data = await this.api('GET', `/projects/${id}/runtime/logs${suffix}`);
        if (current()) this.runtimeLogs = data;
      } catch (e) {
        this.connectionFailure(e);
        if (current()) this.runtimeLogsError = e.message;
      }
    },

    async copyManualStart() {
      const text = this.runtimeFor()?.manual_start || this.serviceHealth?.manual_start ||
        '在终端使用原配置和数据库执行 cairn up，再回到此页面重试连接。';
      try { await navigator.clipboard.writeText(text); this.showToast('已复制人工启动说明。'); }
      catch { this.showToast('复制不可用，请选择页面中的启动说明。', 'error'); }
    },
  };
}
