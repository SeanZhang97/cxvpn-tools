/* Mihomo 连接与核心日志：页面可见时使用后端事件流，离开后立即休眠。 */
(() => {
  const byId = id => document.getElementById(id);
  const state = {
    page: 'overview', logSource: 'proxy', view: 'idle', generation: 0,
    versions: { connections: 0, logs: 0 }, connections: null, logs: null,
    connectionScope: 'active', paused: false,
  };

  const html = value => String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char]);

  function formatBytes(value) {
    let size = Number(value || 0);
    if (!Number.isFinite(size) || size <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    const digits = unit === 0 || size >= 100 ? 0 : size >= 10 ? 1 : 2;
    return `${size.toFixed(digits)} ${units[unit]}`;
  }

  function targetText(item) {
    const meta = item?.metadata || {};
    const address = meta.host || meta.destination_ip || '未知目标';
    return `${address}${meta.destination_port ? `:${meta.destination_port}` : ''}`;
  }

  function filteredConnections(snapshot, scope, query, sort) {
    const needle = String(query || '').trim().toLocaleLowerCase();
    let rows = [...(snapshot?.[scope] || [])];
    if (needle) rows = rows.filter(item => [
      targetText(item), item.metadata?.process, item.metadata?.process_path,
      item.rule, item.rule_payload, ...(item.chains || []),
    ].some(value => String(value || '').toLocaleLowerCase().includes(needle)));
    rows.sort((left, right) => {
      if (sort === 'traffic-desc') return (right.upload + right.download) - (left.upload + left.download);
      if (sort === 'host-asc') return targetText(left).localeCompare(targetText(right), 'zh-CN');
      return String(right.start || '').localeCompare(String(left.start || ''));
    });
    return rows;
  }

  function filteredLogs(snapshot, level, query, order) {
    const needle = String(query || '').trim().toLocaleLowerCase();
    let rows = [...(snapshot?.items || [])].filter(item =>
      (level === 'all' || item.type === level) &&
      (!needle || String(item.payload || '').toLocaleLowerCase().includes(needle)));
    if (order === 'oldest') rows.reverse();
    return rows;
  }

  function phaseLabel(phase, retrySeconds = 0) {
    if (phase === 'connected') return '实时连接';
    if (phase === 'connecting') return '正在连接';
    if (phase === 'reconnecting') return `连接恢复中${retrySeconds ? ` · ${retrySeconds} 秒` : ''}`;
    return '代理核心未运行';
  }

  function renderConnections() {
    const snapshot = state.connections || { active: [], closed: [] };
    byId('connections-active-count').textContent = snapshot.active?.length || 0;
    byId('connections-closed-count').textContent = snapshot.closed?.length || 0;
    byId('connections-upload-total').textContent = formatBytes(snapshot.upload_total);
    byId('connections-download-total').textContent = formatBytes(snapshot.download_total);
    const phase = byId('connections-stream-state');
    phase.textContent = phaseLabel(snapshot.phase, snapshot.retry_seconds);
    phase.className = `activity-stream-state ${snapshot.phase || 'idle'}`;

    const rows = filteredConnections(
      snapshot, state.connectionScope, byId('connections-search').value,
      byId('connections-sort').value);
    const body = byId('connections-body');
    const table = body.closest('table');
    table.classList.toggle('hidden', rows.length === 0);
    byId('connections-empty').textContent = state.connectionScope === 'active'
      ? '暂无活动连接' : '暂无最近关闭记录';
    body.innerHTML = rows.map(item => {
      const meta = item.metadata || {};
      const process = meta.process || meta.process_path || meta.network || '未知进程';
      const started = item.start ? String(item.start).replace('T', ' ').replace(/\..*$/, '') : '--';
      const close = state.connectionScope === 'active'
        ? `<button class="btn mini ghost" type="button" data-close-connection="${html(item.id)}">关闭</button>` : '';
      return `<tr><td class="connection-target"><strong title="${html(targetText(item))}">${html(targetText(item))}</strong><small title="${html(process)}">${html(process)}</small></td><td title="${html(item.rule_payload || '')}">${html(item.rule || '--')}</td><td class="connection-chain" title="${html((item.chains || []).join(' → '))}">${html((item.chains || []).join(' → ') || 'DIRECT')}</td><td>${formatBytes(item.upload)}</td><td>${formatBytes(item.download)}</td><td>${html(started)}</td><td>${close}</td></tr>`;
    }).join('');
  }

  function renderLogs() {
    if (state.paused) return;
    const snapshot = state.logs || { items: [] };
    const list = byId('proxy-log-list');
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 50;
    const rows = filteredLogs(snapshot, byId('proxy-log-level').value,
      byId('proxy-log-search').value, byId('proxy-log-order').value);
    byId('proxy-log-count').textContent = `${rows.length} 条记录`;
    const badge = byId('proxy-log-stream-state');
    badge.textContent = phaseLabel(snapshot.phase, snapshot.retry_seconds);
    badge.className = `live-indicator ${snapshot.phase || 'idle'}`;
    list.innerHTML = rows.map(item => `<div class="proxy-log-row"><time>${html(item.time)}</time><span class="proxy-log-level ${html(item.type)}">${html(item.type)}</span><span class="proxy-log-payload">${html(item.payload)}</span></div>`).join('');
    if (nearBottom && byId('proxy-log-order').value === 'newest') list.scrollTop = list.scrollHeight;
  }

  function desiredView() {
    if (document.visibilityState === 'hidden') return 'idle';
    if (state.page === 'connections') return 'connections';
    if (state.page === 'logs' && state.logSource === 'proxy') return 'logs';
    return 'idle';
  }

  async function activate() {
    const view = desiredView();
    if (view === state.view) return;
    state.view = view;
    const generation = ++state.generation;
    try { await api().set_routing_activity_view(view); } catch (_) { return; }
    if (view === 'idle') return;
    try {
      const initial = await api().get_routing_activity_snapshot(view);
      if (generation !== state.generation || state.view !== view) return;
      state.versions[view] = Number(initial?.version || 0);
      state[view] = initial?.snapshot || state[view];
      if (view === 'connections') renderConnections(); else renderLogs();
    } catch (_) { return; }
    while (generation === state.generation && state.view === view) {
      try {
        const result = await api().wait_routing_activity(state.versions[view], view, 25);
        if (generation !== state.generation || state.view !== view) return;
        state.versions[view] = Number(result?.version || state.versions[view]);
        if (result?.snapshot) {
          state[view] = result.snapshot;
          if (view === 'connections') renderConnections(); else renderLogs();
        }
      } catch (_) {
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
  }

  function switchLogSource(source) {
    state.logSource = source === 'runtime' ? 'runtime' : 'proxy';
    document.querySelectorAll('[data-log-source]').forEach(button => {
      const selected = button.dataset.logSource === state.logSource;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', String(selected));
    });
    byId('proxy-log-panel').classList.toggle('hidden', state.logSource !== 'proxy');
    byId('runtime-log-panel').classList.toggle('hidden', state.logSource !== 'runtime');
    if (state.logSource === 'runtime') void updateLogs();
    void activate();
  }

  async function closeConnection(connectionId = '') {
    const all = !connectionId;
    const confirmed = await confirmAction({
      title: all ? '关闭全部活动连接' : '关闭此连接',
      message: all ? '现有连接会被中断，应用可按当前代理规则重新建立连接。' : '此连接会立即中断，应用可能自动重连。',
      confirmText: all ? '关闭全部' : '关闭连接',
    });
    if (!confirmed) return;
    const result = await api().close_routing_connection(connectionId);
    toast(result?.ok === false ? result : { ok: true, msg: all ? '已关闭全部活动连接' : '连接已关闭' });
  }

  function bind() {
    ['connections-sort', 'proxy-log-level', 'proxy-log-order'].forEach(id => {
      window.RoutingWorkspace?.enhanceSelect?.(byId(id), { compact: true });
    });
    document.querySelectorAll('[data-connection-scope]').forEach(button => {
      button.onclick = () => {
        state.connectionScope = button.dataset.connectionScope;
        document.querySelectorAll('[data-connection-scope]').forEach(item => {
          const selected = item === button;
          item.classList.toggle('active', selected);
          item.setAttribute('aria-selected', String(selected));
        });
        renderConnections();
      };
    });
    byId('connections-search').oninput = renderConnections;
    byId('connections-sort').onchange = renderConnections;
    byId('connections-body').onclick = event => {
      const button = event.target.closest('[data-close-connection]');
      if (button) void closeConnection(button.dataset.closeConnection);
    };
    byId('btn-connections-close-all').onclick = () => void closeConnection('');
    byId('btn-connections-clear').onclick = async () => {
      await api().clear_routing_activity('connections');
    };
    document.querySelectorAll('[data-log-source]').forEach(button => {
      button.onclick = () => switchLogSource(button.dataset.logSource);
    });
    byId('proxy-log-level').onchange = renderLogs;
    byId('proxy-log-search').oninput = renderLogs;
    byId('proxy-log-order').onchange = renderLogs;
    byId('btn-proxy-log-pause').onclick = () => {
      state.paused = !state.paused;
      byId('btn-proxy-log-pause').textContent = state.paused ? '继续' : '暂停';
      if (!state.paused) renderLogs();
    };
    byId('btn-proxy-log-clear').onclick = async () => {
      await api().clear_routing_activity('logs');
    };
    window.addEventListener('cxvpn:pagechange', event => {
      state.page = event.detail?.page || 'overview';
      void activate();
    });
    document.addEventListener('visibilitychange', () => void activate());
  }

  window.RoutingActivityTools = { formatBytes, filteredConnections, filteredLogs };
  window.RoutingActivityWorkspace = {
    runtimeLogsVisible: () => state.page === 'logs' && state.logSource === 'runtime',
  };
  window.addEventListener('pywebviewready', () => { bind(); void activate(); });
})();
