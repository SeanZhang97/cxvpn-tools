(() => {
  const byId = id => document.getElementById(id);
  const backend = () => window.pywebview?.api;
  let selectedMode = 'rule';
  let selectedGroupId = 'all';
  let returnTarget = { page: 'proxy', tab: 'home' };
  let busy = false;
  let busyLabel = '';
  let operationNotice = null;
  let telemetryActive = null;
  let latestObservability = null;
  let telemetryState = { phase: 'idle' };
  let telemetryData = { up: 0, down: 0, upTotal: 0, downTotal: 0 };

  function clone(value) {
    return JSON.parse(JSON.stringify(value || {}));
  }

  function routingState() {
    return window.RoutingWorkspace?.state?.() || {};
  }

  function enabledProviders(config) {
    return (config?.proxy_providers || []).filter(item => item.enabled);
  }

  function usesProxy(config) {
    const values = [config?.default_outbound, ...(config?.rules || [])
      .filter(item => item.enabled !== false).map(item => item.outbound)];
    return values.some(value => value === 'proxy' || String(value || '').startsWith('proxy:'));
  }

  function outboundName(value) {
    const raw = String(value || 'physical');
    if (raw === 'physical') return '物理网络';
    if (raw === 'proxy') return '聚合代理池';
    if (raw === 'block') return '阻止访问';
    if (raw.startsWith('proxy:')) return `代理订阅 ${raw.slice(6)}`;
    if (raw.startsWith('vpn:')) return `VPN ${raw.slice(4)}`;
    return raw;
  }

  function targetProvider(config) {
    const providers = enabledProviders(config);
    const outbound = String(config?.default_outbound || '');
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
    setText('proxy-upload-rate', tools?.formatRate?.(telemetryData.up) || '0 B/s');
    setText('proxy-download-rate', tools?.formatRate?.(telemetryData.down) || '0 B/s');
    const state = byId('proxy-stream-state');
    if (!state) return;
    const labels = {
      connected: '实时流量', connecting: '正在连接', reconnecting: '重连中', idle: '等待服务',
    };
    state.textContent = labels[telemetryState.phase] || '等待服务';
    state.classList.toggle('connected', telemetryState.phase === 'connected');
    state.classList.toggle('reconnecting', telemetryState.phase === 'reconnecting');
    state.title = telemetryState.phase === 'reconnecting' && telemetryState.retrySeconds
      ? `${telemetryState.retrySeconds} 秒后重试` : '';
  }

  function updateTelemetry(data) {
    telemetryData = { ...telemetryData, ...(data || {}) };
    renderTelemetry();
  }

  function updateTelemetryState(state) {
    telemetryState = state || { phase: 'idle' };
    if (telemetryState.phase !== 'connected') {
      telemetryData = { up: 0, down: 0, upTotal: 0, downTotal: 0 };
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

  function setModeUi(mode) {
    selectedMode = mode === 'global' ? 'global' : 'rule';
    document.querySelectorAll('[data-proxy-mode]').forEach(button => {
      const selected = button.dataset.proxyMode === selectedMode;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-checked', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    const label = selectedMode === 'global' ? '全局模式' : '规则模式';
    setText('proxy-mode-status', label);
    setText('proxy-current-mode', label);
    setText('proxy-mode-detail', selectedMode === 'global'
      ? '所有流量都通过当前代理，临时忽略域名分流规则。'
      : '按域名分流规则匹配，未命中流量走当前代理。');
  }

  function setView(name) {
    document.querySelectorAll('[data-proxy-view]').forEach(view => {
      view.classList.toggle('active', view.dataset.proxyView === name);
    });
    byId('main-content')?.scrollTo({ top: 0, behavior: 'smooth' });
    const state = routingState();
    if (state.loaded) reconcileTelemetry(!!state.runtimeStatus?.running);
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

  function effectiveConfigSummary(config, mode = config?.traffic_mode) {
    const capture = config?.capture_mode === 'tun' ? 'TUN（高级）' : 'Windows 系统代理';
    const traffic = mode === 'global' ? '全局模式' : '规则模式';
    return `${capture} · ${traffic} · 默认出口 ${outboundName(config?.default_outbound)}`;
  }

  function sync() {
    const state = routingState();
    const { setup, appliedConfig, runtimeStatus, groups = [], loaded, dirty } = state;
    const config = appliedConfig || setup?.config;
    if (!loaded || !config) return;
    const status = runtimeStatus || setup?.status || {};
    const providers = enabledProviders(config);
    const cache = cacheSummary(setup, providers);
    const phase = servicePhase(config, status);
    const running = phase === 'running';
    const provider = targetProvider(config);
    selectedGroupId = provider?.id || 'all';
    const group = preferredGroup(groups, provider);
    const node = currentNode(group);
    const persistedNodes = provider ? providerNodes(setup, provider.id) : [];
    const targetNode = provider?.selection_mode === 'manual'
      ? persistedNodes.find(item => nodeMatchesPreference(item, provider, provider.selected_node, true))
        || group?.nodes?.find(item => nodeMatchesPreference(item, provider, provider.selected_node))
      : null;
    const manualCurrent = running && provider?.selection_mode === 'manual' &&
      runtimeUsesPreference(group, provider);
    const guard = usesProxy(config) ? selectionGuard(setup, provider, group)
      : { valid: true, reason: '' };
    const summary = window.RoutingWorkspace?.nodeSummary?.(provider?.id || 'all') || {};
    renderOperationNotice();
    setModeUi(config.traffic_mode || selectedMode);
    setText('proxy-capture-status', config.capture_mode === 'tun' ? 'TUN（高级）' : 'Windows 系统代理');
    setText('proxy-default-outbound', outboundName(config.default_outbound));
    const builtinCount = status.builtin_rule_pack?.rule_count || 0;
    setText('proxy-rule-composition', `${(config.rules || []).filter(item => item.enabled !== false).length} user / ${config.traffic_mode === 'global' ? 0 : builtinCount} builtin / MATCH`);

    const serviceDot = byId('proxy-service-dot');
    serviceDot?.classList.toggle('on', running);
    serviceDot?.classList.toggle('warn', phase === 'degraded' || phase === 'unknown');
    setText('proxy-service-label', running ? '内置服务运行中'
      : phase === 'degraded' ? '配置已启用，服务异常'
        : phase === 'unknown' ? '无法确认服务状态'
          : phase === 'standby' ? '节点核心待机中' : '内置服务已就绪');
    setText('proxy-state-title', running ? '代理已开启'
      : phase === 'degraded' ? '代理需要修复'
        : phase === 'unknown' ? '服务状态未知' : '代理未开启');
    setText('proxy-state-detail', running
      ? `${config.capture_mode === 'tun' ? 'TUN' : 'Windows 系统代理'}接管 · ${selectedMode === 'global' ? '全局' : '规则'}策略 · 默认出口 ${outboundName(config.default_outbound)}。`
      : phase === 'unknown'
        ? '暂时无法读取 Windows 服务状态。为避免重复启动或误判关闭，请先重新检查。'
        : phase === 'degraded'
        ? status.crash_fused ? 'Mihomo 连续崩溃已熔断，Windows 系统接管已安全恢复。请查看诊断日志后重试。'
          : '已保存配置，但服务没有正常运行。可重试启动，或仅关闭配置恢复到未启用状态。'
        : phase === 'standby'
          ? 'Windows 保持原始路由；后台节点核心仅监听本机回环，可更新订阅并测试当前已有节点。'
        : dirty
          ? `当前网络保持 Windows 原始路由。下次开启将使用：${effectiveConfigSummary(config, selectedMode)}；除首页当前流量模式外，高级分流未保存草稿不会自动生效。`
          : `当前网络保持 Windows 原始路由。下次开启将使用：${effectiveConfigSummary(config, selectedMode)}。`);

    const draftNotice = byId('proxy-draft-notice');
    draftNotice?.classList.toggle('hidden', !dirty);

    const toggle = byId('btn-proxy-toggle');
    if (toggle) {
      toggle.textContent = busy && busyLabel ? busyLabel
        : running ? '关闭代理' : phase === 'degraded' ? '重试启动'
        : phase === 'unknown' ? '重新检查' : '开启代理';
      toggle.classList.toggle('danger', running);
      toggle.classList.toggle('primary', !running);
      toggle.disabled = busy || state.busy || (!running && phase !== 'unknown' && !guard.valid);
      toggle.title = !running && !guard.valid ? guard.reason : '';
    }
    const disable = byId('btn-proxy-disable');
    if (disable) {
      disable.classList.toggle('hidden', phase !== 'degraded');
      disable.disabled = busy || state.busy;
    }

    const warning = byId('proxy-selection-warning');
    warning?.classList.toggle('hidden', running || guard.valid);
    setText('proxy-selection-warning-text', guard.reason);
    setText('btn-proxy-choose-node', guard.action || '去选择节点');

    setText('proxy-provider-name', provider?.name || '尚未配置');
    setText('proxy-provider-meta', providers.length
      ? `${provider?.selection_mode === 'manual' ? '手动节点' : '自动优选'} · ${providers.length} 个已启用订阅` : '前往添加订阅');
    setText('proxy-node-name', provider?.selection_mode === 'manual'
      ? targetNode?.display_name || provider.selected_node || '尚未选择'
      : provider ? '自动优选' : '等待选择');
    setText('proxy-node-delay', provider?.selection_mode === 'manual'
      ? manualCurrent ? '当前使用' : targetNodeTestSummary(targetNode)
      : running && node ? `${node.display_name || node.name} · 当前使用` : '启动后自动选择');
    setText('proxy-provider-count', `${providers.length} 个`);
    setText('proxy-node-count', `${summary.visible || cache.total} 个`);
    setText('proxy-cache-state', cache.available ? `已保存 ${cache.total} 条缓存记录` : '尚无可用缓存');
    renderSubscriptionHealth(setup, provider);
    setText('proxy-session-state', running ? '运行正常' : phase === 'degraded' ? '需要处理'
      : phase === 'standby' ? '节点核心待机' : '未启用');
    setText('proxy-config-state', config.enabled ? '已启用' : '未启用');
    setText('proxy-runtime-state', running ? '接管中' : status.service_state === 'Unknown' ? '状态未知'
      : phase === 'standby' ? '待机中' : '未运行');
    setText('proxy-health-state', summary.tested ? `${summary.alive}/${summary.tested} 可用` : '尚未测速');
    renderObservability(latestObservability || setup?.observability);
    setText('proxy-recent-node', running ? node?.display_name || group?.selected || '正在选择代理节点' : '尚未连接代理节点');
    setText('proxy-recent-delay', node?.delay ? `${node.delay} ms` : '未测速');
    setText('proxy-recent-source', running && group ? `${provider?.name || group.name} · 实际运行节点` : provider ? `${provider.name} · 目标偏好已保存` : '等待配置订阅');
    byId('proxy-recent-delay')?.classList.toggle('on', !!node?.delay);
    byId('proxy-current-provider').disabled = busy;
    byId('proxy-current-node').disabled = busy || !provider;
    syncNodeStats();
    reconcileTelemetry(running);
  }

  async function latestSetup() {
    const result = await backend().get_routing_setup();
    if (result?.ok === false) throw new Error(result.msg || '读取代理配置失败');
    return result;
  }

  async function applyConfig(config, message, progressText = '正在应用…',
    failureTitle = '代理操作未完成', submit = null) {
    setOperationNotice();
    busy = true;
    busyLabel = progressText;
    sync();
    try {
      const result = await (submit ? submit() : backend().apply_routing(config));
      if (result?.ok === false) throw new Error(result.msg || message);
      window.RoutingWorkspace?.acceptApplyResult?.(result);
      void window.RoutingWorkspace?.refreshBackground?.(true);
      toast({ ok: true, msg: result.msg || message });
      return true;
    } catch (error) {
      const detail = typeof friendlyError === 'function' ? friendlyError(error) : String(error);
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
    const targetGroup = preferredGroup(cached.groups || cachedSetup.proxy_groups || [], provider);
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
    const guard = usesProxy(config) ? selectionGuard(cachedSetup, provider, targetGroup)
      : { valid: true, reason: '' };
    if (nextEnabled && !guard.valid) {
      toast({ ok: false, tone: 'warning', msg: guard.reason });
      if (provider) openNodes(provider.id); else openSubscriptions();
      return;
    }
    const repairing = phase === 'degraded' && nextEnabled;
    const nativeServiceReady = cachedStatus.installed
      && cachedStatus.service_backend === 'native'
      && !cachedStatus.service_update_required;
    const fastToggleReady = config.capture_mode === 'system-proxy'
      && cachedStatus.fast_toggle_ready && cachedStatus.core_running;
    const needsUac = nextEnabled ? !nativeServiceReady
      : cachedStatus.service_backend === 'legacy';
    const draftNotice = nextEnabled && cached.dirty
      ? '高级分流仍有未保存草稿。除首页当前流量模式外，本次不会带入这些草稿；如需应用草稿，请先取消并到高级分流保存。'
      : '';
    const confirmed = await confirmAction({
      title: nextEnabled ? (repairing ? '修复网络代理' : '开启网络代理') : '关闭网络代理',
      message: nextEnabled
        ? `${repairing ? '将重新检查并修复代理服务' : config.capture_mode === 'tun' ? '将使用 TUN（高级）接管系统流量' : '将通过 Windows 系统代理接管应用流量'}。本次生效配置：${effectiveConfigSummary(config, selectedMode)}。${draftNotice}确认后会自动检查节点和网络，失败会恢复原设置。${config.capture_mode === 'tun' ? '请先关闭其他代理软件的 TUN 模式。' : ''}${needsUac ? '首次使用可能需要 Windows 管理员授权。' : ''}`
        : needsUac
          ? '将停止并迁移旧版 Mihomo TUN 服务，系统恢复使用 Windows 当前路由。该操作需要 UAC。'
          : fastToggleReady
            ? '将关闭 Windows 系统代理接管；Mihomo 节点核心继续待机。'
            : '将停止 Mihomo 运行时并恢复 Windows 当前路由；路由服务会保留，以便下次免 UAC 启动。',
      confirmText: needsUac ? (nextEnabled ? '授权并开启' : '授权并关闭')
        : nextEnabled ? (repairing ? '确认修复' : '确认开启') : '确认关闭',
      tone: 'notice',
      kicker: nextEnabled ? '网络接管确认' : '停止网络接管',
    });
    if (!confirmed) return;
    await applyConfig(
      config,
      nextEnabled ? (repairing ? '代理服务已修复' : '代理已开启') : '代理已关闭',
      nextEnabled ? '正在启动代理…' : '正在关闭代理…',
      nextEnabled ? '代理未开启，系统路由已恢复' : '代理关闭未完成',
      () => backend().set_routing_enabled(
        nextEnabled, nextEnabled ? selectedMode : ''));
  }

  async function changeMode(mode) {
    if (busy || mode === selectedMode) return;
    const previous = selectedMode;
    setModeUi(mode);
    const state = routingState();
    if (!state.runtimeStatus?.running) return;
    const confirmed = await confirmAction({
      title: mode === 'global' ? '切换到全局模式' : '切换到规则模式',
      message: mode === 'global'
        ? '全局模式会临时忽略域名分流规则，让所有流量走当前代理。切回规则模式后原规则仍保留。'
        : '规则模式会恢复已保存的域名分流规则，未命中流量继续走当前代理。',
      confirmText: '确认切换',
    });
    if (!confirmed) {
      setModeUi(previous);
      return;
    }
    const latest = await latestSetup();
    const config = clone(latest.config);
    config.traffic_mode = mode;
    config.enabled = true;
    const ok = await applyConfig(config, '代理模式已切换');
    if (!ok) setModeUi(previous);
  }

  async function openNodes(groupId = '', origin = null) {
    if (groupId) selectedGroupId = groupId;
    returnTarget = ['routing', 'subscriptions', 'rules'].includes(origin?.page)
      ? { page: origin.page, tab: origin.tab || 'overview' }
      : { page: 'proxy', tab: 'home' };
    setText('btn-proxy-nodes-back', returnTarget.page === 'routing'
      ? '返回高级分流' : returnTarget.page === 'subscriptions'
        ? '返回订阅' : returnTarget.page === 'rules' ? '返回规则' : '返回网络代理');
    await goToPage('nodes');
    await window.RoutingWorkspace?.load?.();
    window.RoutingWorkspace?.openNodes?.(selectedGroupId);
    const select = byId('routing-node-group');
    if (selectedGroupId && select && [...select.options].some(item => item.value === selectedGroupId)) {
      select.value = selectedGroupId;
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

  function bindModeKeyboard(button) {
    button.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...document.querySelectorAll('[data-proxy-mode]')];
      const index = buttons.indexOf(button);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (index + (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
      changeMode(buttons[next].dataset.proxyMode);
    };
  }

  function bind() {
    byId('btn-proxy-toggle').onclick = () => changeProxyState(false);
    byId('btn-proxy-disable').onclick = () => changeProxyState(true);
    byId('btn-proxy-refresh').onclick = async () => {
      if (busy) return;
      busy = true;
      try { await window.RoutingWorkspace.refresh(true); }
      finally { busy = false; sync(); }
    };
    byId('proxy-current-provider').onclick = openSubscriptions;
    byId('proxy-current-node').onclick = () => openNodes(selectedGroupId);
    byId('btn-proxy-manage').onclick = openSubscriptions;
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
    document.querySelectorAll('[data-proxy-mode]').forEach(button => {
      button.onclick = () => changeMode(button.dataset.proxyMode);
      bindModeKeyboard(button);
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
  };
  window.addEventListener('pywebviewready', bind);
})();
