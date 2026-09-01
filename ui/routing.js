/* routing.js - 统一域名分流页面（独立于历史 app.js） */
(() => {
  let setup = null;
  let appliedConfig = null;
  let runtimeStatus = null;
  let routingConfig = null;
  let loaded = false;
  let busy = false;
  let setupInFlight = null;
  let bootstrapInFlight = null;
  let lastLoadedAt = 0;
  let savedSnapshot = '';
  let savedConfig = null;
  let dirty = false;
  let activeTab = 'overview';
  let pendingNodeGroup = '';
  const providerExpanded = new Set();
  const providerPreviews = new Map();
  const providerPreviewErrors = new Map();
  const routingTestJobs = new Map();
  const testStartBusy = new Set();
  let preferenceBusy = '';
  let nodeRegionFilter = 'all';
  let renderedNodeGroup = '';
  const SETUP_TTL_MS = 15000;
  const TEST_POLL_MS = 250;
  const TEST_POLL_MAX_MS = 5000;

  const byId = id => document.getElementById(id);
  const backend = () => window.pywebview.api;
  const nodeTools = window.RoutingNodeTools;
  let openSelect = null;
  let selectSequence = 0;

  function option(value, label, detail = '') {
    const node = document.createElement('option');
    node.value = value;
    node.textContent = label;
    node.dataset.detail = detail;
    return node;
  }

  function closeRoutingSelect(widget = openSelect, restoreFocus = false) {
    if (!widget) return;
    widget.root.classList.remove('open');
    widget.menu.classList.add('hidden');
    widget.trigger.setAttribute('aria-expanded', 'false');
    widget.trigger.removeAttribute('aria-activedescendant');
    widget.activeIndex = -1;
    if (restoreFocus) widget.trigger.focus();
    if (openSelect === widget) openSelect = null;
  }

  function placeRoutingMenu(widget) {
    if (!widget || widget.menu.classList.contains('hidden')) return;
    const rect = widget.trigger.getBoundingClientRect();
    const gap = 7;
    const viewportPadding = 10;
    const wantedHeight = Math.min(widget.menu.scrollHeight, 300);
    const below = window.innerHeight - rect.bottom - viewportPadding;
    const above = rect.top - viewportPadding;
    const openUpward = below < Math.min(wantedHeight, 180) && above > below;
    const available = Math.max(96, (openUpward ? above : below) - gap);
    widget.root.classList.toggle('opens-upward', openUpward);
    widget.menu.style.left = `${Math.max(viewportPadding, rect.left)}px`;
    widget.menu.style.width = `${Math.min(rect.width, window.innerWidth - viewportPadding * 2)}px`;
    widget.menu.style.maxHeight = `${Math.min(300, available)}px`;
    widget.menu.style.top = openUpward
      ? `${Math.max(viewportPadding, rect.top - gap - Math.min(wantedHeight, available))}px`
      : `${rect.bottom + gap}px`;
  }

  function createChevron() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('viewBox', '0 0 24 24');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm7 10 5 5 5-5');
    svg.append(path);
    return svg;
  }

  function enhanceRoutingSelect(select, { compact = false } = {}) {
    if (select._routingWidget) {
      select._routingWidget.compact = compact;
      select._routingWidget.root.classList.toggle('compact', compact);
      select._routingWidget.refresh();
      return select._routingWidget;
    }
    const root = document.createElement('div');
    root.className = `routing-select-widget${compact ? ' compact' : ''}`;
    select.parentNode.insertBefore(root, select);
    root.append(select);
    select.classList.add('routing-select-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'routing-select-trigger';
    trigger.id = `routing-select-trigger-${++selectSequence}`;
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    const current = document.createElement('span');
    current.className = 'routing-select-current';
    trigger.append(current, createChevron());
    root.append(trigger);

    const label = document.querySelector(`label[for="${select.id}"]`) || select.closest('.field')?.querySelector('label');
    if (label) {
      if (!label.id) label.id = `${trigger.id}-label`;
      trigger.setAttribute('aria-labelledby', label.id);
      if (select.id) label.htmlFor = trigger.id;
    } else {
      trigger.setAttribute('aria-label', select.getAttribute('aria-label') || '选择选项');
    }
    const menu = document.createElement('div');
    menu.id = `${trigger.id}-menu`;
    menu.className = 'routing-select-menu hidden';
    menu.setAttribute('role', 'listbox');
    trigger.setAttribute('aria-controls', menu.id);
    document.body.append(menu);

    const widget = {
      root, select, trigger, current, menu, compact, activeIndex: -1,
      refresh() {
        const options = [...select.options];
        const selected = options.find(item => item.value === select.value) || options[0];
        trigger.disabled = select.disabled;
        root.classList.toggle('disabled', select.disabled);
        if (select.disabled && openSelect === widget) closeRoutingSelect(widget);
        current.replaceChildren();
        const title = document.createElement('strong');
        title.textContent = selected?.textContent || '请选择';
        current.append(title);
        if (!widget.compact && selected?.dataset.detail) {
          const detail = document.createElement('small');
          detail.textContent = selected.dataset.detail;
          current.append(detail);
        }
        menu.replaceChildren();
        options.forEach((item, index) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.disabled = select.disabled || item.disabled;
          button.setAttribute('role', 'option');
          button.id = `${menu.id}-option-${index}`;
          button.dataset.index = String(index);
          button.dataset.value = item.value;
          const copy = document.createElement('span');
          const strong = document.createElement('strong');
          strong.textContent = item.textContent;
          const small = document.createElement('small');
          small.textContent = item.dataset.detail || '';
          copy.append(strong);
          if (small.textContent) copy.append(small);
          const check = document.createElement('i');
          check.className = 'routing-select-indicator';
          check.setAttribute('aria-hidden', 'true');
          check.innerHTML = '<svg viewBox="0 0 24 24"><path d="m7.5 12.5 3 3 6.5-7"/></svg>';
          const active = item === selected;
          button.classList.toggle('selected', active);
          button.setAttribute('aria-selected', String(active));
          button.append(copy, check);
          button.onpointerenter = () => widget.setActive(index);
          button.onclick = event => {
            event.stopPropagation();
            widget.choose(index);
          };
          menu.append(button);
        });
        if (openSelect === widget) placeRoutingMenu(widget);
      },
      setActive(index) {
        const items = [...menu.querySelectorAll('[role="option"]')];
        if (!items.length) return;
        widget.activeIndex = Math.max(0, Math.min(index, items.length - 1));
        items.forEach((item, itemIndex) => item.classList.toggle('active', itemIndex === widget.activeIndex));
        widget.trigger.setAttribute('aria-activedescendant', items[widget.activeIndex].id);
        items[widget.activeIndex].scrollIntoView({ block: 'nearest' });
      },
      choose(index) {
        if (select.disabled) return;
        const selected = select.options[index];
        if (!selected || selected.disabled) return;
        select.value = selected.value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        widget.refresh();
        closeRoutingSelect(widget, true);
      },
      open() {
        if (select.disabled) return;
        if (openSelect && openSelect !== widget) closeRoutingSelect(openSelect);
        openSelect = widget;
        root.classList.add('open');
        menu.classList.remove('hidden');
        trigger.setAttribute('aria-expanded', 'true');
        const selectedIndex = Math.max(0, select.selectedIndex);
        widget.setActive(selectedIndex);
        placeRoutingMenu(widget);
      },
      destroy() {
        closeRoutingSelect(widget);
        menu.remove();
        select._routingWidget = null;
      },
    };
    select._routingWidget = widget;
    trigger.onclick = event => {
      event.stopPropagation();
      if (select.disabled) return;
      if (openSelect === widget) closeRoutingSelect(widget);
      else widget.open();
    };
    trigger.onkeydown = event => {
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        if (openSelect !== widget) widget.open();
        const count = select.options.length;
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? count - 1
          : widget.activeIndex + (event.key === 'ArrowDown' ? 1 : -1);
        widget.setActive(next);
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (openSelect === widget) widget.choose(Math.max(0, widget.activeIndex));
        else widget.open();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        closeRoutingSelect(widget, true);
      } else if (event.key === 'Tab') {
        closeRoutingSelect(widget);
      }
    };
    select.addEventListener('change', () => widget.refresh());
    widget.refresh();
    return widget;
  }

  function destroyRoutingSelects(root) {
    root.querySelectorAll('select').forEach(select => select._routingWidget?.destroy());
  }

  function outboundOptions(select, selected, includeVpn = true) {
    select.replaceChildren(
      option('physical', '物理网络', '任何 VPN 都不走'),
    );
    const providers = (routingConfig?.proxy_providers || []).filter(item => item.enabled);
    if (providers.length) {
      select.append(option('proxy', '全部代理订阅', `${providers.length} 个已启用订阅 · 使用全部订阅策略`));
      providers.forEach(provider => {
        select.append(option(`proxy:${provider.id}`, `代理 · ${provider.name}`,
          `${strategyLabel(provider.strategy)} · 独立选点`));
      });
    }
    select.append(option('block', '阻止连接', '直接拒绝，不回退到其他出口'));
    if (includeVpn) {
      (setup?.vpns || []).forEach(vpn => {
        const status = vpn.status === 'Connected' ? '已连接' : '未连接';
        select.append(option(`vpn:${vpn.name}`, vpn.name, `Windows VPN · ${status}`));
      });
    }
    if (![...select.options].some(item => item.value === selected) && selected) {
      select.append(option(selected, '当前出口不可用', '订阅已停用、删除或 VPN 不存在'));
    }
    select.value = selected || 'physical';
  }

  function strategyLabel(value) {
    return ({
      'url-test': '自动测速',
      fallback: '故障转移',
      select: '手动选择',
    })[value] || '自动测速';
  }

  function strategyOptions() {
    return [
      ['url-test', '自动测速', '定时测试延迟并自动选择较快节点'],
      ['fallback', '故障转移', '按订阅顺序使用首个可用节点'],
      ['select', '手动选择', '从运行中的节点列表固定选择'],
    ];
  }

  function downloadRouteOptions() {
    const detected = setup?.system_proxy?.available
      ? `已检测到 ${setup.system_proxy.address}` : '未检测到 Windows 手动系统代理';
    return [
      ['auto', '智能自动更新（推荐）', '依次尝试已有缓存节点、物理网络，并在失败时自动回退到已检测的 Windows 系统代理'],
      ['physical', '物理网络（首次获取）', '固定绕过 Windows 系统代理和 VPN 默认路由'],
      ['system-proxy', 'Windows 系统代理（迁移/恢复）', `${detected}；不作为日常依赖`],
      ['custom-proxy', '自定义本地代理（迁移/恢复）', '临时使用本机 HTTP 代理导入第一份可用节点'],
    ];
  }

  function downloadRouteLabel(provider) {
    return ({
      auto: '智能自动更新',
      physical: '物理网络首次获取',
      'system-proxy': '系统代理恢复',
      'custom-proxy': '本地代理恢复',
    })[provider.download_route || 'auto'];
  }

  function snapshot(value) {
    return JSON.stringify(value);
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value || {}));
  }

  function cacheRecord(providerId) {
    const caches = setup?.provider_caches;
    const raw = Array.isArray(caches)
      ? caches.find(item => item?.id === providerId || item?.provider_id === providerId)
      : caches?.[providerId];
    if (!raw || typeof raw !== 'object') return null;
    const parsed = Number(raw.node_count ?? raw.nodes?.length ?? 0);
    const count = Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
    return { ...raw, available: raw.available == null ? count > 0 : !!raw.available, node_count: count };
  }

  function normalizeProviderDefaults(provider) {
    provider.selection_mode = provider.selection_mode === 'manual' ? 'manual' : 'auto';
    provider.selected_node = String(provider.selected_node || '');
    provider.auto_update = !!provider.auto_update;
    return provider;
  }

  function providerNodesRecord(providerId) {
    const records = setup?.provider_nodes;
    const raw = Array.isArray(records)
      ? records.find(item => item?.id === providerId || item?.provider_id === providerId)
      : records?.[providerId];
    if (!raw) return null;
    const nodes = Array.isArray(raw) ? raw : raw.nodes;
    if (!Array.isArray(nodes)) return null;
    return { nodes, updated_at: Array.isArray(raw) ? '' : raw.updated_at || '' };
  }

  function hydrateProviderNodes() {
    (routingConfig?.proxy_providers || []).forEach(provider => {
      normalizeProviderDefaults(provider);
      const savedProvider = savedConfig?.proxy_providers?.find(item => item.id === provider.id);
      if (savedProvider && providerPreviewSignature(savedProvider) !== providerPreviewSignature(provider)) return;
      const existing = providerPreview(provider);
      if (existing && !existing.persisted) return;
      const record = providerNodesRecord(provider.id);
      if (!record) return;
      providerPreviews.set(provider.id, {
        id: provider.id,
        name: provider.name,
        strategy: provider.strategy || 'url-test',
        selected: provider.selected_node || '',
        nodes: clone(record.nodes),
        alive_count: partitionNodes(record.nodes).selectable.filter(node => node.alive === true).length,
        preview: true,
        persisted: true,
        updated_at: record.updated_at,
        signature: providerPreviewSignature(provider),
      });
    });
  }

  function isMetadataNode(node) {
    const name = String(node?.display_name || node?.name || '').trim();
    if (!name) return false;
    return /(?:剩余|可用|已用|总计|套餐|流量|重置|到期|过期|有效期|官网|网站|公告|通知|客服|群组|QQ群|TG群|Telegram|更新时间|订阅信息)[：:\s]|(?:GB|MB|TB)\s*(?:剩余|可用)|(?:距离|下次).*重置|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}/i.test(name);
  }

  function splitNodeLabel(node) {
    return nodeTools.splitNodeLabel(node);
  }

  function partitionNodes(nodes) {
    const selectable = [];
    const metadata = [];
    (nodes || []).forEach(node => (isMetadataNode(node) ? metadata : selectable).push(node));
    return { selectable, metadata };
  }

  function preferenceNodeName(node, allowNameFallback = false) {
    const displayName = String(node?.display_name || '').trim();
    if (displayName) return displayName;
    return allowNameFallback ? String(node?.name || '').trim() : '';
  }

  function runtimeGroupUsesPreference(group, provider) {
    const selectedNode = String(provider?.selected_node || '').trim();
    if (!group || !selectedNode) return false;
    const runtimeNode = (group.nodes || []).find(item => item.name === group.selected);
    return preferenceNodeName(runtimeNode) === selectedNode
      || group.selected === selectedNode
      || group.selected === `[${provider.name}] ${selectedNode}`;
  }

  function nodeSummary(config = routingConfig, groupId = 'all', includePreviews = true) {
    const providers = (config?.proxy_providers || []).filter(item => item.enabled);
    const providerIds = groupId === 'all' ? new Set(providers.map(item => item.id)) : new Set([groupId]);
    const cached = [...providerIds].reduce((total, id) => total + Number(cacheRecord(id)?.node_count || 0), 0);
    const runtimeGroups = setup?.proxy_groups || [];
    let runtimeGroup = runtimeGroups.find(item => item.id === groupId);
    if (!runtimeGroup && groupId === 'all') runtimeGroup = runtimeGroups.find(item => item.id === 'all');
    const runtimeParts = partitionNodes(runtimeGroup?.nodes || []);
    const previewGroups = includePreviews
      ? [...providerIds].map(id => {
        const provider = config?.proxy_providers?.find(item => item.id === id);
        return provider ? providerPreview(provider) : null;
      }).filter(Boolean)
      : [];
    const previewParts = partitionNodes(previewGroups.flatMap(item => item.nodes || []));
    const runtimeNodes = runtimeParts.selectable.length;
    const previewNodes = previewParts.selectable.length;
    const observedParts = runtimeNodes ? runtimeParts : previewParts;
    const tested = observedParts.selectable.filter(node => node.tested === true ||
      node.alive === true || node.alive === false || Number(node.delay) > 0).length;
    const alive = observedParts.selectable.filter(node => node.alive === true).length;
    return {
      cached,
      preview: previewNodes,
      runtime: runtimeNodes,
      tested,
      alive,
      metadata: runtimeParts.metadata.length + previewParts.metadata.length,
      visible: runtimeNodes || previewNodes || cached,
      source: runtimeNodes ? 'runtime' : previewNodes ? 'preview' : cached ? 'cache' : 'empty',
    };
  }

  function nodeHealthSummary(nodes) {
    const selectable = partitionNodes(nodes || []).selectable;
    const tested = selectable.filter(node => node.tested === true ||
      node.alive === true || node.alive === false || Number(node.delay) > 0).length;
    const alive = selectable.filter(node => node.alive === true).length;
    return tested
      ? `${alive}/${tested} 已测速可用 · 共 ${selectable.length} 个节点`
      : `${selectable.length} 个节点 · 尚未测速`;
  }

  function nodeHealthDetail(node) {
    if (node?.tested !== true && node?.alive !== true && node?.alive !== false && !Number(node?.delay)) {
      return '未检测';
    }
    if (node.alive === true) return node.delay ? `检测通过 · ${node.delay} ms` : '检测通过 · 未返回延迟';
    return '本次检测未通过';
  }

  function providerReady(provider) {
    if (!provider?.enabled || !String(provider.url || '').trim()) return false;
    const preview = providerPreview(provider);
    const runtime = proxyGroupState(provider.id);
    return !!cacheRecord(provider.id)?.available || partitionNodes(preview?.nodes).selectable.length > 0
      || partitionNodes(runtime?.nodes).selectable.length > 0;
  }

  function setDirty(value = true) {
    dirty = value;
    byId('routing-dirty')?.classList.toggle('hidden', !value);
    byId('btn-routing-discard')?.classList.toggle('hidden', !value);
  }

  function syncDirty() {
    if (!routingConfig) return;
    try { setDirty(snapshot(collectConfig()) !== savedSnapshot); }
    catch (_) { setDirty(true); }
  }

  function setRoutingTab(name) {
    if (name === 'nodes' && window.ProxyWorkspace?.openNodes) {
      window.ProxyWorkspace.openNodes(pendingNodeGroup || '', { page: 'routing', tab: activeTab });
      pendingNodeGroup = '';
      return;
    }
    if (name === 'providers' && byId('page-proxy')?.classList.contains('active') &&
        window.ProxyWorkspace?.openSubscriptions) {
      window.ProxyWorkspace.openSubscriptions();
      return;
    }
    activeTab = name;
    document.querySelectorAll('[data-routing-tab]').forEach(button => {
      const selected = button.dataset.routingTab === name;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll('[data-routing-panel]').forEach(panel => {
      const selected = panel.dataset.routingPanel === name;
      panel.classList.toggle('active', selected);
      panel.setAttribute('aria-hidden', String(!selected));
    });
    closeRoutingSelect();
    if (name === 'nodes') renderNodes();
    const main = byId('main-content');
    if (main) main.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function setStatus(status) {
    const badge = byId('routing-state');
    const detail = byId('routing-state-detail');
    const running = !!status?.running;
    const standby = !!status?.standby;
    const enabled = !!status?.configured_enabled;
    const unknown = status?.service_state === 'Unknown';
    badge.className = `pill ${running ? 'on' : unknown ? 'warn' : standby ? 'neutral' : 'off'}`;
    badge.textContent = running ? '代理接管中' : unknown ? '服务状态未知'
      : standby ? '节点核心待机' : enabled ? '配置已启用 / 服务未运行' : '未启用';
    const version = status?.mihomo_version ? `Mihomo ${status.mihomo_version}` : 'Mihomo';
    detail.textContent = running
      ? `${version} · ${status.capture_mode === 'tun' ? 'TUN' : '系统代理'}接管 · ${status.actual_rule_count || 1} 条实际规则 · ${status.provider_count || 0} 个代理订阅 · 物理出口 ${status.physical_interface || '自动'}`
      : standby
        ? `${version} · 不接管系统流量 · 可更新订阅并测试当前已有节点 · ${status.provider_count || 0} 个代理订阅`
      : unknown ? '无法确认 Windows 服务状态；为避免误启停，请刷新后重试。'
        : status?.msg || '统一分流服务未运行';
  }

  function showConflicts(conflicts) {
    const target = byId('routing-conflict');
    if (!conflicts?.length || routingConfig?.capture_mode !== 'tun') {
      target.classList.add('hidden');
      target.textContent = '';
      return;
    }
    const names = [...new Set(conflicts.map(item => item.name).filter(Boolean))].join('、');
    target.textContent = `检测到其他 TUN 正在承载默认路由：${names}。应用前请关闭对应客户端的 TUN 模式。`;
    target.classList.remove('hidden');
  }

  function renderOverview() {
    const providers = (routingConfig?.proxy_providers || []).filter(item => item.enabled);
    const groups = setup?.proxy_groups || [];
    const allGroup = groups.find(item => item.id === 'all');
    const summary = nodeSummary(routingConfig);
    const readyProviders = providers.filter(providerReady);
    const activeRules = (routingConfig?.rules || []).filter(rule => rule.enabled !== false);
    const nodeDetail = summary.source === 'runtime'
      ? `${summary.alive} 个已测速可用 · ${summary.tested} 个已有检测结果`
      : summary.source === 'preview'
        ? `${summary.preview} 个已解析节点 · 尚未测速`
        : summary.source === 'cache'
          ? `${summary.cached} 条缓存记录 · 尚未载入测速`
          : '尚未获取节点';
    const metrics = [
      ['用户规则', String(activeRules.length), `${routingConfig?.rules?.length || 0} 条已添加 · 优先于内置规则`, 'rules'],
      ['可启动订阅', String(readyProviders.length), `${providers.length} 个已启用 · ${routingConfig?.proxy_providers?.length || 0} 个已添加`, 'providers'],
      ['节点摘要', String(summary.visible), nodeDetail, 'nodes'],
      ['当前代理', allGroup?.nodes?.find(item => item.name === allGroup.selected)?.display_name || '未运行', strategyLabel(routingConfig?.proxy_strategy), 'nodes'],
    ];
    const root = byId('routing-overview-metrics');
    root.replaceChildren(...metrics.map(([label, value, detail, target]) => {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'routing-metric';
      card.dataset.target = target;
      card.title = `打开${label}`;
      card.onclick = () => setRoutingTab(target);
      const small = document.createElement('small'); small.textContent = label;
      const strong = document.createElement('strong'); strong.textContent = value;
      const span = document.createElement('span'); span.textContent = detail;
      card.append(small, strong, span);
      return card;
    }));

    const referencedVpns = new Set([routingConfig.default_outbound, ...(routingConfig.rules || [])
      .filter(rule => rule.enabled !== false).map(item => item.outbound)]
      .filter(value => value?.startsWith('vpn:')).map(value => value.slice(4)));
    const vpnRows = new Map((setup?.vpns || []).map(item => [item.name, item]));
    const vpnReady = [...referencedVpns].every(name => {
      const vpn = vpnRows.get(name);
      return vpn && !vpn.ipv4_default_gateway && !vpn.ipv6_default_gateway;
    });
    const proxyRequired = [routingConfig.default_outbound, ...(routingConfig.rules || [])
      .filter(rule => rule.enabled !== false).map(item => item.outbound)]
      .some(value => value === 'proxy' || value?.startsWith('proxy:'));
    const checks = [
      [!!byId('routing-interface')?.value, '物理出口', byId('routing-interface')?.value || '未找到可用物理接口'],
      [routingConfig?.capture_mode !== 'tun' || !(setup?.tun_conflicts || []).length, '接管方式', routingConfig?.capture_mode === 'tun'
        ? ((setup?.tun_conflicts || []).length ? '需关闭其他代理软件的 TUN 模式' : 'TUN 高级接管可用')
        : `Windows 系统代理 → 127.0.0.1:${routingConfig?.mixed_port || 17890}`],
      [vpnReady, 'Windows VPN', referencedVpns.size ? `${referencedVpns.size} 个引用已检查默认网关` : '规则未引用 Windows VPN'],
      [!proxyRequired || readyProviders.length > 0, '代理订阅', !proxyRequired
        ? `${routingConfig?.proxy_providers?.length || 0} 个已添加；当前出口不依赖代理`
        : readyProviders.length
          ? `${providers.length} 个已启用，${readyProviders.length} 个已有缓存、预览或运行节点`
          : `${routingConfig?.proxy_providers?.length || 0} 个已添加，${providers.length} 个已启用，但尚无可启动订阅`],
    ];
    const checkRoot = byId('routing-checks');
    checkRoot.replaceChildren(...checks.map(([ok, label, detail]) => {
      const row = document.createElement('div'); row.className = `routing-check ${ok ? 'ok' : 'warn'}`;
      const icon = document.createElement('i');
      icon.setAttribute('aria-hidden', 'true');
      if (ok) icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="m7.5 12.5 3 3 6.5-7"/></svg>';
      else icon.textContent = '!';
      const copy = document.createElement('div');
      const strong = document.createElement('strong'); strong.textContent = label;
      const span = document.createElement('span'); span.textContent = detail;
      copy.append(strong, span); row.append(icon, copy); return row;
    }));
    byId('routing-tab-rule-count').textContent = String(routingConfig.rules?.length || 0);
    byId('routing-tab-provider-count').textContent = String(routingConfig.proxy_providers?.length || 0);
    byId('routing-tab-node-count').textContent = String(summary.visible);
    const builtin = setup?.builtin_rule_pack?.id === routingConfig?.builtin_rule_pack
      ? setup.builtin_rule_pack : null;
    const builtinCount = builtin?.rule_count ?? ({ off: 0, 'local-direct-v1': 11, 'cn-direct-v1': 42 }[routingConfig?.builtin_rule_pack] || 0);
    const actual = routingConfig?.traffic_mode === 'global' ? 1 : activeRules.length + builtinCount + 1;
    byId('routing-rule-composition').textContent = `用户规则 ${activeRules.length} 条 · 内置规则 ${routingConfig?.traffic_mode === 'global' ? 0 : builtinCount} 条（${routingConfig?.builtin_rule_pack || 'off'}） · 实际规则 ${actual} 条；顺序固定为 user → builtin → MATCH。`;
  }

  function fillForm() {
    byId('routing-enabled').checked = !!routingConfig.enabled;
    byId('routing-capture-mode').value = routingConfig.capture_mode || 'system-proxy';
    byId('routing-traffic-mode').value = routingConfig.traffic_mode || 'rule';
    byId('routing-mixed-port').value = routingConfig.mixed_port || 17890;
    byId('routing-builtin-pack').value = routingConfig.builtin_rule_pack || 'off';
    const interfaces = byId('routing-interface');
    interfaces.replaceChildren();
    (setup.interfaces || []).forEach(item => {
      interfaces.append(option(item.name, item.name, item.description));
    });
    if (!interfaces.options.length) interfaces.append(option('', '未找到物理默认接口'));
    interfaces.value = routingConfig.physical_interface || interfaces.options[0].value;
    enhanceRoutingSelect(interfaces);
    outboundOptions(byId('routing-default'), routingConfig.default_outbound);
    enhanceRoutingSelect(byId('routing-default'));
    const allStrategy = byId('routing-proxy-strategy');
    allStrategy.replaceChildren(...strategyOptions().map(item => option(...item)));
    allStrategy.value = routingConfig.proxy_strategy || 'url-test';
    enhanceRoutingSelect(allStrategy);
    byId('routing-dns').value = (routingConfig.dns_servers || []).join(', ');
    byId('routing-default-dns').value = (routingConfig.default_nameserver || []).join(', ');
    byId('routing-proxy-dns').value = (routingConfig.proxy_server_nameserver || []).join(', ');
    byId('routing-direct-dns').value = (routingConfig.direct_nameserver || []).join(', ');
    renderProviders();
    renderAllProxyChoice();
    renderRules();
    renderOverview();
    renderNodes();
  }

  function makeSelect(values, current, className) {
    const select = document.createElement('select');
    select.className = className;
    values.forEach(([value, label, detail]) => select.append(option(value, label, detail)));
    select.value = current;
    return select;
  }

  function proxyGroupState(id) {
    return (setup?.proxy_groups || []).find(item => item.id === id);
  }

  function providerCache(provider) {
    const listed = cacheRecord(provider.id);
    const state = proxyGroupState(provider.id);
    const raw = providerPreviews.get(provider.id)?.cache || provider.cache || listed
      || state?.cache || state?.cache_status;
    if (!raw || typeof raw !== 'object') return null;
    const parsedCount = Number(raw.node_count ?? raw.nodes?.length ?? 0);
    const nodeCount = Number.isFinite(parsedCount) && parsedCount > 0 ? Math.floor(parsedCount) : 0;
    return {
      available: raw.available == null ? nodeCount > 0 : !!raw.available,
      node_count: nodeCount,
      updated_at: raw.updated_at || '',
      source: raw.source || '',
    };
  }

  function cacheSourceLabel(value) {
    const raw = String(value || '');
    return ({
      subscription: '在线订阅', remote: '在线订阅', refreshed: '在线更新',
      import: '本地 YAML', imported: '本地 YAML', yaml: '本地 YAML',
      cache: '本地缓存', persisted: '本地缓存', migration: '迁移缓存',
    })[raw.toLowerCase()] || raw || '本地缓存';
  }

  function cacheUpdatedLabel(value) {
    if (!value) return '更新时间未知';
    const normalized = typeof value === 'number' && value > 0 && value < 1e12 ? value * 1000 : value;
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return '更新时间未知';
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).format(date);
  }

  function syncProviderCache(providerId, cache) {
    if (!cache || typeof cache !== 'object') return;
    setup.provider_caches = setup.provider_caches && !Array.isArray(setup.provider_caches)
      ? setup.provider_caches : {};
    setup.provider_caches[providerId] = cache;
  }

  function syncProviderNodesFromGroups(providerId, groups, updatedAt = Date.now()) {
    const group = (groups || []).find(item => item.id === providerId);
    if (!group || !Array.isArray(group.nodes)) return;
    const nodes = group.nodes.map(node => {
      const rawName = preferenceNodeName(node, true);
      return rawName ? { ...node, name: rawName, display_name: node.display_name || rawName } : { ...node };
    });
    setup.provider_nodes = setup.provider_nodes && !Array.isArray(setup.provider_nodes)
      ? setup.provider_nodes : {};
    setup.provider_nodes[providerId] = { updated_at: updatedAt, nodes };
    const provider = providerForGroup(providerId);
    const existing = providerPreviews.get(providerId);
    if (provider && (!existing || existing.persisted)) {
      providerPreviews.set(providerId, {
        ...(existing || {}), id: providerId, name: provider.name,
        strategy: provider.strategy || 'url-test', selected: provider.selected_node || '',
        nodes: clone(nodes), preview: true, persisted: true, updated_at: updatedAt,
        alive_count: partitionNodes(nodes).selectable.filter(node => node.alive === true).length,
        signature: providerPreviewSignature(provider),
      });
    }
  }

  function providerPreviewSignature(provider) {
    return snapshot({
      id: provider.id,
      name: provider.name,
      url: provider.url,
      filter: provider.filter || '',
      exclude_filter: provider.exclude_filter || '',
      download_route: provider.download_route || 'auto',
      download_proxy: provider.download_proxy || '',
      user_agent: provider.user_agent || '',
    });
  }

  function providerPreview(provider) {
    const preview = providerPreviews.get(provider.id);
    return preview?.signature === providerPreviewSignature(provider) ? preview : null;
  }

  function providerPreviewError(provider) {
    const error = providerPreviewErrors.get(provider.id);
    return error?.signature === providerPreviewSignature(provider) ? error.message : '';
  }

  function invalidateProviderPreview(provider) {
    providerPreviews.delete(provider.id);
    providerPreviewErrors.delete(provider.id);
  }

  function nodeGroups() {
    const groups = [...(setup?.proxy_groups || [])];
    (routingConfig?.proxy_providers || []).forEach(provider => {
      const preview = providerPreview(provider);
      if (!preview) return;
      const index = groups.findIndex(item => item.id === provider.id);
      if (index >= 0) {
        if (!preview.persisted) groups[index] = preview;
      } else groups.push(preview);
    });
    return groups;
  }

  function openProviderNodes(provider) {
    const state = proxyGroupState(provider.id);
    const preview = providerPreview(provider);
    if ((!state || !providerIsSaved(provider)) && !preview) {
      previewProvider(provider);
      return;
    }
    pendingNodeGroup = provider.id;
    setRoutingTab('nodes');
    if (preview) {
      const tested = partitionNodes(preview.nodes).selectable.filter(node => node.tested).length;
      feedback(tested
        ? `当前显示“${provider.name}”的临时测速结果，不会创建 TUN 或接管流量。`
        : `当前显示“${provider.name}”的独立订阅预览，可直接执行临时测速。`, 'success');
    }
  }

  async function previewProvider(provider) {
    if (busy) return;
    const signature = providerPreviewSignature(provider);
    setBusy(true, 'provider');
    providerPreviewErrors.delete(provider.id);
    renderProviders();
    feedback(`正在获取并解析“${provider.name || '未命名订阅'}”…`);
    try {
      const result = normalizeResult(
        await backend().preview_routing_provider({ ...provider }, {
          physical_interface: byId('routing-interface')?.value || '',
          dns_servers: byId('routing-dns')?.value || '',
        }), '订阅获取成功');
      if (!result.ok) throw new Error(result.msg);
      const currentProvider = routingConfig.proxy_providers?.find(item => item.id === provider.id);
      if (!currentProvider || providerPreviewSignature(currentProvider) !== signature) {
        feedback('订阅在获取过程中已被修改，本次旧结果已忽略。', 'warning');
        return;
      }
      providerPreviews.set(provider.id, {
        id: provider.id,
        name: provider.name,
        strategy: provider.strategy || 'url-test',
        selected: '',
        nodes: result.nodes || [],
        alive_count: 0,
        preview: true,
        checked_at: result.checked_at,
        cache: result.cache || null,
        signature,
      });
      syncProviderCache(provider.id, result.cache);
      pendingNodeGroup = provider.id;
      const route = result.download_route || byId('routing-interface')?.value || '系统默认网络';
      const tone = result.update_state === 'cache_retained' || result.warning ? 'warning' : 'success';
      feedback(`${result.msg}；数据来源“${route}”，不会创建 TUN 或接管流量。`, tone);
      nodeFeedback(`${provider.name}：已解析 ${partitionNodes(result.nodes).selectable.length} 个可选节点，尚未测速。`, tone);
      toast({ ok: true, tone, msg: result.msg });
      setRoutingTab('nodes');
    } catch (error) {
      const message = friendlyError(error);
      providerPreviews.delete(provider.id);
      providerPreviewErrors.set(provider.id, { signature, message });
      feedback(`订阅获取失败：${message}`, 'error');
      nodeFeedback(`订阅获取失败：${message}`, 'error');
      toast({ ok: false, msg: message });
    } finally {
      setBusy(false);
      renderProviders();
      renderNodes();
      renderOverview();
    }
  }

  async function importProviderYaml(provider) {
    if (busy) return;
    const picker = document.createElement('input');
    picker.type = 'file';
    picker.accept = '.yaml,.yml,application/yaml,text/yaml,text/plain';
    picker.onchange = async () => {
      const file = picker.files?.[0];
      if (!file) return;
      if (file.size > 10 * 1024 * 1024) {
        feedback('导入失败：YAML 文件不能超过 10 MB。', 'error');
        toast({ ok: false, msg: 'YAML 文件不能超过 10 MB' });
        return;
      }
      const signature = providerPreviewSignature(provider);
      setBusy(true, 'provider');
      providerPreviewErrors.delete(provider.id);
      renderProviders();
      feedback(`正在导入“${provider.name || '未命名订阅'}”的本地 YAML…`);
      try {
        const content = await file.text();
        const result = normalizeResult(
          await backend().import_routing_provider({ ...provider }, content, {
            physical_interface: byId('routing-interface')?.value || '',
            dns_servers: byId('routing-dns')?.value || '',
          }),
          'YAML 节点已导入');
        if (!result.ok) throw new Error(result.msg);
        const currentProvider = routingConfig.proxy_providers?.find(item => item.id === provider.id);
        if (!currentProvider || providerPreviewSignature(currentProvider) !== signature) {
          feedback('订阅在导入过程中已被修改，本次旧结果已忽略。', 'warning');
          return;
        }
        providerPreviews.set(provider.id, {
          id: provider.id,
          name: provider.name,
          strategy: provider.strategy || 'url-test',
          selected: '',
          nodes: result.nodes || [],
          alive_count: 0,
          preview: true,
          imported: true,
          checked_at: result.checked_at,
          cache: result.cache || null,
          signature,
        });
        syncProviderCache(provider.id, result.cache);
        pendingNodeGroup = provider.id;
        feedback(`${result.msg}；节点已保存到本软件缓存，不依赖 Clash Verge。`, 'success');
        nodeFeedback(`${provider.name}：已导入 ${partitionNodes(result.nodes).selectable.length} 个可选节点，尚未测速。`, 'success');
        toast({ ok: true, msg: result.msg });
        setRoutingTab('nodes');
      } catch (error) {
        const message = friendlyError(error);
        providerPreviewErrors.set(provider.id, { signature, message });
        feedback(`YAML 导入失败：${message}`, 'error');
        nodeFeedback(`YAML 导入失败：${message}`, 'error');
        toast({ ok: false, msg: message });
      } finally {
        setBusy(false);
        renderProviders();
        renderNodes();
        renderOverview();
      }
    };
    picker.click();
  }

  function runtimeChoice(groupId, strategy, label) {
    const box = document.createElement('div');
    box.className = 'routing-runtime-inner';
    const state = proxyGroupState(groupId);
    if (!state || state.strategy !== strategy) {
      box.classList.add('muted');
      box.textContent = strategy === 'select'
        ? '保存并启动分流后，可在这里选择实际节点。'
        : '保存并启动分流后显示当前节点和健康状态。';
      return box;
    }
    const selected = state.nodes?.find(item => item.name === state.selected);
    if (strategy !== 'select') {
      const delay = selected?.delay ? ` · ${selected.delay} ms` : '';
      box.textContent = `当前节点：${selected?.display_name || state.selected || '等待检测'}${delay} · 共 ${partitionNodes(state.nodes).selectable.length} 个可选节点`;
      return box;
    }
    const field = document.createElement('div');
    field.className = 'field grow';
    const nodeLabel = document.createElement('label');
    nodeLabel.textContent = `${label}当前节点`;
    const select = makeSelect(partitionNodes(state.nodes).selectable.map(node => [
      node.name,
      node.display_name,
      nodeHealthDetail(node),
    ]), state.selected, 'routing-node-select');
    select.setAttribute('aria-label', `${label}当前节点`);
    select.onchange = () => {
      const provider = providerForGroup(groupId);
      if (provider) saveProxyPreference(groupId, 'manual', select.value);
      else switchProxyNode(groupId, select.value);
    };
    field.append(nodeLabel, select);
    box.append(field);
    enhanceRoutingSelect(select, { compact: true });
    return box;
  }

  function renderAllProxyChoice() {
    const root = byId('routing-all-node');
    destroyRoutingSelects(root);
    root.replaceChildren();
    const hasProviders = (routingConfig.proxy_providers || []).some(item => item.enabled);
    root.classList.toggle('hidden', !hasProviders);
    if (!hasProviders) return;
    root.append(runtimeChoice('all', routingConfig.proxy_strategy || 'url-test', '全部订阅'));
  }

  async function switchProxyNode(groupId, nodeName) {
    if (busy || !nodeName) return;
    setBusy(true);
    feedback('正在切换代理节点…'); nodeFeedback('正在切换代理节点…');
    try {
      const result = normalizeResult(
        await backend().select_routing_proxy(groupId, nodeName), '代理节点已切换');
      if (!result.ok) throw new Error(result.msg);
      setup.proxy_groups = result.groups || [];
      renderProviders();
      renderAllProxyChoice();
      renderOverview();
      renderNodes();
      feedback(result.msg, 'success'); nodeFeedback(result.msg, 'success');
      toast({ ok: true, msg: result.msg });
    } catch (error) {
      feedback(`节点切换失败：${friendlyError(error)}`, 'error');
      nodeFeedback(`节点切换失败：${friendlyError(error)}`, 'error');
      toast({ ok: false, msg: friendlyError(error) });
    } finally {
      setBusy(false);
    }
  }

  async function refreshProvider(providerId) {
    if (busy) return;
    setBusy(true);
    feedback('正在更新代理订阅…');
    try {
      const result = normalizeResult(await backend().refresh_routing_provider(providerId), '代理订阅已更新');
      if (!result.ok) throw new Error(result.msg);
      setup.proxy_groups = result.groups || [];
      syncProviderCache(providerId, result.cache);
      syncProviderNodesFromGroups(providerId, setup.proxy_groups, result.updated_at || Date.now());
      renderNodeWorkspace();
      const message = result.selection_invalid
        ? `${result.msg || '代理订阅已更新'}；原节点失效需重选。`
        : result.msg;
      const tone = result.selection_invalid || result.warning ? 'warning' : 'success';
      feedback(message, tone); nodeFeedback(message, tone);
      toast({ ok: true, tone, msg: message });
    } catch (error) {
      feedback(`更新失败：${friendlyError(error)}`, 'error');
      toast({ ok: false, msg: friendlyError(error) });
    } finally { setBusy(false); }
  }

  function testIsActive(job) {
    return job?.status === 'pending' || job?.status === 'running';
  }

  function testContext(groupId) {
    return routingTestJobs.get(groupId) || null;
  }

  function providerForGroup(groupId) {
    return routingConfig?.proxy_providers?.find(item => item.id === groupId) || null;
  }

  function mergeTestNodes(existing, incoming) {
    const updates = new Map((incoming || []).map(node => [node.name, node]));
    const merged = (existing || []).map(node => updates.has(node.name)
      ? { ...node, ...updates.get(node.name) } : node);
    const known = new Set((existing || []).map(node => node.name));
    (incoming || []).forEach(node => { if (!known.has(node.name)) merged.push({ ...node }); });
    return merged;
  }

  function replaceRuntimeTestGroup(groupId, source) {
    setup.proxy_groups = setup.proxy_groups || [];
    const index = setup.proxy_groups.findIndex(item => item.id === groupId);
    if (index >= 0) setup.proxy_groups[index] = clone(source);
    else setup.proxy_groups.push(clone(source));
  }

  function applyTestJob(context, job) {
    context.job = clone(job);
    const provider = context.providerId ? providerForGroup(context.providerId) : null;
    if (context.kind === 'preview' && !provider) return false;
    if (provider && context.signature !== providerPreviewSignature(provider)) return false;
    if (context.kind === 'preview') {
      const current = providerPreview(provider) || context.previousGroup;
      if (!current) return false;
      const nodes = mergeTestNodes(current.nodes, job.nodes);
      providerPreviews.set(context.groupId, {
        ...current,
        selected: provider?.selected_node || current.selected || '',
        nodes,
        alive_count: nodes.filter(node => !isMetadataNode(node) && node.alive === true).length,
        tested_at: job.status === 'completed' ? Date.now() : current.tested_at,
        signature: context.signature,
      });
      if (job.status === 'completed') {
        setup.provider_nodes = setup.provider_nodes && !Array.isArray(setup.provider_nodes)
          ? setup.provider_nodes : {};
        setup.provider_nodes[context.groupId] = { nodes: clone(nodes), updated_at: Date.now() };
      }
    } else {
      const current = nodeGroups().find(item => item.id === context.groupId) || context.previousGroup;
      if (current) replaceRuntimeTestGroup(context.groupId, {
        ...current,
        nodes: mergeTestNodes(current.nodes, job.nodes),
        alive_count: Number(job.alive || 0),
      });
    }
    return true;
  }

  function reapplyTestJobs() {
    routingTestJobs.forEach(context => {
      if (context?.job && context.job.status !== 'error') applyTestJob(context, context.job);
    });
  }

  function restoreTestResult(context) {
    if (!context.previousGroup) return;
    if (context.kind === 'preview') {
      providerPreviews.set(context.groupId, clone(context.previousGroup));
    } else {
      replaceRuntimeTestGroup(context.groupId, context.previousGroup);
    }
  }

  function renderTestProgress(groupId) {
    const root = byId('routing-test-progress');
    if (!root) return;
    const context = testContext(groupId);
    const job = context?.job;
    root.classList.toggle('hidden', !job);
    root.classList.remove('completed', 'cancelled', 'error');
    if (!job) return;
    if (!testIsActive(job)) root.classList.add(job.status);
    root.setAttribute('aria-busy', String(testIsActive(job)));
    const total = Math.max(0, Number(job.total || job.nodes?.length || 0));
    const completed = Math.min(total, Math.max(0, Number(job.completed || 0)));
    const statusLabels = {
      pending: '等待测速引擎启动', running: '正在逐个更新节点延迟',
      completed: '测速已完成', cancelled: '测速已停止', error: '测速失败，已保留上次结果',
    };
    byId('routing-test-progress-title').textContent = `${context.groupName || '当前代理组'} · 节点测速`;
    byId('routing-test-progress-message').textContent = job.error || job.msg || statusLabels[job.status] || '正在处理';
    const progress = byId('routing-test-progress-bar');
    progress.max = Math.max(1, total); progress.value = completed;
    progress.textContent = `${total ? Math.round(completed / total * 100) : 0}%`;
    byId('routing-test-progress-count').textContent = `${completed} / ${total}`;
    byId('routing-test-progress-alive').textContent = String(Number(job.alive || 0));
    byId('routing-test-progress-failed').textContent = String(Number(job.failed || 0));
    const cancel = byId('btn-routing-test-cancel');
    cancel.classList.toggle('hidden', !testIsActive(job));
    cancel.disabled = !!context.cancelling || !context.jobId;
    cancel.textContent = context.cancelling ? '正在停止…' : context.jobId ? '停止测速' : '正在启动…';
  }

  function renderNodeWorkspace() {
    renderNodes(); renderProviders(); renderOverview(); renderAllProxyChoice();
    window.ProxyWorkspace?.sync?.();
  }

  function finishTestJob(context, job) {
    if (context.timer) clearTimeout(context.timer);
    context.timer = null;
    if (job.status === 'error') restoreTestResult(context);
    context.job = clone(job);
    renderNodeWorkspace();
    const message = job.status === 'completed'
      ? `测速完成：${job.alive || 0}/${job.total || 0} 个节点可用。`
      : job.status === 'cancelled' ? `测速已停止，已完成 ${job.completed || 0}/${job.total || 0}。`
        : `测速失败：${job.error || job.msg || '未知错误'}，已保留上次结果。`;
    const tone = job.status === 'completed' ? 'success' : job.status === 'cancelled' ? 'warning' : 'error';
    feedback(message, tone); nodeFeedback(message, tone);
    if (job.status !== 'cancelled') toast({ ok: job.status === 'completed', msg: message });
  }

  function scheduleTestPoll(context, delay = TEST_POLL_MS) {
    if (!testIsActive(context.job)) return;
    if (context.timer) clearTimeout(context.timer);
    context.timer = setTimeout(() => pollTestJob(context), delay);
  }

  async function pollTestJob(context) {
    if (routingTestJobs.get(context.groupId) !== context || context.polling) return;
    const provider = context.providerId ? providerForGroup(context.providerId) : null;
    if (context.providerId && (!provider || context.signature !== providerPreviewSignature(provider))) {
      context.job = { ...context.job, status: 'cancelled', msg: '订阅已修改，旧测速任务结果已忽略。' };
      try { await backend().cancel_routing_test_job(context.jobId); } catch (_) { /* 忽略取消竞态 */ }
      finishTestJob(context, context.job);
      return;
    }
    context.polling = true;
    try {
      const result = await backend().get_routing_test_job(context.jobId);
      if (result?.ok === false || !result?.job) throw new Error(result?.msg || '读取测速进度失败');
      context.pollFailures = 0;
      if (!applyTestJob(context, result.job)) return;
      renderNodeWorkspace();
      if (testIsActive(result.job)) scheduleTestPoll(context);
      else finishTestJob(context, result.job);
    } catch (error) {
      context.pollFailures = Number(context.pollFailures || 0) + 1;
      if (context.pollFailures < 8) {
        context.job = { ...context.job, msg: '测速仍在继续，正在重新读取进度…' };
        const retryDelay = Math.min(
          TEST_POLL_MAX_MS, TEST_POLL_MS * (2 ** context.pollFailures));
        renderNodeWorkspace(); scheduleTestPoll(context, retryDelay);
      } else {
        context.job = {
          ...context.job,
          msg: '持续无法读取测速进度，正在请求后台停止任务…',
        };
        renderNodeWorkspace();
        try {
          const cancelled = await backend().cancel_routing_test_job(context.jobId);
          if (cancelled?.ok === false || !cancelled?.job) {
            throw new Error(cancelled?.msg || friendlyError(error));
          }
          applyTestJob(context, cancelled.job);
          if (testIsActive(cancelled.job)) scheduleTestPoll(context, TEST_POLL_MAX_MS);
          else finishTestJob(context, cancelled.job);
        } catch (_) {
          context.job = {
            ...context.job,
            msg: '测速任务状态暂时未知，后台可能仍在运行；将继续读取进度。',
          };
          renderNodeWorkspace();
          scheduleTestPoll(context, TEST_POLL_MAX_MS);
        }
      }
    } finally {
      context.polling = false;
    }
  }

  function renderNodes() {
    const groupSelect = byId('routing-node-group');
    if (!groupSelect || !routingConfig) return;
    const groups = nodeGroups();
    const requestedProvider = pendingNodeGroup
      ? routingConfig.proxy_providers?.find(item => item.id === pendingNodeGroup)
      : null;
    const previous = pendingNodeGroup || groupSelect.value || 'all';
    groupSelect.replaceChildren(...groups.map(group => {
      const parts = partitionNodes(group.nodes);
      return option(group.id, group.name, group.preview
        ? `订阅预览 · 已解析 ${parts.selectable.length} 个可选节点`
        : `${strategyLabel(group.strategy)} · ${nodeHealthSummary(parts.selectable)}`);
    }));
    if (requestedProvider && !groups.some(item => item.id === requestedProvider.id)) {
      groupSelect.append(option(requestedProvider.id, requestedProvider.name, '尚未获取 · 可在订阅页独立预览'));
    }
    if (!groupSelect.options.length) groupSelect.append(option('', '尚无节点', '先到订阅管理获取节点，或启动统一分流'));
    groupSelect.value = [...groupSelect.options].some(item => item.value === previous)
      ? previous : (groups[0]?.id || '');
    groupSelect.disabled = busy || !groups.length;
    pendingNodeGroup = '';
    enhanceRoutingSelect(groupSelect, { compact: true });
    const group = groups.find(item => item.id === groupSelect.value);
    if ((group?.id || '') !== renderedNodeGroup) {
      nodeRegionFilter = 'all';
      renderedNodeGroup = group?.id || '';
    }
    const provider = providerForGroup(group?.id);
    const context = testContext(group?.id);
    const activeTest = testIsActive(context?.job) || testStartBusy.has(group?.id);
    byId('routing-node-toolbar')?.classList.toggle('hidden', !group);
    const term = (byId('routing-node-search')?.value || '').trim().toLowerCase();
    const aliveCheckbox = byId('routing-node-alive');
    const wasPreviewTested = !!group?.preview && partitionNodes(group.nodes).selectable.some(node => node.tested === true);
    if (group?.preview && !wasPreviewTested) aliveCheckbox.checked = false;
    const aliveOnly = !!aliveCheckbox?.checked;
    const parts = partitionNodes(group?.nodes || []);
    const previewTested = !!group?.preview && parts.selectable.some(node => node.tested === true);
    const regionOptions = nodeTools.regionOptions(parts.selectable);
    if (!regionOptions.some(item => item.code === nodeRegionFilter)) nodeRegionFilter = 'all';
    const regionRoot = byId('routing-node-regions');
    regionRoot?.replaceChildren(...regionOptions.map(region => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'routing-region-chip';
      button.dataset.region = region.code;
      button.classList.toggle('active', region.code === nodeRegionFilter);
      button.setAttribute('aria-pressed', String(region.code === nodeRegionFilter));
      button.setAttribute('aria-label', `${region.label}，${region.count} 个节点`);
      const label = document.createElement('span'); label.textContent = region.label;
      const count = document.createElement('small'); count.textContent = String(region.count);
      button.append(label, count);
      button.onclick = () => { nodeRegionFilter = region.code; renderNodes(); };
      return button;
    }));
    const nodeSelection = node => {
      const preferenceName = preferenceNodeName(node, !!group?.preview);
      const manualSelected = provider?.selection_mode === 'manual' && provider.selected_node === preferenceName;
      const runtimeSelected = !!runtimeStatus?.running && (group?.selected === node.name
        || proxyGroupState(provider?.id)?.selected === node.name);
      return { preferenceName, manualSelected, runtimeSelected, selected: manualSelected || runtimeSelected };
    };
    const selectedNames = parts.selectable.filter(node => nodeSelection(node).selected)
      .flatMap(node => [node.name, node.display_name]);
    const sortMode = byId('routing-node-sort')?.value || 'default';
    const nodes = nodeTools.sortNodes(parts.selectable.filter(node => (!aliveOnly || node.alive === true) &&
      (nodeRegionFilter === 'all' || nodeTools.regionCode(node) === nodeRegionFilter) &&
      (!term || `${node.display_name || node.name} ${node.type}`.toLowerCase().includes(term))), sortMode, selectedNames);
    const metadata = parts.metadata.filter(node => !term
      || `${node.display_name || node.name} ${node.type}`.toLowerCase().includes(term));
    const grid = byId('routing-node-grid');
    grid.replaceChildren(...nodes.map(node => {
      const card = document.createElement('article');
      const { preferenceName, manualSelected, runtimeSelected, selected } = nodeSelection(node);
      const nodeState = node.state || (node.tested ? 'completed' : activeTest ? 'pending' : '');
      card.className = `routing-node${selected ? ' selected' : ''}${nodeState ? ` ${nodeState}` : ''}${node.tested && node.alive === false ? ' offline' : ''}`;
      if (selected) card.setAttribute('aria-current', 'true');
      const head = document.createElement('div');
      const identity = document.createElement('span'); identity.className = 'routing-node-identity';
      const label = splitNodeLabel(node);
      if (label.country) {
        const country = document.createElement('i'); country.className = 'routing-country-badge';
        country.textContent = label.country; country.setAttribute('aria-label', `国家或地区 ${label.country}`);
        identity.append(country);
      }
      const name = document.createElement('strong'); name.textContent = label.text;
      identity.append(name);
      const delay = document.createElement('span');
      delay.className = `delay${nodeState === 'testing' ? ' testing' : node.tested && node.alive === false ? ' bad' : ''}`;
      delay.textContent = nodeState === 'testing' ? '测速中…' : nodeState === 'pending' && activeTest ? '等待测速'
        : !node.tested ? '未测速' : node.delay ? `${node.delay} ms`
        : node.alive === true ? '可用 / 无延迟' : node.alive === false ? '不可用' : '未测速';
      head.append(identity, delay);
      const meta = document.createElement('small');
      meta.textContent = `${node.type || '代理节点'}${group.preview ? ' · 已持久化节点' : ''}`;
      const action = document.createElement('div'); action.className = 'routing-node-action';
      const badge = document.createElement('span');
      badge.textContent = runtimeSelected ? '当前使用' : manualSelected ? '待启用' : provider?.selection_mode === 'auto' ? '自动模式候选' : '可设为目标节点';
      badge.classList.toggle('on', runtimeSelected || manualSelected);
      const choose = document.createElement('button'); choose.type = 'button'; choose.className = 'btn mini ghost';
      choose.textContent = !provider ? '先选择订阅' : manualSelected ? '已选择' : '使用此节点';
      choose.disabled = busy || !provider || !preferenceName || preferenceBusy === provider.id || manualSelected;
      choose.title = !provider ? '请先选择具体订阅，聚合代理组不能保存单一节点偏好'
        : !preferenceName ? '运行节点缺少 display_name，无法安全保存原始节点名'
        : group.preview ? '保存为待启用节点，不会立即连接' : '保存为该订阅的手动节点偏好';
      choose.onclick = () => saveProxyPreference(provider.id, 'manual', preferenceName);
      action.append(badge, choose); card.append(head, meta, action);
      return card;
    }));
    const metadataRoot = byId('routing-node-metadata');
    if (metadataRoot) {
      metadataRoot.classList.toggle('hidden', metadata.length === 0);
      metadataRoot.replaceChildren(...metadata.map(node => {
        const item = document.createElement('div'); item.className = 'routing-node-meta-item';
        const label = splitNodeLabel(node);
        const strong = document.createElement('strong'); strong.textContent = label.text;
        const small = document.createElement('small'); small.textContent = '订阅信息 · 不参与测速、选点和节点计数';
        item.append(strong, small); return item;
      }));
    }
    const empty = byId('routing-nodes-empty');
    empty.classList.toggle('hidden', nodes.length > 0);
    if (!nodes.length) {
      const message = !group
        ? '尚无节点，可返回订阅管理点击“获取节点”。'
        : term ? '没有匹配当前搜索条件的节点。'
          : nodeRegionFilter !== 'all' ? '当前地区筛选没有匹配节点，可切换到“全部”。'
          : aliveOnly ? '当前代理组没有可用节点，可取消“仅显示可用”或重新测速。'
            : group.preview ? '订阅可以访问，但当前筛选条件下没有解析出节点。'
              : '运行中的代理组尚未加载节点，可刷新列表或更新订阅。';
      empty.replaceChildren();
      const copy = document.createElement('p'); copy.textContent = message;
      empty.append(copy);
      if (!group) {
        const action = document.createElement('button');
        action.type = 'button'; action.className = 'btn ghost'; action.textContent = '去订阅管理';
        action.onclick = () => setRoutingTab('providers');
        empty.append(action);
      }
    }
    byId('routing-node-summary').textContent = group
      ? group.preview
        ? previewTested
          ? `${group.name} · 临时测速 ${parts.selectable.filter(node => node.alive === true).length}/${parts.selectable.length} 可用 · 未启用 TUN${parts.metadata.length ? ` · ${parts.metadata.length} 条订阅信息已排除` : ''}${term || aliveOnly || nodeRegionFilter !== 'all' ? ` · 当前显示 ${nodes.length}` : ''}`
          : `${group.name} · 已解析 ${parts.selectable.length} 个可选节点 · 等待临时测速${parts.metadata.length ? ` · ${parts.metadata.length} 条订阅信息已单独展示` : ''}${term || nodeRegionFilter !== 'all' ? ` · 当前显示 ${nodes.length}` : ''}`
        : `${group.name} · ${strategyLabel(group.strategy)} · ${nodeHealthSummary(parts.selectable)}${parts.metadata.length ? ` · ${parts.metadata.length} 条订阅信息已排除` : ''}${term || aliveOnly || nodeRegionFilter !== 'all' ? ` · 当前显示 ${nodes.length}` : ''}`
      : requestedProvider
        ? `订阅“${requestedProvider.name}”尚未获取节点，可返回订阅管理直接获取，无需启动统一分流。`
        : '尚无节点。可在订阅管理中独立获取节点，无需启动统一分流。';
    if (group && !provider) {
      byId('routing-node-summary').textContent += ' · 如需固定节点，请先在上方选择一个具体订阅。';
    }
    const selectionInvalid = provider?.selection_mode === 'manual' && !!provider.selected_node &&
      parts.selectable.length > 0 && !parts.selectable.some(node =>
        preferenceNodeName(node, !!group?.preview) === provider.selected_node);
    if (selectionInvalid) {
      byId('routing-node-summary').textContent += ` · 原节点“${provider.selected_node}”已失效，请重新选择。`;
    }
    const groupTest = byId('btn-routing-group-test');
    groupTest.disabled = busy || !group || activeTest;
    groupTest.textContent = activeTest ? '测速中…'
      : context?.job || previewTested ? '重新测速' : group?.preview ? '临时全部测速' : '全部测速';
    groupTest.title = group?.preview ? '后台启动无 TUN 的 Mihomo，逐节点回显延迟且不接管系统流量' : '后台测试当前运行代理组的全部节点';
    const autoSelect = byId('btn-routing-auto-select');
    autoSelect.disabled = busy || !provider || preferenceBusy === provider?.id;
    autoSelect.textContent = provider?.selection_mode === 'auto' ? '已启用自动优选' : '自动优选';
    autoSelect.title = !provider ? '请选择一个具体订阅后设置自动优选' : '由订阅策略自动选择较优节点';
    byId('routing-node-alive').disabled = busy || (!!group?.preview && !previewTested && !context?.job);
    const locateCurrent = byId('btn-routing-locate-current');
    locateCurrent.disabled = busy || !parts.selectable.some(node => nodeSelection(node).selected);
    locateCurrent.title = locateCurrent.disabled ? '当前代理组没有已选择或正在使用的节点' : '清除节点筛选并滚动到当前节点';
    byId('btn-routing-nodes-refresh').textContent = group?.preview ? '重新获取订阅' : '刷新列表';
    renderTestProgress(group?.id || '');
  }

  function locateCurrentNode() {
    const group = nodeGroups().find(item => item.id === byId('routing-node-group')?.value);
    const provider = providerForGroup(group?.id);
    const selected = partitionNodes(group?.nodes || []).selectable.find(node => {
      const preferenceName = preferenceNodeName(node, !!group?.preview);
      const manualSelected = provider?.selection_mode === 'manual' && provider.selected_node === preferenceName;
      const runtimeSelected = !!runtimeStatus?.running && (group?.selected === node.name
        || proxyGroupState(provider?.id)?.selected === node.name);
      return manualSelected || runtimeSelected;
    });
    if (!selected) {
      nodeFeedback('当前代理组没有可定位的已选择节点。', 'warning');
      return;
    }
    byId('routing-node-search').value = '';
    byId('routing-node-alive').checked = false;
    nodeRegionFilter = 'all';
    byId('routing-node-sort').value = 'current';
    byId('routing-node-sort')._routingWidget?.refresh();
    renderNodes();
    requestAnimationFrame(() => {
      const card = byId('routing-node-grid')?.querySelector('.routing-node.selected');
      if (!card) return;
      const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
      card.scrollIntoView({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' });
      card.tabIndex = -1;
      card.focus({ preventScroll: true });
      card.classList.add('located');
      window.setTimeout(() => card.classList.remove('located'), 1600);
      nodeFeedback(`已定位当前节点：${nodeTools.nodeText(selected)}`, 'success');
    });
  }

  async function testProxyGroup() {
    const groupId = byId('routing-node-group').value;
    if (!groupId || busy || testStartBusy.has(groupId) || testIsActive(testContext(groupId)?.job)) return;
    const group = nodeGroups().find(item => item.id === groupId);
    const provider = group?.preview
      ? routingConfig.proxy_providers?.find(item => item.id === groupId) : null;
    if (group?.preview && !provider) {
      nodeFeedback('当前订阅已不存在，请返回订阅管理后刷新。', 'error');
      return;
    }
    const signature = provider ? providerPreviewSignature(provider) : '';
    const progress = group?.preview
      ? '正在启动无 TUN 后台测速任务，节点完成后会立即回显…'
      : '正在启动后台测速任务，节点完成后会立即回显…';
    const previousContext = testContext(groupId);
    const context = {
      jobId: '', groupId, providerId: provider?.id || '', groupName: group.name,
      kind: group.preview ? 'preview' : 'runtime', signature, previousGroup: clone(group),
      job: {
        id: '', status: 'pending', total: partitionNodes(group.nodes).selectable.length,
        completed: 0, alive: 0, failed: 0, nodes: [], msg: progress, error: '',
      },
      timer: null, pollFailures: 0,
    };
    routingTestJobs.set(groupId, context);
    testStartBusy.add(groupId); feedback(progress); nodeFeedback(progress); renderNodes();
    try {
      const result = group?.preview
        ? await backend().start_preview_routing_test({ ...provider }, {
          physical_interface: byId('routing-interface')?.value || '',
          dns_servers: byId('routing-dns')?.value || '',
        })
        : await backend().start_routing_group_test(groupId);
      if (result?.ok === false || !result?.job_id || !result?.job) throw new Error(result?.msg || '测速任务启动失败');
      context.jobId = result.job_id;
      context.job = clone(result.job);
      applyTestJob(context, result.job);
      renderNodeWorkspace();
      if (testIsActive(result.job)) scheduleTestPoll(context);
      else finishTestJob(context, result.job);
    } catch (error) {
      if (previousContext) routingTestJobs.set(groupId, previousContext);
      else routingTestJobs.delete(groupId);
      feedback(`测速失败：${friendlyError(error)}`, 'error');
      nodeFeedback(`测速失败：${friendlyError(error)}`, 'error');
      toast({ ok: false, msg: friendlyError(error) });
    } finally { testStartBusy.delete(groupId); renderNodes(); }
  }

  async function cancelProxyTest() {
    const groupId = byId('routing-node-group')?.value || '';
    const context = testContext(groupId);
    if (!context || !testIsActive(context.job) || context.cancelling) return;
    context.cancelling = true; renderTestProgress(groupId);
    try {
      const result = await backend().cancel_routing_test_job(context.jobId);
      if (result?.ok === false || !result?.job) throw new Error(result?.msg || '停止测速失败');
      applyTestJob(context, result.job);
      context.cancelling = false;
      if (testIsActive(result.job)) scheduleTestPoll(context);
      else finishTestJob(context, result.job);
    } catch (error) {
      context.cancelling = false;
      nodeFeedback(`停止测速失败：${friendlyError(error)}`, 'error');
      renderTestProgress(groupId);
    }
  }

  function mergePreferenceIntoConfig(config, authoritative, providerId, mode, nodeName) {
    if (!config) return;
    config.proxy_providers = config.proxy_providers || [];
    let target = config.proxy_providers.find(item => item.id === providerId);
    const source = authoritative?.proxy_providers?.find(item => item.id === providerId);
    if (!target && source) {
      target = clone(source);
      config.proxy_providers.push(target);
    }
    if (target) {
      if (source) target.enabled = !!source.enabled;
      target.selection_mode = source?.selection_mode === 'manual' ? 'manual' : mode;
      target.selected_node = String(source?.selected_node ?? (mode === 'manual' ? nodeName : ''));
      target.auto_update = source ? !!source.auto_update : !!target.auto_update;
    }
    if (authoritative?.default_outbound) config.default_outbound = authoritative.default_outbound;
  }

  async function saveProxyPreference(providerId, mode, nodeName = '') {
    const provider = providerForGroup(providerId);
    if (!provider || preferenceBusy) return;
    if (mode === 'manual') {
      const confirmed = await confirmAction({
        title: '确认目标节点',
        message: `将“${nodeName}”保存为“${provider.name}”的手动目标。运行中会回读确认实际选择；该操作不会修改默认出口。`,
        confirmText: '确认使用',
      });
      if (!confirmed) return;
    }
    preferenceBusy = providerId; renderNodes(); renderProviders();
    const action = mode === 'manual' ? `正在保存目标节点“${nodeName}”…` : '正在启用自动优选…';
    nodeFeedback(action); feedback(action);
    try {
      const result = await backend().save_proxy_preference(
        providerId, mode, nodeName, { ...provider });
      if (result?.ok === false || !result?.config) throw new Error(result?.msg || '代理偏好保存失败');
      mergePreferenceIntoConfig(routingConfig, result.config, providerId, mode, nodeName);
      mergePreferenceIntoConfig(appliedConfig, result.config, providerId, mode, nodeName);
      mergePreferenceIntoConfig(savedConfig, result.config, providerId, mode, nodeName);
      setup.config = clone(result.config);
      const defaultSelect = byId('routing-default');
      if (result.config.default_outbound && defaultSelect &&
          [...defaultSelect.options].some(item => item.value === result.config.default_outbound)) {
        defaultSelect.value = result.config.default_outbound;
        defaultSelect._routingWidget?.refresh();
      }
      if (typeof CFG !== 'undefined') CFG.routing = clone(result.config);
      if (result.status) {
        setup.status = clone(result.status);
        runtimeStatus = clone(result.status);
        setStatus(runtimeStatus);
      }
      const preview = providerPreview(provider);
      if (preview) {
        providerPreviews.set(providerId, {
          ...preview,
          selected: mode === 'manual' ? nodeName : preview.selected,
          signature: providerPreviewSignature(provider),
        });
      }
      savedSnapshot = snapshot(savedConfig || collectConfig());
      syncDirty();
      const message = result.msg || (mode === 'manual'
        ? (result.requires_apply ? '目标节点已保存，开启代理后生效。' : '目标节点已保存并生效。')
        : (result.requires_apply ? '自动优选已保存，开启代理后生效。' : '已切换为自动优选。'));
      nodeFeedback(message, result.requires_apply ? 'warning' : 'success');
      feedback(message, result.requires_apply ? 'warning' : 'success');
      toast({ ok: true, msg: message });
    } catch (error) {
      const message = `保存代理偏好失败：${friendlyError(error)}`;
      nodeFeedback(message, 'error'); feedback(message, 'error'); toast({ ok: false, msg: message });
    } finally {
      preferenceBusy = '';
      renderNodeWorkspace();
    }
  }

  async function refreshNodes() {
    if (busy) return;
    const current = nodeGroups().find(item => item.id === byId('routing-node-group').value);
    if (current?.preview) {
      const provider = routingConfig.proxy_providers?.find(item => item.id === current.id);
      if (provider) await previewProvider(provider);
      return;
    }
    setBusy(true); feedback('正在读取运行中的代理节点…'); nodeFeedback('正在读取运行中的代理节点…');
    try {
      const result = await backend().get_routing_proxies();
      if (result?.ok === false) throw new Error(result.msg);
      setup.proxy_groups = result.groups || [];
      renderNodes(); renderProviders(); renderOverview(); renderAllProxyChoice();
      const message = setup.proxy_groups.length ? '代理节点已刷新' : '服务未运行或暂未加载节点';
      const tone = setup.proxy_groups.length ? 'success' : 'warning';
      feedback(message, tone); nodeFeedback(message, tone);
    } catch (error) {
      feedback(`刷新失败：${friendlyError(error)}`, 'error');
      nodeFeedback(`刷新失败：${friendlyError(error)}`, 'error');
    } finally { setBusy(false); renderNodes(); }
  }

  function providerReferences(providerId) {
    const value = `proxy:${providerId}`;
    const direct = (routingConfig.default_outbound === value ? 1 : 0) +
      (routingConfig.rules || []).filter(rule => rule.enabled !== false && rule.outbound === value).length;
    const enabled = (routingConfig.proxy_providers || []).filter(item => item.enabled);
    const removingLastEnabled = enabled.length === 1 && enabled[0].id === providerId;
    const aggregate = removingLastEnabled
      ? (routingConfig.default_outbound === 'proxy' ? 1 : 0) +
        (routingConfig.rules || []).filter(rule => rule.enabled !== false && rule.outbound === 'proxy').length
      : 0;
    return direct + aggregate;
  }

  function providerIsSaved(provider) {
    const saved = savedConfig?.proxy_providers?.find(item => item.id === provider.id);
    return !!saved && snapshot(saved) === snapshot(provider);
  }

  function refreshOutboundEditors() {
    outboundOptions(byId('routing-default'), routingConfig.default_outbound);
    byId('routing-default')._routingWidget?.refresh();
    renderRules(); renderAllProxyChoice(); renderOverview(); renderNodes();
  }

  function renderProviders() {
    const root = byId('routing-providers');
    destroyRoutingSelects(root); root.replaceChildren();
    const providers = routingConfig.proxy_providers || [];
    const addButton = byId('btn-routing-provider-add');
    addButton.disabled = busy || providers.length >= 16;
    addButton.title = providers.length >= 16 ? '最多可添加 16 个代理订阅' : '添加代理订阅';
    byId('routing-providers-empty').classList.toggle('hidden', providers.length > 0);
    providers.forEach((provider, index) => {
      const state = proxyGroupState(provider.id);
      const preview = providerPreview(provider);
      const previewError = providerPreviewError(provider);
      const cache = providerCache(provider);
      const expanded = providerExpanded.has(provider.id);
      const selected = state?.nodes?.find(item => item.name === state.selected);
      const persistedNodes = partitionNodes(providerNodesRecord(provider.id)?.nodes || preview?.nodes || []).selectable;
      const targetNode = persistedNodes.find(item => preferenceNodeName(item, true) === provider.selected_node)
        || state?.nodes?.find(item => preferenceNodeName(item) === provider.selected_node);
      const selectionActive = runtimeStatus?.running && state?.selected &&
        (provider.selection_mode === 'auto' || runtimeGroupUsesPreference(state, provider));
      const selectionInvalid = provider.selection_mode === 'manual' && !!provider.selected_node &&
        persistedNodes.length > 0 && !targetNode;
      const card = document.createElement('div');
      card.className = `routing-provider${provider.enabled ? '' : ' disabled'}${expanded ? ' expanded' : ''}`;
      card.dataset.providerId = provider.id;

      const header = document.createElement('div'); header.className = 'routing-provider-head';
      const identity = document.createElement('div'); identity.className = 'routing-provider-identity';
      const order = document.createElement('span'); order.className = 'routing-rule-order';
      order.textContent = String(index + 1).padStart(2, '0');
      const title = document.createElement('div');
      const strong = document.createElement('strong'); strong.textContent = provider.name || `订阅 ${index + 1}`;
      const small = document.createElement('small');
      small.textContent = preview
        ? `已解析 ${partitionNodes(preview.nodes).selectable.length} 个可选节点 · ${cache?.available ? `${cache.node_count} 条缓存记录` : '尚未写入缓存'}`
        : `${strategyLabel(provider.strategy)} · ${downloadRouteLabel(provider)} · ${cache?.available ? `${cache.node_count} 条缓存记录` : '暂无节点缓存'} · ${provider.auto_update ? `每 ${Math.round((provider.interval || 3600) / 60)} 分钟自动更新` : '仅手动更新'}`;
      title.append(strong, small); identity.append(order, title);

      const status = document.createElement('div'); status.className = 'routing-provider-status';
      const current = document.createElement('span');
      const unverifiedDraft = !providerIsSaved(provider) && !String(provider.url || '').trim() && !preview;
      current.textContent = previewError ? cache?.available
        ? `本次更新失败，已继续使用缓存：${previewError}`
        : `首次获取失败：${previewError}`
        : selectionInvalid ? `原节点“${provider.selected_node}”失效需重选`
        : unverifiedDraft ? '未验证草稿：填写订阅 URL 后可先预览节点'
          : preview ? persistedNodes.length
            ? provider.selection_mode === 'manual' && targetNode
              ? `目标节点：${targetNode.display_name || targetNode.name} · ${selectionActive ? '当前使用' : '待启用'}`
              : `节点已持久化 · ${provider.selection_mode === 'auto' ? '自动优选' : '尚未选择目标节点'}`
            : cache?.available
              ? '缓存可用，尚未生成节点快照；可重新获取或临时测速'
              : '尚未生成节点快照，请重新获取订阅'
          : state && !providerIsSaved(provider) ? '运行服务仍使用已应用配置；当前修改尚未应用'
            : state ? `运行中：${selected?.display_name || state.selected || '等待检测'} · ${provider.selection_mode === 'manual' ? '手动节点' : '自动优选'}`
            : cache?.available ? '节点缓存可用，启动统一分流后即可连接'
              : '尚未获取节点，首次获取将使用物理网络';
      current.classList.toggle('error', !!previewError && !cache?.available);
      current.classList.toggle('warning', selectionInvalid || !!previewError && !!cache?.available);
      current.title = current.textContent;
      if (selected?.delay) current.textContent += ` · ${selected.delay} ms`;
      status.append(current);
      if (cache?.available) {
        const cacheMeta = document.createElement('span');
        cacheMeta.className = 'routing-provider-cache';
        cacheMeta.textContent = `缓存：${cache.node_count} 条记录 · ${cacheUpdatedLabel(cache.updated_at)} · ${cacheSourceLabel(cache.source)}`;
        cacheMeta.title = cacheMeta.textContent;
        status.append(cacheMeta);
      }

      const actions = document.createElement('div'); actions.className = 'routing-provider-actions';
      const update = document.createElement('button'); update.type = 'button'; update.className = 'btn mini ghost'; update.textContent = '更新服务节点';
      update.disabled = busy || !provider.enabled || !state || !providerIsSaved(provider);
      update.title = !providerIsSaved(provider) ? '请先保存并应用当前订阅修改'
        : !state ? '统一分流服务未运行，当前没有可更新的运行节点'
          : !provider.enabled ? '订阅已停用' : '立即更新运行中服务使用的订阅节点';
      update.onclick = () => refreshProvider(provider.id);
      const fetch = document.createElement('button'); fetch.type = 'button'; fetch.className = 'btn mini ghost'; fetch.textContent = '重新获取订阅';
      fetch.disabled = busy || !String(provider.url || '').trim();
      fetch.title = '立即从订阅地址更新并持久化节点清单';
      fetch.onclick = () => previewProvider(provider);
      const viewNodes = document.createElement('button'); viewNodes.type = 'button'; viewNodes.className = 'btn mini ghost routing-provider-nodes';
      viewNodes.textContent = preview || (state && providerIsSaved(provider))
        ? '查看节点' : busy ? '获取中…' : state ? '验证修改' : '预览节点';
      viewNodes.disabled = busy;
      viewNodes.title = state || preview
        ? `查看“${provider.name}”的 ${partitionNodes((preview || state).nodes).selectable.length} 个可选节点`
        : '独立获取并解析订阅，不启用 TUN、不接管系统流量';
      viewNodes.onclick = () => openProviderNodes(provider);
      const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'btn mini ghost'; edit.textContent = expanded ? '收起' : '编辑';
      edit.disabled = busy;
      edit.onclick = () => { expanded ? providerExpanded.delete(provider.id) : providerExpanded.add(provider.id); renderProviders(); };
      const enabled = document.createElement('label'); enabled.className = 'switch compact-switch';
      const enabledInput = document.createElement('input'); enabledInput.type = 'checkbox'; enabledInput.checked = !!provider.enabled;
      enabledInput.disabled = busy;
      enabledInput.setAttribute('aria-label', `${provider.name || `订阅 ${index + 1}`}启用状态`);
      enabledInput.onchange = async () => {
        const references = providerReferences(provider.id);
        if (!enabledInput.checked && references) {
          const confirmed = await confirmAction({ title: '停用被引用的订阅', message: `有 ${references} 个出口正在引用“${provider.name}”。停用后配置将无法通过预检，需同时修改这些出口。`, confirmText: '仍然停用' });
          if (!confirmed) { enabledInput.checked = true; return; }
        }
        provider.enabled = enabledInput.checked; setDirty(); renderProviders(); refreshOutboundEditors();
      };
      enabled.append(enabledInput, document.createElement('i'));
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn mini danger'; remove.textContent = '删除';
      remove.disabled = busy;
      remove.onclick = async () => {
        const references = providerReferences(provider.id);
        const confirmed = await confirmAction({ title: '删除代理订阅', message: references ? `有 ${references} 个出口正在引用“${provider.name}”。删除后需重新选择这些出口，是否继续？` : `确定删除“${provider.name}”吗？`, confirmText: '删除' });
        if (!confirmed) return;
        routingConfig.proxy_providers.splice(index, 1); providerExpanded.delete(provider.id);
        providerPreviews.delete(provider.id); providerPreviewErrors.delete(provider.id);
        setDirty(); renderProviders(); refreshOutboundEditors();
      };
      actions.append(viewNodes, fetch, update, edit, enabled, remove);
      header.append(identity, status, actions);

      const recovery = document.createElement('div');
      recovery.className = `routing-provider-recovery${previewError && !cache?.available ? '' : ' hidden'}`;
      const recoveryCopy = document.createElement('div');
      const recoveryTitle = document.createElement('strong'); recoveryTitle.textContent = '需要一份初始节点';
      const recoveryText = document.createElement('span');
      recoveryText.textContent = '智能自动更新已尝试缓存节点、物理网络和已检测到的 Windows 系统代理；仍失败时可指定自定义本地代理，或直接导入现有 Clash/Mihomo YAML。';
      recoveryCopy.append(recoveryTitle, recoveryText);
      const recoveryActions = document.createElement('div'); recoveryActions.className = 'routing-provider-recovery-actions';
      const chooseRecovery = document.createElement('button'); chooseRecovery.type = 'button'; chooseRecovery.className = 'btn mini ghost'; chooseRecovery.textContent = '选择恢复出口';
      chooseRecovery.disabled = busy;
      chooseRecovery.onclick = () => { providerExpanded.add(provider.id); renderProviders(); };
      const importRecovery = document.createElement('button'); importRecovery.type = 'button'; importRecovery.className = 'btn mini ghost'; importRecovery.textContent = '导入 YAML';
      importRecovery.disabled = busy;
      importRecovery.onclick = () => importProviderYaml(provider);
      recoveryActions.append(chooseRecovery, importRecovery); recovery.append(recoveryCopy, recoveryActions);

      const detail = document.createElement('div'); detail.className = `routing-provider-detail${expanded ? '' : ' hidden'}`;
      const fields = document.createElement('div'); fields.className = 'routing-provider-fields';
      const nameField = document.createElement('div'); nameField.className = 'field grow';
      const nameLabel = document.createElement('label'); nameLabel.textContent = '订阅名称';
      const name = document.createElement('input'); name.value = provider.name || ''; name.maxLength = 40; name.placeholder = '例如：机场一';
      name.disabled = busy;
      name.oninput = () => { provider.name = name.value; invalidateProviderPreview(provider); syncDirty(); };
      name.onchange = () => { renderProviders(); refreshOutboundEditors(); };
      nameField.append(nameLabel, name);
      const strategyField = document.createElement('div'); strategyField.className = 'field grow';
      const strategyTitle = document.createElement('label'); strategyTitle.textContent = '节点策略';
      const strategy = makeSelect(strategyOptions(), provider.strategy || 'url-test', 'routing-provider-strategy');
      strategy.disabled = busy;
      strategy.onchange = () => { provider.strategy = strategy.value; syncDirty(); renderProviders(); renderNodes(); };
      strategyField.append(strategyTitle, strategy);
      const intervalField = document.createElement('div'); intervalField.className = 'field routing-provider-interval';
      const intervalLabel = document.createElement('label'); intervalLabel.textContent = '自动更新间隔（分钟）';
      const interval = document.createElement('input'); interval.type = 'number'; interval.min = '5'; interval.max = '1440'; interval.value = String(Math.round((provider.interval || 3600) / 60));
      interval.disabled = busy || !provider.auto_update;
      interval.oninput = () => { provider.interval = Math.max(300, Number(interval.value || 60) * 60); syncDirty(); };
      intervalField.append(intervalLabel, interval); fields.append(nameField, strategyField, intervalField);

      const autoUpdateRow = document.createElement('div'); autoUpdateRow.className = 'routing-provider-update-settings';
      const autoUpdateSwitch = document.createElement('label'); autoUpdateSwitch.className = 'switch compact-switch';
      const autoUpdateInput = document.createElement('input'); autoUpdateInput.type = 'checkbox'; autoUpdateInput.checked = !!provider.auto_update;
      autoUpdateInput.disabled = busy; autoUpdateInput.setAttribute('aria-label', `${provider.name || '当前订阅'}自动更新`);
      autoUpdateInput.onchange = () => { provider.auto_update = autoUpdateInput.checked; syncDirty(); renderProviders(); };
      autoUpdateSwitch.append(autoUpdateInput, document.createElement('i'));
      const autoUpdateCopy = document.createElement('div');
      const autoUpdateTitle = document.createElement('strong'); autoUpdateTitle.textContent = '软件运行期间自动更新订阅';
      const autoUpdateHint = document.createElement('small'); autoUpdateHint.textContent = provider.auto_update
        ? `每 ${Math.round((provider.interval || 3600) / 60)} 分钟检查一次；仍可随时手动“重新获取订阅”。`
        : '当前关闭，仅在点击“重新获取订阅”时更新节点。';
      autoUpdateCopy.append(autoUpdateTitle, autoUpdateHint); autoUpdateRow.append(autoUpdateSwitch, autoUpdateCopy);

      const urlLabel = document.createElement('label'); urlLabel.textContent = '订阅 URL';
      const urlWrap = document.createElement('div'); urlWrap.className = 'inp-wrap';
      const url = document.createElement('input'); url.type = 'password'; url.autocomplete = 'off'; url.value = provider.url || ''; url.placeholder = 'https://example.com/subscribe?token=...';
      url.disabled = busy;
      url.oninput = () => { provider.url = url.value; invalidateProviderPreview(provider); syncDirty(); };
      const eye = document.createElement('button'); eye.type = 'button'; eye.className = 'eye'; eye.dataset.secretLabel = '订阅 URL'; eye.onclick = () => toggleSecret(url, eye);
      eye.disabled = busy;
      urlWrap.append(url, eye); if (typeof concealSecret === 'function') concealSecret(url, eye);

      const filters = document.createElement('div'); filters.className = 'routing-provider-filters';
      const includeField = document.createElement('div'); includeField.className = 'field grow';
      const includeLabel = document.createElement('label'); includeLabel.textContent = '只包含节点（可选）';
      const include = document.createElement('input'); include.value = provider.filter || ''; include.placeholder = '正则，例如：港|HK'; include.oninput = () => { provider.filter = include.value; invalidateProviderPreview(provider); syncDirty(); };
      include.disabled = busy;
      includeField.append(includeLabel, include);
      const excludeField = document.createElement('div'); excludeField.className = 'field grow';
      const excludeLabel = document.createElement('label'); excludeLabel.textContent = '排除节点（可选）';
      const exclude = document.createElement('input'); exclude.value = provider.exclude_filter || ''; exclude.placeholder = '正则，例如：流量|过期|官网'; exclude.oninput = () => { provider.exclude_filter = exclude.value; invalidateProviderPreview(provider); syncDirty(); };
      exclude.disabled = busy;
      excludeField.append(excludeLabel, exclude); filters.append(includeField, excludeField);
      const downloadFields = document.createElement('div'); downloadFields.className = 'routing-provider-download';
      const routeField = document.createElement('div'); routeField.className = 'field grow';
      const routeLabel = document.createElement('label'); routeLabel.textContent = '订阅下载出口';
      const route = makeSelect(downloadRouteOptions(), provider.download_route || 'auto', 'routing-provider-download-route');
      route.disabled = busy;
      route.onchange = () => {
        provider.download_route = route.value;
        invalidateProviderPreview(provider); syncDirty(); renderProviders();
      };
      routeField.append(routeLabel, route);
      const proxyField = document.createElement('div');
      proxyField.className = `field grow${(provider.download_route || 'auto') === 'custom-proxy' ? '' : ' hidden'}`;
      const proxyLabel = document.createElement('label'); proxyLabel.textContent = '自定义本地 HTTP 代理';
      const proxyInput = document.createElement('input'); proxyInput.value = provider.download_proxy || ''; proxyInput.placeholder = 'http://127.0.0.1:7897'; proxyInput.autocomplete = 'off';
      proxyInput.disabled = busy;
      proxyInput.oninput = () => { provider.download_proxy = proxyInput.value; invalidateProviderPreview(provider); syncDirty(); };
      proxyField.append(proxyLabel, proxyInput);
      const agentField = document.createElement('div'); agentField.className = 'field grow';
      const agentLabel = document.createElement('label'); agentLabel.textContent = 'User-Agent';
      const agent = document.createElement('input'); agent.value = provider.user_agent || 'Clash-Verge'; agent.placeholder = 'Clash-Verge'; agent.maxLength = 200;
      agent.disabled = busy;
      agent.oninput = () => { provider.user_agent = agent.value; invalidateProviderPreview(provider); syncDirty(); };
      agentField.append(agentLabel, agent);
      downloadFields.append(routeField, proxyField, agentField);
      const routeHint = document.createElement('p'); routeHint.className = 'routing-provider-route-hint';
      routeHint.textContent = (provider.download_route || 'auto') === 'auto'
        ? cache?.available
          ? `内置引擎将通过已保存的 ${cache.node_count} 条缓存记录恢复订阅节点；Clash Verge 未启动或已卸载也不受影响。`
          : '当前没有节点缓存，将通过物理网络完成首次获取；成功后由内置引擎自行更新。'
        : (provider.download_route || 'auto') === 'physical'
          ? '固定绑定上方选择的物理接口，适合首次获取或直连可达的订阅。'
          : (provider.download_route || 'auto') === 'system-proxy'
            ? `仅用于首次迁移或故障恢复。${setup?.system_proxy?.available ? `当前检测到 ${setup.system_proxy.address}。` : '当前未检测到 Windows 手动系统代理。'}`
            : '仅用于首次迁移或故障恢复；只接受 127.0.0.1、::1 或 localhost 上的 HTTP 代理。';
      const importRow = document.createElement('div'); importRow.className = 'routing-provider-import-row';
      const importCopy = document.createElement('span'); importCopy.textContent = '已有 Clash/Mihomo YAML？可直接导入作为本软件的初始节点缓存（最大 10 MB）。';
      const importButton = document.createElement('button'); importButton.type = 'button'; importButton.className = 'btn mini ghost'; importButton.textContent = '导入 YAML';
      importButton.disabled = busy;
      importButton.onclick = () => importProviderYaml(provider);
      importRow.append(importCopy, importButton);
      detail.append(fields, autoUpdateRow, urlLabel, urlWrap, downloadFields, routeHint, filters, importRow);
      card.append(header, recovery, detail); root.append(card);
      if (expanded) {
        enhanceRoutingSelect(strategy, { compact: true });
        enhanceRoutingSelect(route, { compact: true });
      }
    });
  }

  function renderRules() {
    const root = byId('routing-rules');
    destroyRoutingSelects(root);
    root.replaceChildren();
    const rules = routingConfig.rules || [];
    byId('routing-rules-empty').classList.toggle('hidden', rules.length > 0);
    rules.forEach((rule, index) => {
      const row = document.createElement('div');
      row.className = `routing-rule-row${rule.enabled === false ? ' disabled' : ''}`;
      row.dataset.ruleId = rule.id;

      const order = document.createElement('span');
      order.className = 'routing-rule-order';
      order.textContent = String(index + 1).padStart(2, '0');

      const match = makeSelect([
        ['exact', '精确域名', '仅匹配完整域名'],
        ['suffix', '域名后缀', '匹配根域名及子域名'],
        ['wildcard', '通配符', '使用 * 或 ? 匹配'],
      ], rule.match_type || 'suffix', 'routing-match');
      match.disabled = busy;
      match.setAttribute('aria-label', `第 ${index + 1} 条规则匹配方式`);
      match.onchange = () => { rule.match_type = match.value; syncDirty(); };

      const domain = document.createElement('input');
      domain.className = 'routing-domain';
      domain.value = rule.domain || '';
      domain.placeholder = match.value === 'wildcard' ? '*.example.com' : 'example.com';
      domain.setAttribute('aria-label', `第 ${index + 1} 条规则域名`);
      domain.disabled = busy;
      domain.oninput = () => { rule.domain = domain.value; syncDirty(); };
      match.addEventListener('change', () => {
        domain.placeholder = match.value === 'wildcard' ? '*.example.com' : 'example.com';
      });

      const outbound = document.createElement('select');
      outbound.className = 'routing-outbound';
      outboundOptions(outbound, rule.outbound);
      outbound.setAttribute('aria-label', `第 ${index + 1} 条规则出口`);
      outbound.disabled = busy;
      outbound.onchange = () => { rule.outbound = outbound.value; syncDirty(); };

      const actions = document.createElement('div');
      actions.className = 'routing-rule-actions';
      const enabled = document.createElement('label'); enabled.className = 'switch compact-switch routing-rule-switch';
      const enabledInput = document.createElement('input'); enabledInput.type = 'checkbox';
      enabledInput.checked = rule.enabled !== false; enabledInput.disabled = busy;
      enabledInput.setAttribute('aria-label', `第 ${index + 1} 条规则启用状态`);
      enabledInput.onchange = () => {
        rule.enabled = enabledInput.checked;
        syncDirty(); renderRules(); renderOverview();
      };
      enabled.append(enabledInput, document.createElement('i'));
      const up = document.createElement('button');
      up.type = 'button'; up.className = 'btn mini ghost'; up.textContent = '↑'; up.title = '上移';
      up.disabled = busy || index === 0;
      up.onclick = () => moveRule(index, -1);
      const down = document.createElement('button');
      down.type = 'button'; down.className = 'btn mini ghost'; down.textContent = '↓'; down.title = '下移';
      down.disabled = busy || index === rules.length - 1;
      down.onclick = () => moveRule(index, 1);
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'btn mini danger'; remove.textContent = '删除';
      remove.disabled = busy;
      remove.onclick = () => {
        routingConfig.rules.splice(index, 1);
        setDirty(); renderRules(); renderOverview();
      };
      actions.append(enabled, up, down, remove);
      row.append(order, match, domain, outbound, actions);
      root.append(row);
      enhanceRoutingSelect(match, { compact: true });
      enhanceRoutingSelect(outbound, { compact: true });
    });
  }

  function moveRule(index, offset) {
    const next = index + offset;
    if (next < 0 || next >= routingConfig.rules.length) return;
    [routingConfig.rules[index], routingConfig.rules[next]] =
      [routingConfig.rules[next], routingConfig.rules[index]];
    setDirty(); renderRules();
  }

  async function testDomainMatch() {
    if (busy) return;
    const domain = byId('routing-test-domain').value.trim();
    const target = byId('routing-test-result');
    if (!domain) { target.textContent = '请输入要测试的完整域名。'; target.className = 'routing-test-result error'; return; }
    try {
      const result = await backend().preview_routing_match(collectConfig(), domain);
      if (result?.ok === false) throw new Error(result.msg);
      const match = result.result;
      const source = match.source === 'user' ? `user · 第 ${match.rule_index} 条规则（${match.rule_domain}）`
        : match.source === 'builtin' ? `builtin · ${routingConfig.builtin_rule_pack}（${match.rule_domain}）`
          : 'default · MATCH';
      target.replaceChildren();
      const strong = document.createElement('strong'); strong.textContent = `${match.domain} → ${match.outbound_name}`;
      const span = document.createElement('span'); span.textContent = `${source} · ${match.detail}`;
      target.append(strong, span); target.className = `routing-test-result ${match.available ? 'success' : 'warning'}`;
    } catch (error) {
      target.textContent = friendlyError(error); target.className = 'routing-test-result error';
    }
  }

  function collectConfig() {
    return {
      ...routingConfig,
      schema_version: 2,
      enabled: byId('routing-enabled').checked,
      capture_mode: byId('routing-capture-mode').value,
      traffic_mode: byId('routing-traffic-mode').value,
      mixed_port: Number(byId('routing-mixed-port').value || 17890),
      builtin_rule_pack: byId('routing-builtin-pack').value,
      physical_interface: byId('routing-interface').value,
      default_outbound: byId('routing-default').value,
      proxy_strategy: byId('routing-proxy-strategy').value,
      proxy_providers: (routingConfig.proxy_providers || []).map(item => ({ ...item })),
      dns_servers: byId('routing-dns').value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean),
      default_nameserver: byId('routing-default-dns').value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean),
      proxy_server_nameserver: byId('routing-proxy-dns').value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean),
      direct_nameserver: byId('routing-direct-dns').value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean),
      rules: (routingConfig.rules || []).map(rule => ({ ...rule, enabled: rule.enabled !== false })),
    };
  }

  function feedback(message, tone = '') {
    const element = byId('routing-feedback');
    element.textContent = message || '';
    element.className = `form-feedback${tone ? ` ${tone}` : ''}`;
  }

  function nodeFeedback(message, tone = '') {
    const element = byId('routing-node-feedback');
    if (!element) return;
    element.textContent = message || '';
    element.className = `form-feedback routing-node-feedback${tone ? ` ${tone}` : ''}`;
  }

  function updateApplyButton() {
    const applyButton = byId('btn-routing-apply');
    if (!applyButton || busy) return;
    const configEnabled = !!byId('routing-enabled')?.checked;
    const serviceInstalled = !!setup?.status?.installed;
    const serviceUnknown = setup?.status?.service_state === 'Unknown';
    applyButton.textContent = serviceUnknown ? '重新确认后应用' : !configEnabled && !serviceInstalled
      ? '保存配置' : '保存并应用';
  }

  function routingNeedsUac(nextEnabled) {
    const status = setup?.status || {};
    if (status.service_state === 'Unknown') return true;
    if (nextEnabled) return !status.installed || status.service_backend !== 'native';
    return !!status.installed && status.service_backend === 'legacy';
  }

  function setBusy(value, label = '') {
    busy = value;
    const preview = byId('btn-routing-preview');
    const apply = byId('btn-routing-apply');
    const refresh = byId('btn-routing-refresh');
    const addProvider = byId('btn-routing-provider-add');
    const nodeRefresh = byId('btn-routing-nodes-refresh');
    [preview, apply, refresh, nodeRefresh].forEach(button => { if (button) button.disabled = value; });
    if (addProvider) addProvider.disabled = value || (routingConfig?.proxy_providers?.length || 0) >= 16;
    preview.textContent = value && label === 'preview' ? '预检中…' : '仅预检';
    apply.textContent = value && label === 'apply'
      ? (routingNeedsUac(!!byId('routing-enabled')?.checked) ? '等待 UAC / 应用中…' : '应用中…')
      : '保存并应用';
    if (!value) updateApplyButton();
    if (routingConfig) { renderProviders(); renderRules(); renderNodes(); }
    [
      'routing-enabled', 'routing-capture-mode', 'routing-traffic-mode', 'routing-interface',
      'routing-mixed-port', 'routing-dns', 'routing-default-dns', 'routing-proxy-dns',
      'routing-direct-dns', 'routing-builtin-pack', 'routing-proxy-strategy',
      'routing-default', 'routing-test-domain', 'btn-routing-test', 'btn-routing-add',
      'routing-node-search', 'btn-routing-discard',
    ].forEach(id => {
      const control = byId(id);
      if (control) control.disabled = value;
    });
    document.querySelectorAll('#page-routing select').forEach(select => select._routingWidget?.refresh());
    window.ProxyWorkspace?.sync?.();
  }

  function hydrateSetup(result, preserveDraft = false, markFresh = true) {
    const keepDraft = loaded && dirty && preserveDraft;
    setup = result;
    appliedConfig = clone(result.config);
    (appliedConfig.proxy_providers || []).forEach(normalizeProviderDefaults);
    runtimeStatus = clone(result.status);
    savedConfig = clone(appliedConfig);
    loaded = true;
    if (markFresh) lastLoadedAt = Date.now();
    if (!keepDraft) {
      routingConfig = clone(appliedConfig);
      providerPreviews.clear();
      providerPreviewErrors.clear();
      hydrateProviderNodes();
      reapplyTestJobs();
      fillForm();
      savedSnapshot = snapshot(collectConfig());
      setDirty(false);
    } else {
      hydrateProviderNodes();
      reapplyTestJobs();
      renderProviders(); renderAllProxyChoice(); renderOverview(); renderNodes();
    }
    setStatus(runtimeStatus);
    showConflicts(result.tun_conflicts || []);
    window.ProxyWorkspace?.sync?.();
    return result;
  }

  function loadBootstrap() {
    if (loaded) return Promise.resolve(setup);
    if (bootstrapInFlight) return bootstrapInFlight;
    bootstrapInFlight = (async () => {
      setBusy(true);
      try {
        const result = await backend().get_routing_bootstrap();
        if (result?.ok === false) throw new Error(result.msg || '读取代理快照失败');
        hydrateSetup(result, false, false);
        return result;
      } catch (error) {
        feedback(`读取失败：${friendlyError(error)}`, 'error');
        return null;
      } finally {
        setBusy(false);
        bootstrapInFlight = null;
        window.ProxyWorkspace?.sync?.();
      }
    })();
    bootstrapInFlight.then(result => {
      if (result) void loadSetup(true, true, true);
    });
    return bootstrapInFlight;
  }

  function loadSetup(force = false, preserveDraft = false, background = false) {
    if (!loaded && !force) return loadBootstrap();
    if (setupInFlight) return setupInFlight;
    const fresh = loaded && Date.now() - lastLoadedAt < SETUP_TTL_MS;
    if (!force && fresh) {
      window.ProxyWorkspace?.sync?.();
      return Promise.resolve(setup);
    }
    setupInFlight = (async () => {
      if (!background) setBusy(true);
      if (!dirty) feedback('');
      try {
        const result = await backend().get_routing_setup();
        if (result?.ok === false) throw new Error(result.msg || '读取分流配置失败');
        return hydrateSetup(result, preserveDraft || !force, true);
      } catch (error) {
        if (!background || !loaded) {
          feedback(`读取失败：${friendlyError(error)}`, 'error');
        }
        return null;
      } finally {
        if (!background) setBusy(false);
        setupInFlight = null;
        window.ProxyWorkspace?.sync?.();
      }
    })();
    return setupInFlight;
  }

  async function preview() {
    if (busy) return;
    setBusy(true, 'preview');
    feedback('正在校验域名、出口、网络接口和 Mihomo 配置…');
    try {
      const result = normalizeResult(await backend().preview_routing(collectConfig()), '配置预检通过');
      if (!result.ok) throw new Error(result.msg);
      const warnings = result.warnings?.length ? `；${result.warnings.join('；')}` : '';
      feedback(`${result.msg}${warnings}`, result.warnings?.length ? 'warning' : 'success');
      toast({ ok: true, msg: result.msg });
    } catch (error) {
      feedback(`预检失败：${friendlyError(error)}`, 'error');
      toast({ ok: false, msg: friendlyError(error) });
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (busy) return;
    const next = collectConfig();
    const requiresUac = routingNeedsUac(!!next.enabled);
    const legacyDisable = !next.enabled && !!setup?.status?.installed
      && setup?.status?.service_backend === 'legacy';
    const requiresConfirmation = !!next.enabled || legacyDisable
      || setup?.status?.service_state === 'Unknown';
    const action = next.enabled ? '启用或更新统一分流' : '关闭统一分流';
    if (requiresConfirmation) {
      const confirmed = await confirmAction({
        title: action,
        message: next.enabled
          ? `系统流量将由 CXVPN 的 Mihomo TUN 接管；启动后会验证实际节点、DNS 和公网访问，失败自动恢复原始路由。${requiresUac ? '首次安装或升级路由服务会弹出 Windows UAC；' : ''}请确认其他代理软件的 TUN 模式已关闭。`
          : legacyDisable
            ? '检测到旧版分流服务，将停止并注销它，系统恢复使用当前 Windows 路由。该迁移操作会弹出 Windows UAC。'
            : '将停止 Mihomo 运行时并恢复 Windows 原始路由；CXVPN 路由服务会保留，以便下次免 UAC 启动。',
        confirmText: requiresUac ? '授权并应用' : next.enabled ? '确认开启' : '确认关闭',
      });
      if (!confirmed) return;
    }
    setBusy(true, 'apply');
    feedback(requiresUac
      ? '正在预检并等待 Windows 管理员授权，请留意 UAC 弹窗…'
      : next.enabled
        ? '正在校验配置并启动 Mihomo，节点与公网自检通过后才会提交…'
        : '正在停止 Mihomo 并恢复 Windows 原始路由…');
    try {
      const result = normalizeResult(await backend().apply_routing(next), '分流配置已应用');
      if (!result.ok) throw new Error(result.msg);
      routingConfig = JSON.parse(JSON.stringify(result.config));
      (routingConfig.proxy_providers || []).forEach(normalizeProviderDefaults);
      appliedConfig = clone(routingConfig);
      savedConfig = JSON.parse(JSON.stringify(routingConfig));
      if (next.enabled) {
        providerPreviews.clear();
        providerPreviewErrors.clear();
      }
      if (typeof CFG !== 'undefined') CFG.routing = routingConfig;
      setup.status = result.status;
      runtimeStatus = clone(result.status);
      let runtimeWarning = '';
      try {
        const refreshed = await backend().get_routing_setup();
        if (refreshed?.ok !== false) {
          setup = refreshed;
          routingConfig = JSON.parse(JSON.stringify(refreshed.config || routingConfig));
          (routingConfig.proxy_providers || []).forEach(normalizeProviderDefaults);
          appliedConfig = clone(refreshed.config || routingConfig);
          (appliedConfig.proxy_providers || []).forEach(normalizeProviderDefaults);
          runtimeStatus = clone(refreshed.status || result.status);
          savedConfig = JSON.parse(JSON.stringify(routingConfig));
        } else runtimeWarning = refreshed?.msg || '运行状态刷新失败';
      } catch (error) {
        runtimeWarning = friendlyError(error);
      }
      hydrateProviderNodes();
      reapplyTestJobs();
      fillForm();
      savedSnapshot = snapshot(collectConfig());
      setDirty(false);
      runtimeStatus = clone(runtimeStatus || result.status);
      lastLoadedAt = Date.now();
      setStatus(runtimeStatus);
      const warningItems = [...(result.warnings || [])];
      if (runtimeWarning) warningItems.push(`配置已保存，但运行状态读取失败：${runtimeWarning}`);
      const warnings = warningItems.length ? `；${warningItems.join('；')}` : '';
      feedback(`${result.msg}${warnings}`, warningItems.length ? 'warning' : 'success');
      toast({ ok: true, msg: result.msg });
    } catch (error) {
      feedback(`应用失败：${friendlyError(error)}`, 'error');
      toast({ ok: false, msg: friendlyError(error) });
    } finally {
      setBusy(false);
      window.ProxyWorkspace?.sync?.();
    }
  }

  function bindPage() {
    byId('btn-routing-refresh').onclick = async () => {
      if (dirty) {
        const confirmed = await confirmAction({ title: '重新读取配置', message: '当前有未保存的修改，重新读取会放弃这些修改。', confirmText: '放弃并刷新' });
        if (!confirmed) return;
      }
      await loadSetup(true, false);
    };
    byId('btn-routing-preview').onclick = preview;
    byId('btn-routing-apply').onclick = apply;
    byId('btn-routing-discard').onclick = () => loadSetup(true, false);
    byId('btn-routing-test').onclick = testDomainMatch;
    byId('routing-test-domain').onkeydown = event => { if (event.key === 'Enter') testDomainMatch(); };
    byId('btn-routing-nodes-refresh').onclick = refreshNodes;
    byId('btn-routing-group-test').onclick = testProxyGroup;
    byId('btn-routing-test-cancel').onclick = cancelProxyTest;
    byId('btn-routing-auto-select').onclick = () => {
      const provider = providerForGroup(byId('routing-node-group')?.value || '');
      if (provider) saveProxyPreference(provider.id, 'auto', '');
    };
    byId('routing-node-search').oninput = renderNodes;
    byId('routing-node-alive').onchange = renderNodes;
    byId('routing-node-sort').onchange = renderNodes;
    byId('btn-routing-locate-current').onclick = locateCurrentNode;
    enhanceRoutingSelect(byId('routing-node-sort'), { compact: true });
    byId('routing-node-group').onchange = () => { nodeRegionFilter = 'all'; renderNodes(); };
    document.querySelectorAll('[data-routing-tab]').forEach(button => {
      button.onclick = () => setRoutingTab(button.dataset.routingTab);
      button.onkeydown = event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const tabs = [...document.querySelectorAll('[data-routing-tab]')];
        const index = tabs.indexOf(button);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
          : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        tabs[next].focus();
        setRoutingTab(tabs[next].dataset.routingTab);
      };
    });
    document.querySelector('[data-routing-workbench="nodes"]')?.addEventListener('click', () => setRoutingTab('nodes'));
    ['routing-enabled', 'routing-capture-mode', 'routing-traffic-mode', 'routing-interface',
      'routing-default', 'routing-builtin-pack', 'routing-mixed-port', 'routing-dns',
      'routing-default-dns', 'routing-proxy-dns', 'routing-direct-dns'].forEach(id => {
      const element = byId(id);
      element.addEventListener(id.includes('dns') || id === 'routing-mixed-port' ? 'input' : 'change', () => {
        if (id === 'routing-enabled') routingConfig.enabled = element.checked;
        if (id === 'routing-capture-mode') routingConfig.capture_mode = element.value;
        if (id === 'routing-traffic-mode') routingConfig.traffic_mode = element.value;
        if (id === 'routing-interface') routingConfig.physical_interface = element.value;
        if (id === 'routing-default') routingConfig.default_outbound = element.value;
        if (id === 'routing-builtin-pack') routingConfig.builtin_rule_pack = element.value;
        if (id === 'routing-mixed-port') routingConfig.mixed_port = Number(element.value || 17890);
        if (id === 'routing-dns') routingConfig.dns_servers = element.value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean);
        if (id === 'routing-default-dns') routingConfig.default_nameserver = element.value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean);
        if (id === 'routing-proxy-dns') routingConfig.proxy_server_nameserver = element.value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean);
        if (id === 'routing-direct-dns') routingConfig.direct_nameserver = element.value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean);
        syncDirty(); renderOverview(); updateApplyButton();
      });
    });
    byId('routing-proxy-strategy').onchange = () => {
      routingConfig.proxy_strategy = byId('routing-proxy-strategy').value;
      setDirty(); renderAllProxyChoice(); renderOverview();
    };
    byId('btn-routing-provider-add').onclick = () => {
      const now = Date.now().toString(36);
      routingConfig.proxy_providers = routingConfig.proxy_providers || [];
      if (routingConfig.proxy_providers.length >= 16) {
        feedback('代理订阅最多可添加 16 个。', 'warning');
        return;
      }
      routingConfig.proxy_providers.push({
        id: `provider-${now}-${Math.random().toString(16).slice(2, 7)}`,
        name: `订阅 ${routingConfig.proxy_providers.length + 1}`,
        url: '',
        enabled: false,
        strategy: 'url-test',
        selection_mode: 'auto',
        selected_node: '',
        auto_update: false,
        interval: 3600,
        filter: '',
        exclude_filter: '',
        download_route: 'auto',
        download_proxy: '',
        user_agent: 'Clash-Verge',
      });
      providerExpanded.add(routingConfig.proxy_providers.at(-1).id);
      setDirty();
      renderProviders();
      outboundOptions(byId('routing-default'), routingConfig.default_outbound);
      byId('routing-default')._routingWidget?.refresh();
      renderAllProxyChoice();
      renderRules();
      byId('routing-providers').lastElementChild?.querySelector('.inp-wrap input')?.focus();
    };
    byId('btn-routing-add').onclick = () => {
      routingConfig.rules.push({
        id: `rule-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        enabled: true,
        match_type: 'suffix',
        domain: '',
        outbound: 'physical',
      });
      setDirty(); renderRules(); renderOverview();
      byId('routing-rules').lastElementChild?.querySelector('.routing-domain')?.focus();
    };
    document.querySelector('#nav [data-page="routing"]')?.addEventListener('click', () => loadSetup());
    document.querySelector('#nav [data-page="proxy"]')?.addEventListener('click', () => loadSetup());
    document.querySelectorAll('#nav [data-page]').forEach(item => {
      item.addEventListener('click', () => closeRoutingSelect());
    });
    document.addEventListener('click', event => {
      if (!openSelect) return;
      if (openSelect.root.contains(event.target) || openSelect.menu.contains(event.target)) return;
      closeRoutingSelect();
    });
    window.addEventListener('resize', () => placeRoutingMenu(openSelect));
    byId('main-content')?.addEventListener('scroll', () => placeRoutingMenu(openSelect), { passive: true });
    window.addEventListener('beforeunload', event => {
      if (!dirty) return;
      event.preventDefault(); event.returnValue = '';
    });
  }

  window.RoutingWorkspace = {
    load: loadSetup,
    refresh: (preserveDraft = true) => loadSetup(true, preserveDraft),
    openTab: setRoutingTab,
    state: () => ({
      setup,
      config: routingConfig,
      appliedConfig,
      draftConfig: routingConfig,
      runtimeStatus,
      groups: nodeGroups(),
      busy,
      loaded,
      dirty,
    }),
    nodeSummary: (groupId = 'all', useDraft = false) => nodeSummary(
      useDraft ? routingConfig : appliedConfig, groupId, true),
    openNodes: (groupId = '') => {
      pendingNodeGroup = groupId || pendingNodeGroup;
      renderNodes();
    },
    renderNodes,
    refreshNodes,
    previewProvider: providerId => {
      const provider = routingConfig?.proxy_providers?.find(item => item.id === providerId);
      return provider ? previewProvider(provider) : Promise.resolve();
    },
  };

  window.addEventListener('pywebviewready', () => {
    bindPage();
    void loadSetup();
  });
})();
