(() => {
  const byId = id => document.getElementById(id);
  const backend = () => window.pywebview?.api;
  // 未开启时首页暂存的流量模式/接管方式；空串表示跟随已保存配置。
  let selectedMode = '';
  let selectedCapture = '';
  let selectedGroupId = 'all';
  let returnTarget = { page: 'proxy', tab: 'home' };
  let busy = false;
  let busyLabel = '';
  let operationNotice = null;
  let telemetryActive = null;
  let latestObservability = null;
  let telemetryState = { phase: 'idle' };
  let telemetryData = { up: 0, down: 0, upTotal: 0, downTotal: 0 };
  const RATE_WINDOW_MS = 60 * 1000;
  const RATE_CHART_HEIGHT = 172;
  let rateHistory = [];
  let rateChart = null;
  let outboundOpen = false;
  let outboundKbIndex = -1;
  let outboundApplying = false;

  const OUTBOUND_ICONS = {
    physical: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.3 3.9 5.2 3.9 8.5s-1.3 6.2-3.9 8.5c-2.6-2.3-3.9-5.2-3.9-8.5S9.4 5.8 12 3.5z"/></svg>',
    proxy: '<svg viewBox="0 0 24 24"><path d="m12 3 8 4.5-8 4.5-8-4.5L12 3z"/><path d="m4 12.5 8 4.5 8-4.5"/><path d="m4 16.5 8 4.5 8-4.5" opacity=".55"/></svg>',
    provider: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="5" rx="1.5"/><rect x="4" y="14" width="16" height="5" rx="1.5"/><path d="M8 7.5h.01M8 16.5h.01"/></svg>',
    vpn: '<svg viewBox="0 0 24 24"><path d="M12 3 5 6v5c0 4.4 3 8.2 7 10 4-1.8 7-5.6 7-10V6l-7-3z"/><path d="M9.5 12l2 2 3.5-4"/></svg>',
    block: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M6 6l12 12"/></svg>',
  };

  function clone(value) {
    return JSON.parse(JSON.stringify(value || {}));
  }

  // pywebview 桥调用在桥消息丢失时没有兜底超时，UI 必须能自行恢复。
  function withTimeout(promise, ms, message) {
    let timer = null;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  function routingState() {
    return window.RoutingWorkspace?.state?.() || {};
  }

  function enabledProviders(config) {
    return (config?.proxy_providers || []).filter(item => item.enabled);
  }

  function usesProxy(config) {
    const values = [config?.default_outbound, ...(config?.traffic_mode === 'global' ? [] :
      [...(config?.rules || []), ...(config?.business_rules || [])])
      .filter(item => item.enabled !== false).map(item => item.outbound)];
    return values.some(value => value === 'proxy' || /^(proxy:|outbound:)/.test(String(value || '')));
  }

  function usesSubscriptionSelection(config) {
    const values = [config?.default_outbound, ...(config?.traffic_mode === 'global' ? [] : config?.rules || [])
      .filter(item => item.enabled !== false).map(item => item.outbound)];
    return values.some(value => value === 'proxy' || String(value || '').startsWith('proxy:'));
  }

  function outboundName(value, config = null) {
    const raw = String(value || 'physical');
    if (raw === 'physical') return '物理网络';
    if (raw === 'proxy') return '聚合代理池';
    if (raw === 'block') return '阻止访问';
    if (raw.startsWith('proxy:')) {
      const id = raw.slice(6);
      const provider = (config?.proxy_providers || []).find(item => item.id === id);
      return `代理订阅 ${provider?.name || id}`;
    }
    if (raw.startsWith('vpn:')) return `VPN ${raw.slice(4)}`;
    return raw;
  }

  function targetProvider(config) {
    const providers = enabledProviders(config);
    const outbound = String(config?.default_outbound || '');
    const target = config?.aggregate_selection;
    if (outbound === 'proxy' && target?.mode === 'manual') {
      const owner = providers.find(item => item.id === target.provider_id);
      return owner ? { ...owner, selection_mode: 'manual', selected_node: target.node_name } : null;
    }
    const requested = outbound.startsWith('proxy:') ? outbound.slice(6) : '';
    return providers.find(item => item.id === requested)
      || providers.find(item => item.selection_mode === 'manual' && item.selected_node)
      || providers[0]
      || null;
  }

  function preferredGroup(groups, provider) {
    return groups.find(item => item.id === provider?.id)
      || groups.find(item => item.id === 'all')
      || null;
  }

  function currentNode(group) {
    if (!group) return null;
    return (group.nodes || []).find(item => item.name === group.selected) || null;
  }

  function providerNodes(setup, providerId) {
    const records = setup?.provider_nodes;
    const raw = Array.isArray(records)
      ? records.find(item => item?.id === providerId || item?.provider_id === providerId)
      : records?.[providerId];
    return Array.isArray(raw) ? raw : Array.isArray(raw?.nodes) ? raw.nodes : [];
  }

  function preferenceNodeName(node, provider, allowNameFallback = false) {
    const displayName = String(node?.display_name || '').trim();
    if (displayName) return displayName;
    const name = String(node?.name || '').trim();
    if (allowNameFallback) return name;
    const prefix = provider?.name ? `[${provider.name}] ` : '';
    return prefix && name.startsWith(prefix) ? name.slice(prefix.length) : '';
  }

  function nodeMatchesPreference(node, provider, selectedNode, allowNameFallback = false) {
    const expected = String(selectedNode || '').trim();
    if (!expected || !node) return false;
    return preferenceNodeName(node, provider, allowNameFallback) === expected;
  }

  function testAgeLabel(value) {
    const raw = Number(value || 0);
    if (!Number.isFinite(raw) || raw <= 0) return '历史测速';
    const testedAt = raw < 1e12 ? raw * 1000 : raw;
    const age = Math.max(0, Date.now() - testedAt);
    const minute = 60 * 1000;
    if (age < minute) return '刚刚测速';
    if (age < 60 * minute) return `${Math.floor(age / minute)} 分钟前测速`;
    if (age < 24 * 60 * minute) return `${Math.floor(age / (60 * minute))} 小时前测速`;
    if (age < 7 * 24 * 60 * minute) return `${Math.floor(age / (24 * 60 * minute))} 天前测速`;
    return `${new Date(testedAt).toLocaleDateString('zh-CN')} 测速`;
  }

  function targetNodeTestSummary(node) {
    if (!node) return '需要重新选择';
    const age = testAgeLabel(node.tested_at);
    if (node.tested && node.alive === false) return `${age} · 不可用`;
    if (Number(node.delay || 0) > 0) return `${node.delay} ms · ${age} · 待启用`;
    if (node.tested) return `${age} · 未测得延迟`;
    return '尚未测速 · 待启用';
  }

  function runtimeUsesPreference(group, provider) {
    const selectedNode = String(provider?.selected_node || '').trim();
    if (!group || !selectedNode) return false;
    const runtimeNode = (group.nodes || []).find(item => item.name === group.selected);
    if (nodeMatchesPreference(runtimeNode, provider, selectedNode)) return true;
    return group.selected === selectedNode || group.selected === `[${provider.name}] ${selectedNode}`;
  }

  function selectionGuard(setup, provider, group = null) {
    if (!provider) return { valid: false, reason: '请先添加并启用一个代理订阅。', action: '管理订阅' };
    if (provider.selection_mode !== 'manual') return { valid: true, reason: '' };
    if (!String(provider.selected_node || '').trim()) {
      return { valid: false, reason: `“${provider.name}”使用手动模式，请先选择目标节点。`, action: '去选择节点' };
    }
    const persisted = providerNodes(setup, provider.id);
    const runtime = group?.nodes || [];
    const exists = persisted.some(item => nodeMatchesPreference(item, provider, provider.selected_node, true))
      || runtime.some(item => nodeMatchesPreference(item, provider, provider.selected_node));
    if (!exists) {
      return { valid: false, reason: `已选节点“${provider.selected_node}”不在最新节点清单中，请重新选择。`, action: '重新选择' };
    }
    return { valid: true, reason: '' };
  }

  function cacheSummary(setup, providers) {
    const caches = setup?.provider_caches || {};
    const available = providers.map(item => caches[item.id]).filter(item => item?.available);
    const total = available.reduce((sum, item) => sum + Number(item.node_count || 0), 0);
    return { available: available.length, total };
  }

  function setText(id, value) {
    const element = byId(id);
    if (element) element.textContent = value;
  }

  function resetRateHistory() {
    rateHistory = [];
    hideRateTooltip();
  }

  function formatRateValue(value) {
    const formatter = window.RoutingTelemetryTools?.formatRate;
    if (formatter) return formatter(value);
    const amount = Math.max(0, Number(value) || 0);
    if (amount >= 1024 * 1024) return `${(amount / (1024 * 1024)).toFixed(1)} MB/s`;
    if (amount >= 1024) return `${(amount / 1024).toFixed(1)} KB/s`;
    return `${Math.round(amount)} B/s`;
  }

  function formatClock(seconds) {
    return new Date(seconds * 1000).toLocaleTimeString('zh-CN', { hour12: false });
  }

  function hideRateTooltip() {
    const tooltip = byId('proxy-rate-tooltip');
    if (!tooltip) return;
    tooltip.classList.remove('show');
    tooltip.setAttribute('aria-hidden', 'true');
  }

  function showRateTooltip(chart, idx) {
    const tooltip = byId('proxy-rate-tooltip');
    const wrap = byId('proxy-rate-chart-wrap');
    const sample = rateHistory[idx];
    if (!tooltip || !wrap || !chart || !sample) return hideRateTooltip();
    setText('proxy-rate-tooltip-time', formatClock(sample.time / 1000));
    setText('proxy-rate-tooltip-node', sample.node || byId('proxy-node-name')?.textContent?.trim() || '当前节点');
    setText('proxy-rate-tooltip-up-value', `上行 ${formatRateValue(sample.up)}`);
    setText('proxy-rate-tooltip-down-value', `下行 ${formatRateValue(sample.down)}`);
    const over = chart.over.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    const overLeft = over.left - wrapRect.left;
    const overTop = over.top - wrapRect.top;
    const tooltipWidth = tooltip.offsetWidth || 170;
    const tooltipHeight = tooltip.offsetHeight || 92;
    let left = overLeft + chart.cursor.left + 14;
    if (left + tooltipWidth > wrapRect.width - 6) left = overLeft + chart.cursor.left - tooltipWidth - 14;
    let top = overTop + chart.cursor.top - tooltipHeight - 12;
    if (top < 4) top = overTop + chart.cursor.top + 18;
    tooltip.style.left = `${Math.max(4, left)}px`;
    tooltip.style.top = `${Math.max(4, top)}px`;
    tooltip.classList.add('show');
    tooltip.setAttribute('aria-hidden', 'false');
  }

  function rateGradient(colorTop) {
    return (u) => {
      const gradient = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
      gradient.addColorStop(0, colorTop);
      gradient.addColorStop(1, 'rgba(6,10,24,0)');
      return gradient;
    };
  }

  function ensureRateChart() {
    if (rateChart) return rateChart;
    const container = byId('proxy-rate-chart');
    if (!container || typeof window.uPlot !== 'function') return null;
    const axisStyle = {
      stroke: '#7787a5',
      font: '10px "Segoe UI Variable", "Segoe UI", sans-serif',
      grid: { stroke: 'rgba(148,163,184,.09)', width: 1 },
      ticks: { stroke: 'rgba(148,163,184,.18)', width: 1, size: 4 },
    };
    rateChart = new window.uPlot({
      width: Math.max(280, container.clientWidth || 560),
      height: RATE_CHART_HEIGHT,
      padding: [10, 6, 0, 0],
      scales: { x: { time: true }, y: { range: (u, min, max) => [0, (max || 1) * 1.18] } },
      axes: [
        { ...axisStyle, values: (u, splits) => splits.map(value => formatClock(value)) },
        { ...axisStyle, size: 52, values: (u, splits) => splits.map(value => formatRateValue(value)) },
      ],
      series: [
        {},
        {
          label: '上行', stroke: '#fbbf72', width: 1.8,
          paths: window.uPlot.paths.spline(), points: { show: false },
          fill: rateGradient('rgba(251,191,114,.22)'),
        },
        {
          label: '下行', stroke: '#22d3ee', width: 1.8,
          paths: window.uPlot.paths.spline(), points: { show: false },
          fill: rateGradient('rgba(34,211,238,.2)'),
        },
      ],
      cursor: {
        drag: { x: false, y: false },
        focus: { prox: 20 },
        points: {
          size: 9,
          fill: (u, seriesIdx) => seriesIdx === 1 ? '#fbbf72' : '#22d3ee',
          stroke: (u, seriesIdx) => seriesIdx === 1 ? '#fbbf72' : '#22d3ee',
        },
      },
      legend: { show: false },
      hooks: {
        setCursor: [(u) => {
          const { idx, left } = u.cursor;
          if (idx == null || left == null || left < 0) return hideRateTooltip();
          showRateTooltip(u, idx);
        }],
      },
    }, [[], [], []], container);
    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(entries => {
        const width = Math.floor(entries[0]?.contentRect?.width || 0);
        if (width > 120) rateChart?.setSize({ width, height: RATE_CHART_HEIGHT });
      }).observe(container);
    }
    return rateChart;
  }

  function inspectLatestRateSample() {
    const chart = ensureRateChart();
    if (!chart || !rateHistory.length) return;
    const last = rateHistory[rateHistory.length - 1];
    chart.setCursor({
      left: chart.valToPos(last.time / 1000, 'x'),
      top: chart.valToPos(last.down || 0, 'y'),
    });
  }

  function bindRateChart() {
    const container = byId('proxy-rate-chart');
    if (!container || container.dataset.bound === 'true') return;
    container.dataset.bound = 'true';
    container.addEventListener('focus', inspectLatestRateSample);
    container.addEventListener('keydown', event => {
      if (event.key === 'Escape') hideRateTooltip();
    });
    container.addEventListener('blur', hideRateTooltip);
    window.addEventListener('blur', hideRateTooltip);
  }

  function renderRateChart() {
    const chart = ensureRateChart();
    const empty = byId('proxy-rate-empty');
    const has = rateHistory.length > 0;
    if (empty) {
      empty.classList.toggle('hidden', has);
      if (!has) {
        const state = routingState();
        const config = state.appliedConfig || state.setup?.config;
        const phase = servicePhase(config, state.runtimeStatus || state.setup?.status || {});
        const messages = {
          connecting: '正在连接实时流量通道…',
          reconnecting: '实时流量通道重连中…',
        };
        empty.textContent = phase === 'running'
          ? messages[telemetryState.phase] || '正在等待实时速率数据…'
          : phase === 'unknown' ? '服务状态未知 · 暂无实时速率'
          : phase === 'degraded' ? '代理尚未就绪 · 暂无实时速率'
          : '代理未开启 · 启动后显示实时速率';
      }
    }
    if (!chart) return;
    chart.setData([
      rateHistory.map(item => item.time / 1000),
      rateHistory.map(item => item.up),
      rateHistory.map(item => item.down),
    ]);
  }

  function renderOperationNotice() {
    const notice = byId('proxy-operation-notice');
    if (!notice) return;
    notice.classList.toggle('hidden', !operationNotice);
    if (!operationNotice) return;
    setText('proxy-operation-title', operationNotice.title);
    setText('proxy-operation-detail', operationNotice.detail);
  }

  function setOperationNotice(title = '', detail = '') {
    operationNotice = title ? { title, detail } : null;
    renderOperationNotice();
  }

  function proxyHomeVisible() {
    return document.visibilityState !== 'hidden'
      && byId('page-proxy')?.classList.contains('active')
      && document.querySelector('[data-proxy-view="home"]')?.classList.contains('active');
  }

  function renderObservability(value = routingState().setup?.observability || {}) {
    setText('proxy-active-connections', String(value?.active || 0));
    const topHit = value?.rule_hits?.[0];
    setText('proxy-rule-hits', topHit ? `${topHit.source}/${topHit.rule} · ${topHit.connections}` : '暂无');
  }

  function renderTelemetry() {
    const tools = window.RoutingTelemetryTools;
    const upText = tools?.formatRate?.(telemetryData.up) || '0 B/s';
    const downText = tools?.formatRate?.(telemetryData.down) || '0 B/s';
    setText('proxy-upload-rate', upText);
    setText('proxy-download-rate', downText);
    setText('proxy-live-up', upText);
    setText('proxy-live-down', downText);
    byId('proxy-rate-chart')?.setAttribute('aria-label',
      `实时上行 ${upText}，下行 ${downText}，过去 60 秒趋势图，聚焦后按 Escape 关闭详情`);
    renderRateChart();
    const state = byId('proxy-stream-state');
    if (!state) return;
    const snapshot = routingState();
    const running = servicePhase(snapshot.appliedConfig || snapshot.setup?.config,
      snapshot.runtimeStatus || snapshot.setup?.status || {}) === 'running';
    const labels = {
      connected: '实时流量', connecting: '正在连接', reconnecting: '重连中',
      idle: running ? '等待流量数据' : '等待服务',
    };
    state.textContent = labels[telemetryState.phase] || '等待服务';
    state.classList.toggle('connected', telemetryState.phase === 'connected');
    state.classList.toggle('reconnecting', telemetryState.phase === 'reconnecting');
    state.title = telemetryState.phase === 'reconnecting' && telemetryState.retrySeconds
      ? `${telemetryState.retrySeconds} 秒后重试` : '';
  }

  function updateTelemetry(data) {
    telemetryData = { ...telemetryData, ...(data || {}) };
    const now = Date.now();
    const cutoff = now - RATE_WINDOW_MS;
    const node = byId('proxy-node-name')?.textContent?.trim() || '当前节点';
    rateHistory.push({
      time: now,
      up: Math.max(0, Number(telemetryData.up) || 0),
      down: Math.max(0, Number(telemetryData.down) || 0),
      node,
    });
    rateHistory = rateHistory.filter(item => item.time >= cutoff);
    renderTelemetry();
  }

  function updateTelemetryState(state) {
    telemetryState = state || { phase: 'idle' };
    if (telemetryState.phase !== 'connected') {
      telemetryData = { up: 0, down: 0, upTotal: 0, downTotal: 0 };
      resetRateHistory();
    }
    renderTelemetry();
  }

  function applyTelemetrySnapshot(value) {
    const normalized = window.RoutingTelemetryTools?.normalizeSnapshot?.(value);
    if (!normalized) return;
    updateTelemetry(normalized);
    updateTelemetryState(normalized);
    latestObservability = normalized.observability || null;
    renderObservability(latestObservability || {});
  }

  function reconcileTelemetry(running) {
    const active = !!running && proxyHomeVisible();
    if (!active) hideRateTooltip();
    if (active === telemetryActive) return;
    telemetryActive = active;
    backend()?.set_routing_telemetry_active?.(active)?.catch?.(() => {
      telemetryActive = null;
    });
  }

  function renderSubscriptionHealth(setup, provider) {
    const container = byId('proxy-subscription-health');
    const metadata = window.RoutingNodeTools?.subscriptionMetadata?.(
      provider ? providerNodes(setup, provider.id) : []);
    if (!container || !metadata) return;
    container.replaceChildren();
    container.classList.toggle('hidden', !metadata.items.length);
    container.classList.toggle('warning', metadata.tone === 'warning');
    container.classList.toggle('danger', metadata.tone === 'danger');
    metadata.items.forEach(item => {
      const cell = document.createElement('span');
      const label = document.createElement('small');
      const value = document.createElement('strong');
      label.textContent = item.label;
      value.textContent = item.value;
      cell.append(label, value);
      container.append(cell);
    });
    const spoken = metadata.items.map(item => `${item.label}${item.value}`).join('，');
    container.setAttribute('aria-label', `${provider?.name || '当前订阅'}：${spoken}`);
  }

  const QUALITY_SEGMENTS = [
    { key: 'fast', label: '极速 <100ms', bar: 'q-fast', legend: 'lg-fast' },
    { key: 'good', label: '良好 100-250ms', bar: 'q-good', legend: 'lg-good' },
    { key: 'mid', label: '一般 250-500ms', bar: 'q-mid', legend: 'lg-mid' },
    { key: 'bad', label: '较差 / 不可用', bar: 'q-bad', legend: 'lg-bad' },
    { key: 'untested', label: '未测速', bar: 'q-untested', legend: 'lg-untested' },
  ];

  function dashboardNodePool(setup, groups, providers, groupId) {
    const isMetadata = window.RoutingWorkspace?.isMetadataNode || (() => false);
    const runtime = (groups.find(item => item.id === groupId)?.nodes || [])
      .filter(node => !isMetadata(node));
    if (runtime.length) return runtime;
    const ids = groupId === 'all' ? providers.map(item => item.id) : [groupId];
    return ids.flatMap(id => providerNodes(setup, id)).filter(node => !isMetadata(node));
  }

  function qualityBucketOf(node) {
    const delay = Number(node.delay) || 0;
    const tested = node.tested === true || node.alive === true || node.alive === false || delay > 0;
    if (!tested) return 'untested';
    if (node.alive === false || delay >= 500) return 'bad';
    if (delay <= 0) return 'mid';
    if (delay < 100) return 'fast';
    if (delay < 250) return 'good';
    return 'mid';
  }

  function renderNodeQuality(nodes) {
    const section = byId('node-quality');
    if (!section) return;
    const total = nodes.length;
    section.classList.toggle('hidden', !total);
    if (!total) return;
    const counts = { fast: 0, good: 0, mid: 0, bad: 0, untested: 0 };
    nodes.forEach(node => { counts[qualityBucketOf(node)] += 1; });
    const bar = byId('node-quality-bar');
    const legend = byId('node-quality-legend');
    bar?.replaceChildren();
    legend?.replaceChildren();
    QUALITY_SEGMENTS.forEach(segment => {
      const count = counts[segment.key];
      if (bar) {
        const part = document.createElement('i');
        part.className = segment.bar;
        part.style.flexGrow = String(count / total);
        part.title = `${segment.label}：${count} 个`;
        bar.append(part);
      }
      if (legend) {
        const item = document.createElement('span');
        const dot = document.createElement('i');
        dot.className = segment.legend;
        const value = document.createElement('strong');
        value.textContent = String(count);
        item.append(dot, document.createTextNode(`${segment.label} `), value);
        legend.append(item);
      }
    });
    const usable = counts.fast + counts.good + counts.mid;
    setText('node-quality-summary', `${usable} / ${total} 可用`);
    bar?.setAttribute('aria-label', `节点延迟质量分布：${usable}/${total} 可用`);
  }

  function renderTopNodes(nodes, group, provider) {
    const section = byId('top-nodes');
    const list = byId('top-nodes-list');
    if (!section || !list) return;
    const ranked = nodes
      .filter(node => node.alive !== false && Number(node.delay) > 0)
      .sort((a, b) => (Number(a.delay) || 0) - (Number(b.delay) || 0))
      .slice(0, 3);
    section.classList.toggle('hidden', !ranked.length);
    list.replaceChildren();
    if (!ranked.length) return;
    ranked.forEach((node, index) => {
      const isCurrent = (group && node.name === group.selected)
        || nodeMatchesPreference(node, provider, provider?.selected_node, true);
      const row = document.createElement('div');
      row.className = 'top-node-row';
      row.setAttribute('role', 'button');
      row.tabIndex = 0;
      const rank = document.createElement('span');
      rank.className = 'top-node-rank';
      rank.textContent = String(index + 1);
      const name = document.createElement('span');
      name.className = 'top-node-name';
      name.textContent = node.display_name || node.name || '未命名节点';
      name.title = name.textContent;
      const delay = document.createElement('span');
      delay.className = 'top-node-delay';
      delay.textContent = `${node.delay} ms`;
      row.append(rank, name);
      if (isCurrent) {
        const badge = document.createElement('span');
        badge.className = 'top-node-current';
        badge.textContent = '使用中';
        row.append(badge);
      }
      row.append(delay);
      const open = () => openNodes(selectedGroupId);
      row.onclick = open;
      row.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          open();
        }
      };
      list.append(row);
    });
  }

  const MODE_LABELS = {
    capture: { 'system-proxy': '系统代理', tun: 'TUN 模式' },
    captureShort: { 'system-proxy': '系统代理', tun: 'TUN' },
    traffic: { rule: '规则模式', global: '全局模式' },
  };

  function appliedCaptureMode(config, status, running) {
    const saved = config?.capture_mode === 'tun' ? 'tun' : 'system-proxy';
    if (!running) return saved;
    return (status?.capture_mode || saved) === 'tun' ? 'tun' : 'system-proxy';
  }

  function shownModes(config, status, running) {
    const capture = appliedCaptureMode(config, status, running);
    const traffic = config?.traffic_mode === 'global' ? 'global' : 'rule';
    return {
      capture,
      traffic,
      shownCapture: running ? capture : (selectedCapture || capture),
      shownTraffic: running ? traffic : (selectedMode || traffic),
    };
  }

  function renderModeConsole(config, status, running) {
    const { capture, traffic, shownCapture, shownTraffic } = shownModes(config, status, running);
    const pendingCapture = !running && shownCapture !== capture;
    const pendingTraffic = !running && shownTraffic !== traffic;
    const anyPending = pendingCapture || pendingTraffic;
    const conflicts = routingState().setup?.tun_conflicts || [];
    document.querySelectorAll('[data-proxy-seg]').forEach(group => {
      const seg = group.dataset.proxySeg;
      const shown = seg === 'capture' ? shownCapture : shownTraffic;
      const applied = seg === 'capture' ? capture : traffic;
      group.querySelectorAll('.mode-tile').forEach(tile => {
        const value = tile.dataset.value;
        tile.classList.toggle('selected', value === shown);
        tile.classList.toggle('pending', !running && value === shown && shown !== applied);
        tile.setAttribute('aria-checked', String(value === shown));
        tile.tabIndex = value === shown ? 0 : -1;
        if (seg === 'capture' && value === 'tun') {
          tile.classList.toggle('disabled', !!conflicts.length && shown !== 'tun');
        }
      });
    });
    const notice = byId('proxy-tun-notice');
    if (notice) {
      if (conflicts.length) {
        const names = [...new Set(conflicts.map(item => item.name).filter(Boolean))].join('、');
        notice.classList.remove('hidden');
        notice.classList.add('danger');
        setText('proxy-tun-notice-text', `检测到其他 TUN（${names || '未知'}）正在承载默认路由，TUN 模式暂不可用；请先关闭对应客户端的 TUN 模式。`);
      } else if (pendingCapture && shownCapture === 'tun') {
        // 预告只覆盖决策窗口：未运行且 TUN 待生效；已运行由磁贴选中态与运行徽章表达，常驻会稀释红色冲突提示。
        notice.classList.remove('hidden');
        notice.classList.remove('danger');
        setText('proxy-tun-notice-text', 'TUN 将接管全部系统流量并自动关闭 Windows 系统代理；开启后生效，切换期间网络会短暂中断，失败自动恢复。');
      } else {
        notice.classList.add('hidden');
      }
    }
    const badge = byId('proxy-mode-badge');
    if (badge) {
      badge.className = `mode-badge${running ? ' running' : anyPending ? ' pending' : ''}`;
      badge.textContent = running ? '运行中' : anyPending ? '有待生效变更' : '未启用';
    }
    const stripDot = byId('proxy-strip-dot');
    if (stripDot) stripDot.className = `strip-dot${running ? ' on' : anyPending ? ' pending' : ''}`;
    setText('proxy-strip-text', running
      ? `当前运行：${MODE_LABELS.captureShort[capture]} · ${MODE_LABELS.traffic[traffic]}`
      : anyPending
        ? `已暂存：${MODE_LABELS.captureShort[shownCapture]} · ${MODE_LABELS.traffic[shownTraffic]}，开启代理后生效`
        : `已保存：${MODE_LABELS.captureShort[capture]} · ${MODE_LABELS.traffic[traffic]}`);
    const chipCapture = byId('hero-chip-capture');
    if (chipCapture) {
      chipCapture.textContent = MODE_LABELS.capture[shownCapture];
      chipCapture.classList.toggle('pending', pendingCapture);
    }
    const chipTraffic = byId('hero-chip-traffic');
    if (chipTraffic) {
      chipTraffic.textContent = MODE_LABELS.traffic[shownTraffic];
      chipTraffic.classList.toggle('pending', pendingTraffic);
    }
    setText('hero-chip-outbound', `出口 ${outboundName(config?.default_outbound, config)}`);
    setText('proxy-current-mode', `${MODE_LABELS.captureShort[shownCapture]} · ${MODE_LABELS.traffic[shownTraffic]}`);
  }

  function setView(name) {
    document.querySelectorAll('[data-proxy-view]').forEach(view => {
      view.classList.toggle('active', view.dataset.proxyView === name);
    });
    byId('main-content')?.scrollTo({ top: 0, behavior: 'smooth' });
    const state = routingState();
    if (state.loaded) reconcileTelemetry(!!state.runtimeStatus?.running);
  }

  function providerModeLabel(provider) {
    return provider?.selection_mode === 'manual' ? '手动节点' : '自动优选';
  }

  function outboundChoices(config, setup) {
    const choices = [
      { value: 'physical', kind: 'physical', name: '物理网络', desc: '任何代理与 VPN 都不走' },
    ];
    const providers = enabledProviders(config);
    if (providers.length) {
      choices.push({ value: 'proxy', kind: 'proxy', name: '全部代理订阅',
        desc: `${providers.length} 个已启用订阅 · 使用全部订阅策略` });
      providers.forEach(provider => choices.push({
        value: `proxy:${provider.id}`, kind: 'provider', name: `代理 · ${provider.name}`,
        desc: providerModeLabel(provider)
          + (provider.selection_mode === 'manual' && provider.selected_node
            ? ` · ${provider.selected_node}` : ''),
      }));
    }
    (setup?.vpns || []).forEach(vpn => choices.push({
      value: `vpn:${vpn.name}`, kind: 'vpn', name: `VPN · ${vpn.name}`,
      desc: `Windows VPN · ${vpn.status === 'Connected' ? '已连接' : '未连接'}`,
    }));
    choices.push({ value: 'block', kind: 'block', name: '阻止连接', desc: '直接拒绝，不回退到其他出口' });
    return choices;
  }

  function outboundChoiceOf(config, setup) {
    const value = String(config?.default_outbound || 'physical');
    const found = outboundChoices(config, setup).find(item => item.value === value);
    if (found) return found;
    const kind = value.startsWith('vpn:') ? 'vpn'
      : value.startsWith('proxy:') ? 'provider'
      : value === 'block' ? 'block' : value === 'proxy' ? 'proxy' : 'physical';
    return { value, kind, name: outboundName(value, config), desc: '当前出口不可用，订阅已停用或 VPN 不存在' };
  }

  function renderOutboundOptions(config, setup) {
    const box = byId('outbound-options');
    if (!box) return;
    const current = String(config?.default_outbound || 'physical');
    box.replaceChildren();
    outboundChoices(config, setup).forEach(choice => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `ob-opt kind-${choice.kind}` + (choice.value === current ? ' is-current' : '');
      btn.setAttribute('role', 'option');
      btn.setAttribute('aria-selected', choice.value === current ? 'true' : 'false');
      btn.dataset.value = choice.value;
      const icon = document.createElement('span');
      icon.className = 'ob-ico';
      icon.innerHTML = OUTBOUND_ICONS[choice.kind] || OUTBOUND_ICONS.physical;
      const copy = document.createElement('span');
      copy.className = 'ob-copy';
      const name = document.createElement('b');
      name.textContent = choice.name;
      const desc = document.createElement('small');
      desc.textContent = choice.desc;
      copy.append(name, desc);
      const check = document.createElement('span');
      check.className = 'ob-check';
      check.textContent = '✓';
      btn.append(icon, copy, check);
      btn.onclick = () => void changeDefaultOutbound(choice.value);
      box.append(btn);
    });
  }

  function placeOutboundPop() {
    const trigger = byId('proxy-current-node');
    const pop = byId('outbound-pop');
    if (!trigger || !pop || pop.classList.contains('hidden')) return;
    const rect = trigger.getBoundingClientRect();
    const gap = 8;
    const viewportPadding = 10;
    const wantedHeight = Math.min(pop.scrollHeight, 342);
    const below = window.innerHeight - rect.bottom - viewportPadding;
    const above = rect.top - viewportPadding;
    const openUpward = below < Math.min(wantedHeight, 200) && above > below;
    const available = Math.max(120, (openUpward ? above : below) - gap);
    const width = Math.min(rect.width, window.innerWidth - viewportPadding * 2);
    pop.style.left = `${Math.max(viewportPadding,
      Math.min(rect.left, window.innerWidth - width - viewportPadding))}px`;
    pop.style.width = `${width}px`;
    pop.style.maxHeight = `${Math.min(342, available)}px`;
    pop.style.top = openUpward
      ? `${Math.max(viewportPadding, rect.top - gap - Math.min(wantedHeight, available))}px`
      : `${rect.bottom + gap}px`;
  }

  function markOutboundKb() {
    const box = byId('outbound-options');
    if (!box) return;
    [...box.children].forEach((el, index) => el.classList.toggle('kb-focus', index === outboundKbIndex));
    box.children[outboundKbIndex]?.scrollIntoView({ block: 'nearest' });
  }

  function setOutboundOpen(open) {
    const switchBox = byId('outbound-switch');
    const trigger = byId('proxy-current-node');
    const pop = byId('outbound-pop');
    if (!switchBox || !trigger || !pop) return;
    if (open && (busy || outboundApplying)) return;
    outboundOpen = open;
    switchBox.classList.toggle('open', open);
    trigger.setAttribute('aria-expanded', String(open));
    pop.classList.toggle('hidden', !open);
    if (open) {
      const state = routingState();
      const config = state.appliedConfig || state.setup?.config;
      renderOutboundOptions(config, state.setup);
      outboundKbIndex = Math.max(0, outboundChoices(config, state.setup)
        .findIndex(item => item.value === String(config?.default_outbound || 'physical')));
      markOutboundKb();
      placeOutboundPop();
    }
  }

  async function changeDefaultOutbound(value) {
    if (busy || outboundApplying) return;
    const state = routingState();
    const config = state.appliedConfig || state.setup?.config;
    if (!config || !state.loaded) return;
    setOutboundOpen(false);
    if (value === String(config.default_outbound || 'physical')) return;
    const choice = outboundChoices(config, state.setup).find(item => item.value === value);
    const label = choice ? choice.name.replace(/^代理 · |^VPN · /, '') : outboundName(value, config);
    outboundApplying = true;
    sync();
    try {
      await applyConfig(
        clone(config), `默认出口已切换：${label}`, '正在切换出口…', '默认出口切换未完成',
        () => withTimeout(
          backend().set_routing_default_outbound(value), 45000,
          '默认出口切换请求超时，请检查服务状态后重试'));
    } finally {
      outboundApplying = false;
      sync();
    }
  }

  function syncNodeStats() {
    const groupId = byId('routing-node-group')?.value || selectedGroupId;
    const summary = window.RoutingWorkspace?.nodeSummary?.(groupId, true) || {};
    setText('proxy-node-total', String(summary.visible || 0));
    setText('proxy-node-total-label', summary.source === 'cache' ? '缓存记录' : '节点');
    setText('proxy-node-alive-total', String(summary.alive || 0));
  }

  function servicePhase(config, status) {
    if (status?.service_state === 'Unknown') return 'unknown';
    if (status?.running) return 'running';
    if (config?.enabled) return 'degraded';
    if (status?.standby || status?.runtime_mode === 'standby') return 'standby';
    return 'off';
  }

  function effectiveConfigSummary(config, mode = config?.traffic_mode, capture = config?.capture_mode) {
    const captureLabel = capture === 'tun' ? 'TUN（高级）' : 'Windows 系统代理';
    const traffic = mode === 'global' ? '全局模式' : '规则模式';
    return `${captureLabel} · ${traffic} · 默认出口 ${outboundName(config?.default_outbound, config)}`;
  }

  function sync() {
    if (outboundOpen && !proxyHomeVisible()) setOutboundOpen(false);
    const state = routingState();
    const { setup, appliedConfig, runtimeStatus, groups = [], loaded, dirty } = state;
    const config = appliedConfig || setup?.config;
    if (!loaded || !config) return;
    const status = runtimeStatus || setup?.status || {};
    const providers = enabledProviders(config);
    const cache = cacheSummary(setup, providers);
    const phase = servicePhase(config, status);
    const running = phase === 'running';
    const aggregate = config.default_outbound === 'proxy';
    const defaultUsesProxy = aggregate || String(config.default_outbound || '').startsWith('proxy:');
    const provider = targetProvider(config);
    selectedGroupId = aggregate ? 'all' : provider?.id || 'all';
    const group = aggregate ? groups.find(item => item.id === 'all') : preferredGroup(groups, provider);
    const node = currentNode(group);
    const persistedNodes = provider ? providerNodes(setup, provider.id) : [];
    const targetNode = provider?.selection_mode === 'manual'
      ? group?.nodes?.find(item => nodeMatchesPreference(item, provider, provider.selected_node))
        || persistedNodes.find(item => nodeMatchesPreference(item, provider, provider.selected_node, true))
      : null;
    const manualCurrent = running && provider?.selection_mode === 'manual' &&
      runtimeUsesPreference(group, provider);
    const guard = usesSubscriptionSelection(config) ? selectionGuard(setup, provider, group)
      : { valid: true, reason: '' };
    const summary = window.RoutingWorkspace?.nodeSummary?.(selectedGroupId) || {};
    renderOperationNotice();
    const actualCapture = running ? status.capture_mode || config.capture_mode : config.capture_mode;
    const { shownCapture, shownTraffic } = shownModes(config, status, running);
    renderModeConsole(config, status, running);
    const builtinCount = status.builtin_rule_pack?.rule_count || 0;
    const cnFallback = config.system_proxy_bypass?.include_cn_direct ? ' / GEOIP CN' : '';
    const businessCount = config.traffic_mode === 'global' ? 0 : (config.business_rules || [])
      .filter(item => item.enabled !== false).reduce((count, item) => count + item.domains.length, 0);
    setText('proxy-rule-composition', `${(config.rules || []).filter(item => item.enabled !== false).length} user / ${businessCount} business / ${config.traffic_mode === 'global' ? 0 : builtinCount} builtin${cnFallback} / MATCH`);

    const serviceDot = byId('proxy-service-dot');
    serviceDot?.classList.toggle('on', running);
    serviceDot?.classList.toggle('warn', phase === 'degraded' || phase === 'unknown');
    const proxyOrbit = byId('proxy-orbit');
    proxyOrbit?.classList.toggle('is-active', running);
    proxyOrbit?.classList.toggle('is-warning', phase === 'degraded' || phase === 'unknown');
    setText('proxy-service-label', running ? '内置服务运行中'
      : phase === 'degraded' ? '配置已启用，服务异常'
        : phase === 'unknown' ? '无法确认服务状态'
          : phase === 'standby' ? '节点核心待机中' : '内置服务已就绪');
    setText('proxy-state-title', running ? '代理已开启'
      : phase === 'degraded' ? '代理需要修复'
        : phase === 'unknown' ? '服务状态未知' : '代理未开启');
    setText('proxy-state-detail', running
      ? status.configuration_pending
        ? `现有 ${actualCapture === 'tun' ? 'TUN' : 'Windows 系统代理'} 仍在运行，保存配置与运行状态不一致；请检查配置后重新应用。`
        : `${actualCapture === 'tun' ? 'TUN' : 'Windows 系统代理'}接管 · ${config.traffic_mode === 'global' ? '全局' : '规则'}策略 · 默认出口 ${outboundName(config.default_outbound, config)}。${usesProxy(config) ? '' : '当前配置未使用代理订阅。'}`
      : phase === 'unknown'
        ? '暂时无法读取 Windows 服务状态。为避免重复启动或误判关闭，请先重新检查。'
        : phase === 'degraded'
        ? status.crash_fused ? 'Mihomo 连续崩溃已熔断，Windows 系统接管已安全恢复。请查看诊断日志后重试。'
          : '已保存配置，但服务没有正常运行。可重试启动，或仅关闭配置恢复到未启用状态。'
        : phase === 'standby'
          ? 'Windows 保持原始路由；后台节点核心仅监听本机回环，可更新订阅并测试当前已有节点。'
        : dirty
          ? `当前网络保持 Windows 原始路由。下次开启将使用：${effectiveConfigSummary(config, shownTraffic, shownCapture)}；除首页当前流量模式与接管方式外，高级分流未保存草稿不会自动生效。`
          : `当前网络保持 Windows 原始路由。下次开启将使用：${effectiveConfigSummary(config, shownTraffic, shownCapture)}。`);

    const draftNotice = byId('proxy-draft-notice');
    draftNotice?.classList.toggle('hidden', !dirty);

    const toggle = byId('btn-proxy-toggle');
    if (toggle) {
      toggle.textContent = busy && busyLabel ? busyLabel
        : running ? '关闭代理' : phase === 'degraded' ? '重试启动'
        : phase === 'unknown' ? '重新检查' : '开启代理';
      toggle.classList.toggle('danger', running);
      toggle.classList.toggle('primary', !running);
      toggle.disabled = busy || (!running && state.busy) || (!running && phase !== 'unknown' && !guard.valid);
      toggle.title = !running && !guard.valid ? guard.reason : '';
    }
    const disable = byId('btn-proxy-disable');
    if (disable) {
      disable.classList.toggle('hidden', phase !== 'degraded');
      disable.disabled = busy;
    }

    const warning = byId('proxy-selection-warning');
    warning?.classList.toggle('hidden', running || guard.valid);
    setText('proxy-selection-warning-text', guard.reason);
    setText('btn-proxy-choose-node', guard.action || '去选择节点');

    setText('proxy-provider-name', provider?.name || '尚未配置');
    setText('proxy-provider-meta', providers.length
      ? `${providerModeLabel(provider)} · ${providers.length} 个已启用订阅` : '前往添加订阅');
    const outboundChoice = outboundChoiceOf(config, setup);
    const outboundIcon = byId('proxy-outbound-icon');
    if (outboundIcon) outboundIcon.innerHTML = OUTBOUND_ICONS[outboundChoice.kind] || OUTBOUND_ICONS.physical;
    const outboundTrigger = byId('proxy-current-node');
    outboundTrigger?.classList.toggle('is-block', outboundChoice.kind === 'block');
    outboundTrigger?.classList.toggle('is-vpn', outboundChoice.kind === 'vpn');
    if (aggregate) {
      const manual = config.aggregate_selection?.mode === 'manual';
      setText('proxy-provider-name', '全部代理订阅');
      setText('proxy-provider-meta', `${manual ? '手动固定' : '订阅间自动选择'} · ${providers.length} 个已启用订阅`);
      setText('proxy-node-name', manual
        ? `全部订阅 · ${config.aggregate_selection.node_name}` : '全部代理订阅');
      setText('proxy-node-delay', manual ? manualCurrent ? '当前使用' : targetNodeTestSummary(targetNode)
        : running && node ? `${node.name} · 当前使用` : '启动后按各订阅策略择优');
    } else if (defaultUsesProxy && provider) {
      setText('proxy-node-name', provider.selection_mode === 'manual'
        ? `${provider.name} · ${targetNode?.display_name || provider.selected_node || '尚未选择'}`
        : `${provider.name} · 自动优选`);
      setText('proxy-node-delay', provider.selection_mode === 'manual'
        ? manualCurrent ? '当前使用' : targetNodeTestSummary(targetNode)
        : running && node ? `${node.display_name || node.name} · 当前使用` : '启动后自动选择');
    } else {
      setText('proxy-node-name', outboundChoice.name);
      const vpnExit = outboundChoice.kind === 'vpn'
        ? (setup?.vpns || []).find(item => `vpn:${item.name}` === String(config.default_outbound))
        : null;
      setText('proxy-node-delay', outboundChoice.kind === 'physical' ? '不走代理'
        : outboundChoice.kind === 'block' ? '未命中流量将被拒绝'
        : vpnExit ? `Windows VPN · ${vpnExit.status === 'Connected' ? '已连接' : '未连接'}`
        : '出口不可用，请重新选择');
    }
    byId('btn-outbound-nodes')?.classList.toggle('hidden', !defaultUsesProxy);
    setText('proxy-provider-count', `${providers.length} 个`);
    setText('proxy-node-count', `${summary.visible || cache.total} 个`);
    setText('proxy-cache-state', cache.available ? `已保存 ${cache.total} 条缓存记录` : '尚无可用缓存');
    renderSubscriptionHealth(setup, provider);
    const nodePool = dashboardNodePool(setup, groups, providers, selectedGroupId);
    renderNodeQuality(nodePool);
    renderTopNodes(nodePool, group, provider);
    setText('proxy-session-state', running ? '运行正常' : phase === 'degraded' ? '需要处理'
      : phase === 'standby' ? '节点核心待机' : '未启用');
    setText('proxy-config-state', config.enabled ? '已启用' : '未启用');
    setText('proxy-runtime-state', running ? '接管中' : status.service_state === 'Unknown' ? '状态未知'
      : phase === 'standby' ? '待机中' : '未运行');
    setText('proxy-health-state', summary.tested ? `${summary.alive}/${summary.tested} 可用` : '尚未测速');
    renderObservability(latestObservability || setup?.observability);
    setText('proxy-recent-node', running ? node?.display_name || group?.selected || '正在选择代理节点' : '尚未连接代理节点');
    setText('proxy-recent-delay', node?.delay ? `${node.delay} ms` : '未测速');
    setText('proxy-recent-source', running && group ? `${aggregate ? group.name : provider?.name || group.name} · 实际运行节点`
      : aggregate ? '全部代理订阅 · 出口偏好已保存' : provider ? `${provider.name} · 目标偏好已保存` : '等待配置订阅');
    byId('proxy-recent-delay')?.classList.toggle('on', !!node?.delay);
    byId('proxy-current-provider').disabled = busy;
    if (outboundApplying) setText('proxy-node-delay', '正在切换出口…');
    outboundTrigger?.classList.toggle('applying', outboundApplying);
    byId('proxy-current-node').disabled = busy || outboundApplying;
    syncNodeStats();
    reconcileTelemetry(running);
    if (!running) updateTelemetryState({ phase: 'idle' });
    else renderTelemetry();
  }

  async function applyConfig(config, message, progressText = '正在应用…',
    failureTitle = '代理操作未完成', submit = null) {
    setOperationNotice();
    window.RoutingWorkspace?.beginMutation?.();
    busy = true;
    busyLabel = progressText;
    sync();
    try {
      const result = await (submit ? submit() : backend().apply_routing(config));
      if (result?.ok === false) throw new Error(result.msg || message);
      window.RoutingWorkspace?.acceptApplyResult?.(result);
      void window.RoutingWorkspace?.refreshBackground?.(true);
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      toast({ ok: true, tone: warnings.length ? 'warning' : 'success',
        msg: [result.msg || message, ...warnings].join('；') });
      return true;
    } catch (error) {
      const detail = typeof friendlyError === 'function' ? friendlyError(error) : String(error);
      void window.RoutingWorkspace?.refreshBackground?.(true);
      setOperationNotice(failureTitle, detail);
      if (!proxyHomeVisible()) toast({ ok: false, msg: failureTitle });
      return false;
    } finally {
      busy = false;
      busyLabel = '';
      sync();
    }
  }

  async function changeProxyState(forceDisable = false) {
    if (busy) return;
    const cached = routingState();
    const cachedSetup = cached.setup || {};
    const config = clone(cached.appliedConfig || cachedSetup.config);
    const cachedStatus = cached.runtimeStatus || cachedSetup.status || {};
    if (!config || !cached.loaded) {
      toast({ ok: false, tone: 'warning', msg: '代理配置仍在读取，请稍候再试' });
      return;
    }
    const providers = enabledProviders(config);
    const provider = targetProvider(config);
    const cachedGroups = cached.groups || cachedSetup.proxy_groups || [];
    const targetGroup = config.default_outbound === 'proxy'
      ? cachedGroups.find(item => item.id === 'all') : preferredGroup(cachedGroups, provider);
    const phase = servicePhase(config, cachedStatus);
    if (phase === 'unknown') {
      busy = true;
      busyLabel = '正在重新检查…';
      sync();
      await window.RoutingWorkspace.refresh(true);
      busy = false;
      busyLabel = '';
      const refreshed = routingState().runtimeStatus;
      toast({
        ok: refreshed?.service_state !== 'Unknown',
        tone: refreshed?.service_state === 'Unknown' ? 'warning' : 'success',
        msg: refreshed?.service_state === 'Unknown' ? '仍无法确认 Windows 服务状态，请稍后重试' : '服务状态已更新',
      });
      sync();
      return;
    }
    const nextEnabled = forceDisable ? false : phase !== 'running';
    if (nextEnabled && usesProxy(config) && !providers.length) {
      toast({ ok: false, tone: 'warning', msg: '请先添加并启用至少一个代理订阅' });
      openSubscriptions();
      return;
    }
    const guard = usesSubscriptionSelection(config) ? selectionGuard(cachedSetup, provider, targetGroup)
      : { valid: true, reason: '' };
    if (nextEnabled && !guard.valid) {
      toast({ ok: false, tone: 'warning', msg: guard.reason });
      if (config.default_outbound === 'proxy') openNodes('all');
      else if (provider) openNodes(provider.id); else openSubscriptions();
      return;
    }
    const repairing = phase === 'degraded' && nextEnabled;
    const targetCapture = nextEnabled ? (selectedCapture || config.capture_mode || 'system-proxy') : config.capture_mode;
    const targetMode = nextEnabled ? (selectedMode || config.traffic_mode || 'rule') : config.traffic_mode;
    if (nextEnabled && targetCapture === 'tun') {
      const conflicts = cachedSetup.tun_conflicts || [];
      if (conflicts.length) {
        const names = [...new Set(conflicts.map(item => item.name).filter(Boolean))].join('、');
        toast({ ok: false, tone: 'warning', msg: `检测到其他 TUN 正在承载默认路由：${names}，请先关闭对应客户端的 TUN 模式` });
        return;
      }
    }
    const nativeServiceReady = cachedStatus.installed
      && cachedStatus.service_backend === 'native'
      && !cachedStatus.service_update_required;
    const fastToggleReady = config.capture_mode === 'system-proxy'
      && cachedStatus.fast_toggle_ready && cachedStatus.core_running;
    const needsUac = nextEnabled ? !nativeServiceReady
      : cachedStatus.service_backend === 'legacy';
    const draftNotice = nextEnabled && cached.dirty
      ? '高级分流仍有未保存草稿。除首页当前流量模式与接管方式外，本次不会带入这些草稿；如需应用草稿，请先取消并到高级分流保存。'
      : '';
    const needsConfirmation = nextEnabled
      ? !nativeServiceReady || targetCapture === 'tun' || cached.dirty || repairing
      : needsUac;
    const directNotice = nextEnabled && !usesProxy({ ...config, traffic_mode: targetMode })
      ? `当前默认出口为「${outboundName(config.default_outbound, config)}」，本次配置未使用代理订阅；如需走代理，请先在高级分流调整默认出口或规则。`
      : '';
    const confirmed = !needsConfirmation || await confirmAction({
      title: nextEnabled ? (repairing ? '修复网络代理' : '开启网络代理') : '关闭网络代理',
      message: nextEnabled
        ? `${repairing ? '将重新检查并修复代理服务' : targetCapture === 'tun' ? '将使用 TUN（高级）接管系统流量' : '将通过 Windows 系统代理接管应用流量'}。本次生效配置：${effectiveConfigSummary(config, targetMode, targetCapture)}。${draftNotice}${directNotice}确认后会自动检查节点和网络，失败会恢复原设置。${targetCapture === 'tun' ? '请先关闭其他代理软件的 TUN 模式和 Windows 系统代理。' : ''}${needsUac ? '首次使用可能需要 Windows 管理员授权。' : ''}`
        : needsUac
          ? '将停止并迁移旧版 Mihomo TUN 服务，系统恢复使用 Windows 当前路由。该操作需要 UAC。'
          : fastToggleReady
            ? '将关闭 Windows 系统代理接管；Mihomo 节点核心继续待机。'
            : '将停止 Mihomo 运行时并恢复 Windows 当前路由；路由服务会保留，以便下次免 UAC 启动。',
      confirmText: needsUac ? (nextEnabled ? '授权并开启' : '授权并关闭')
        : nextEnabled ? (repairing ? '确认修复' : '确认开启') : '确认关闭',
      tone: nextEnabled && targetCapture === 'tun' ? 'danger' : 'notice',
      kicker: nextEnabled ? '网络接管确认' : '停止网络接管',
    });
    if (!confirmed) return;
    const ok = await applyConfig(
      config,
      nextEnabled ? (repairing ? '代理服务已修复' : '代理已开启') : '代理已关闭',
      nextEnabled ? '正在启动代理…' : '正在关闭代理…',
      nextEnabled ? '代理配置应用失败，请检查当前运行状态' : '代理关闭未完成',
      () => backend().set_routing_enabled(
        nextEnabled, nextEnabled ? selectedMode : '', nextEnabled ? selectedCapture : ''));
    if (ok && nextEnabled) {
      selectedMode = '';
      selectedCapture = '';
    }
  }

  async function changeMode(mode) {
    if (busy) return;
    const state = routingState();
    const config = state.appliedConfig || state.setup?.config;
    if (!config || !state.loaded) return;
    const running = !!state.runtimeStatus?.running;
    const applied = config.traffic_mode === 'global' ? 'global' : 'rule';
    if (!running) {
      const shown = selectedMode || applied;
      selectedMode = mode === applied || mode === shown ? '' : mode;
      sync();
      return;
    }
    if (mode === applied) return;
    // 规则/全局只改分流行为，运行中即时切换，无需确认打断。
    await applyConfig(clone(config), mode === 'global' ? '已切换到全局模式' : '已切换到规则模式',
      '正在切换模式…', '代理模式切换未完成',
      () => backend().set_routing_enabled(true, mode));
  }

  async function changeCapture(capture) {
    if (busy) return;
    const state = routingState();
    const { setup, appliedConfig, runtimeStatus } = state;
    const config = appliedConfig || setup?.config;
    if (!config || !state.loaded) return;
    const running = !!runtimeStatus?.running;
    const status = runtimeStatus || setup?.status || {};
    const applied = appliedCaptureMode(config, status, running);
    const conflicts = setup?.tun_conflicts || [];
    if (capture === 'tun' && conflicts.length && capture !== applied) {
      const names = [...new Set(conflicts.map(item => item.name).filter(Boolean))].join('、');
      toast({ ok: false, tone: 'warning', msg: `检测到其他 TUN 正在承载默认路由：${names}，请先关闭对应客户端的 TUN 模式` });
      return;
    }
    if (!running) {
      const shown = selectedCapture || applied;
      selectedCapture = capture === applied || capture === shown ? '' : capture;
      sync();
      return;
    }
    if (capture === applied) return;
    const confirmed = await confirmAction(capture === 'tun' ? {
      title: '切换到 TUN 模式',
      message: '将创建虚拟网卡接管系统全部流量（含不遵循系统代理的应用），并自动关闭 Windows 系统代理。切换过程需要重建网络配置，期间连接会短暂中断，失败会自动恢复原设置。切换前请确认其他代理软件的 TUN 模式已关闭；首次使用可能需要 Windows 管理员授权。',
      confirmText: '授权并切换',
      tone: 'danger',
      kicker: '网络接管确认',
    } : {
      title: '切换到系统代理',
      message: '将移除 TUN 虚拟网卡，恢复 Windows 系统代理接管。期间网络连接会短暂中断，完成后仅接管遵循系统代理的应用。',
      confirmText: '确认切换',
      tone: 'notice',
      kicker: '网络接管确认',
    });
    if (!confirmed) return;
    await applyConfig(clone(config), capture === 'tun' ? '已切换到 TUN 模式' : '已切换到系统代理',
      '正在切换接管方式…', '接管方式切换未完成',
      () => backend().set_routing_enabled(true, '', capture));
  }

  async function openNodes(groupId = '', origin = null) {
    if (groupId) selectedGroupId = groupId;
    // 页面切换和加载会触发 sync，必须保留本次点击指定的订阅。
    const requestedGroupId = selectedGroupId;
    returnTarget = ['routing', 'subscriptions', 'rules'].includes(origin?.page)
      ? { page: origin.page, tab: origin.tab || 'overview' }
      : { page: 'proxy', tab: 'home' };
    setText('btn-proxy-nodes-back', returnTarget.page === 'routing'
      ? '返回高级分流' : returnTarget.page === 'subscriptions'
        ? '返回订阅' : returnTarget.page === 'rules' ? '返回规则' : '返回网络代理');
    await goToPage('nodes');
    await window.RoutingWorkspace?.load?.();
    window.RoutingWorkspace?.openNodes?.(requestedGroupId);
    const select = byId('routing-node-group');
    if (requestedGroupId && select && [...select.options].some(item => item.value === requestedGroupId)) {
      select.value = requestedGroupId;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    syncNodeStats();
  }

  function openHome() {
    if (!byId('page-proxy')?.classList.contains('active')) void goToPage('proxy');
    setView('home');
    sync();
  }

  function leaveNodes() {
    if (returnTarget.page !== 'proxy') {
      void goToPage(returnTarget.page);
      if (returnTarget.page === 'routing') {
        setTimeout(() => window.RoutingWorkspace?.openTab?.(returnTarget.tab), 0);
      }
      return;
    }
    void goToPage('proxy');
    openHome();
  }

  function openSubscriptions() {
    void goToPage('subscriptions');
  }

  function openAdvancedRouting() {
    void goToPage('routing');
    setTimeout(() => window.RoutingWorkspace?.openTab?.('overview'), 0);
  }

  function reviewRoutingDraft() {
    void goToPage('routing');
  }

  function changeSeg(tile) {
    const seg = tile.closest('[data-proxy-seg]')?.dataset.proxySeg;
    if (seg === 'capture') void changeCapture(tile.dataset.value);
    else if (seg === 'traffic') void changeMode(tile.dataset.value);
  }

  function bindSegKeyboard(tile) {
    tile.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const tiles = [...tile.closest('[data-proxy-seg]').querySelectorAll('.mode-tile')];
      const index = tiles.indexOf(tile);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tiles.length - 1
        : (index + (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1) + tiles.length) % tiles.length;
      tiles[next].focus();
      changeSeg(tiles[next]);
    };
  }

  function bind() {
    bindRateChart();
    byId('btn-proxy-toggle').onclick = () => changeProxyState(false);
    byId('btn-proxy-disable').onclick = () => changeProxyState(true);
    byId('btn-proxy-refresh').onclick = async () => {
      if (busy) return;
      busy = true;
      try { await window.RoutingWorkspace.refresh(true); }
      finally { busy = false; sync(); }
    };
    byId('proxy-current-provider').onclick = openSubscriptions;
    byId('proxy-current-node').onclick = () => setOutboundOpen(!outboundOpen);
    byId('btn-outbound-nodes').onclick = () => {
      setOutboundOpen(false);
      openNodes(selectedGroupId);
    };
    byId('btn-outbound-rules').onclick = () => {
      setOutboundOpen(false);
      void goToPage('routing');
      setTimeout(() => window.RoutingWorkspace?.openTab?.('rules'), 0);
    };
    document.addEventListener('click', event => {
      if (outboundOpen && !byId('outbound-switch')?.contains(event.target)) setOutboundOpen(false);
    });
    document.addEventListener('keydown', event => {
      if (!outboundOpen) return;
      const box = byId('outbound-options');
      const count = box?.children.length || 0;
      if (event.key === 'Escape') {
        setOutboundOpen(false);
        byId('proxy-current-node')?.focus();
      } else if (event.key === 'ArrowDown' && count) {
        outboundKbIndex = (outboundKbIndex + 1) % count;
        markOutboundKb();
      } else if (event.key === 'ArrowUp' && count) {
        outboundKbIndex = (outboundKbIndex - 1 + count) % count;
        markOutboundKb();
      } else if (event.key === 'Home' && count) {
        outboundKbIndex = 0;
        markOutboundKb();
      } else if (event.key === 'End' && count) {
        outboundKbIndex = count - 1;
        markOutboundKb();
      } else if (event.key === 'Enter' && outboundKbIndex >= 0 && count) {
        const target = box.children[outboundKbIndex];
        if (target?.dataset.value) void changeDefaultOutbound(target.dataset.value);
      } else {
        return;
      }
      event.preventDefault();
    });
    window.addEventListener('resize', placeOutboundPop);
    window.addEventListener('scroll', () => placeOutboundPop(), true);
    byId('btn-proxy-manage').onclick = openSubscriptions;
    byId('btn-top-nodes-all').onclick = () => openNodes(selectedGroupId);
    byId('btn-proxy-subscriptions').onclick = openSubscriptions;
    byId('btn-proxy-nodes').onclick = () => openNodes(selectedGroupId);
    byId('btn-proxy-choose-node').onclick = () => {
      const state = routingState();
      const provider = targetProvider(state.appliedConfig || state.setup?.config);
      if (provider) openNodes(provider.id); else openSubscriptions();
    };
    byId('btn-proxy-routing').onclick = openAdvancedRouting;
    byId('btn-proxy-review-draft').onclick = reviewRoutingDraft;
    byId('btn-proxy-operation-dismiss').onclick = () => setOperationNotice();
    byId('btn-proxy-nodes-back').onclick = leaveNodes;
    document.querySelectorAll('.mode-tile').forEach(tile => {
      tile.onclick = () => changeSeg(tile);
      bindSegKeyboard(tile);
    });
    byId('routing-node-group')?.addEventListener('change', syncNodeStats);
    document.querySelector('#nav [data-page="proxy"]')?.addEventListener('click', () => {
      setView('home');
      sync();
      void window.RoutingWorkspace?.loadForProxyHome?.().then(sync);
    });
    document.querySelector('#nav [data-page="nodes"]')?.addEventListener('click', () => {
      returnTarget = { page: 'proxy', tab: 'home' };
      setText('btn-proxy-nodes-back', '返回网络代理');
    });
    window.addEventListener('cxvpn:pagechange', sync);
    window.addEventListener('cxvpn:uistate', event => {
      applyTelemetrySnapshot(event.detail?.routing_telemetry);
    });
    document.addEventListener('visibilitychange', sync);
  }

  window.ProxyWorkspace = {
    sync, openNodes, openSubscriptions, openHome, updateTelemetry, updateTelemetryState,
    setOutboundOpen, changeDefaultOutbound, placeOutboundPop,
  };
  window.addEventListener('pywebviewready', bind);
})();
