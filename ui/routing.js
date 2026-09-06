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
  let autoPolicyDraft = null;
  let autoPolicyProviderId = '';
  let nodeRegionFilter = 'all';
  let renderedNodeGroup = '';
  let historyLoading = false;
  const SETUP_TTL_MS = 15000;
  const TEST_POLL_MS = 250;
  const TEST_POLL_MAX_MS = 5000;
  const DNS_RECOMMENDED = Object.freeze({
    dns_enhanced_mode: 'fake-ip', dns_respect_rules: true,
    dns_servers: ['223.5.5.5', '119.29.29.29'],
    default_nameserver: ['223.5.5.5', '119.29.29.29'],
    proxy_server_nameserver: [
      'https://dns.alidns.com/dns-query',
      'https://doh.pub/dns-query',
    ],
    direct_nameserver: ['223.5.5.5', '119.29.29.29'],
    fake_ip_range: '198.18.0.0/16',
    fake_ip_filter: ['+.lan', '+.local', 'localhost.ptlogin2.qq.com'],
    nameserver_policy: [],
  });

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
    if (!select) return null;
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

  function providerSelectionLabel(provider) {
    if (provider?.selection_mode === 'manual') return '手动节点';
    if (provider?.auto_policy?.enabled) {
      const regions = (provider.auto_policy.stages || [])
        .map(stage => nodeTools?.REGION_LABELS?.[stage.region] || stage.region)
        .filter(Boolean).join(' → ');
      return regions ? `智能优选：${regions}` : '智能优选';
    }
    return '自动优选';
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
    const rawPolicy = provider.auto_policy && typeof provider.auto_policy === 'object'
      ? provider.auto_policy : {};
    provider.auto_policy = {
      enabled: !!rawPolicy.enabled,
      stages: Array.isArray(rawPolicy.stages) ? rawPolicy.stages.map(stage => ({
        region: String(stage?.region || '').toUpperCase(),
        region_keywords: Array.isArray(stage?.region_keywords) ? [...stage.region_keywords] : [],
        preferred_keywords: Array.isArray(stage?.preferred_keywords) ? [...stage.preferred_keywords] : [],
        selection_mode: stage?.selection_mode === 'failure' ? 'failure' : 'latency',
        preferred_node: String(stage?.preferred_node || ''),
      })) : [],
      fallback: rawPolicy.fallback === 'all' ? 'all' : 'reject',
      latency_tolerance: Math.min(500, Math.max(0, Number(rawPolicy.latency_tolerance ?? 20) || 0)),
      latency_tolerance_unit: rawPolicy.latency_tolerance_unit === 'percent' ? 'percent' : 'ms',
    };
    return provider;
  }

  function defaultAutoPolicyDraft() {
    return {
      enabled: true,
      stages: [
        { region: 'JP', region_keywords: [], preferred_keywords: ['高速专线', 'IPLC', 'IEPL'], selection_mode: 'latency', preferred_node: '' },
        { region: 'US', region_keywords: [], preferred_keywords: ['高速专线', 'IPLC', 'IEPL'], selection_mode: 'latency', preferred_node: '' },
      ],
      fallback: 'reject',
      latency_tolerance: 20,
      latency_tolerance_unit: 'ms',
    };
  }

  function autoPolicyFeedback(message, tone = 'warning') {
    const root = byId('routing-auto-policy-feedback');
    if (!root) return;
    root.textContent = message;
    root.dataset.tone = tone;
  }

  function autoPolicyProviderNodes() {
    const provider = routingConfig?.proxy_providers?.find(item => item.id === autoPolicyProviderId);
    if (!provider) return [];
    const persisted = providerNodesRecord(provider.id)?.nodes;
    const preview = providerPreview(provider)?.nodes;
    const runtime = proxyGroupState(provider.id)?.nodes;
    const source = [persisted, preview, runtime].find(
      nodes => Array.isArray(nodes) && nodes.length) || [];
    return partitionNodes(source).selectable;
  }

  function autoPolicyNodeText(node) {
    return preferenceNodeName(node, true);
  }

  function autoPolicyStageNodes(stage) {
    const extras = (stage.region_keywords || []).map(value => String(value).toLocaleLowerCase());
    return autoPolicyProviderNodes().filter(node => {
      if (nodeTools.regionCode(node) === stage.region) return true;
      const text = autoPolicyNodeText(node).toLocaleLowerCase();
      return extras.some(keyword => text.includes(keyword));
    });
  }

  function createKeywordEditor(labelText, values, placeholder, onChange) {
    const field = document.createElement('div'); field.className = 'field routing-keyword-field';
    const label = document.createElement('label'); label.textContent = labelText;
    const editor = document.createElement('div'); editor.className = 'routing-keyword-editor';
    const tags = document.createElement('div'); tags.className = 'routing-keyword-tags';
    const refreshTags = () => {
      tags.innerHTML = '';
      values.forEach((value, index) => {
        const tag = document.createElement('span'); tag.className = 'routing-keyword-tag';
        const textNode = document.createElement('span'); textNode.textContent = value;
        const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×';
        remove.setAttribute('aria-label', `删除关键词 ${value}`);
        remove.onclick = () => { values.splice(index, 1); refreshTags(); onChange(); };
        tag.append(textNode, remove); tags.append(tag);
      });
    };
    const inputRow = document.createElement('div'); inputRow.className = 'routing-keyword-input-row';
    const input = document.createElement('input'); input.maxLength = 40; input.placeholder = placeholder;
    input.setAttribute('aria-label', `${labelText}，每次添加一个`);
    const add = document.createElement('button'); add.type = 'button'; add.className = 'btn mini ghost'; add.textContent = '添加';
    const commit = () => {
      const value = input.value.trim();
      if (!value) return;
      if (/[,，|\r\n]/.test(value)) {
        input.value = '';
        autoPolicyFeedback('每次只能添加一个关键词，请不要使用逗号、竖线或换行分隔。');
        return;
      }
      if (values.length >= 12) { autoPolicyFeedback('每类关键词最多添加 12 个。'); return; }
      if (values.some(item => item.toLocaleLowerCase() === value.toLocaleLowerCase())) {
        autoPolicyFeedback(`关键词“${value}”已经存在。`); return;
      }
      values.push(value); input.value = ''; refreshTags(); onChange(); autoPolicyFeedback('', '');
    };
    input.oninput = () => {
      if (/[,，|\r\n]/.test(input.value)) {
        input.value = '';
        autoPolicyFeedback('每次只能添加一个关键词，请单独输入后点击“添加”。');
      }
    };
    input.onkeydown = event => {
      if (event.key === 'Enter') { event.preventDefault(); commit(); }
    };
    add.onclick = commit;
    inputRow.append(input, add); editor.append(tags, inputRow); field.append(label, editor);
    refreshTags();
    return field;
  }

  function closeAutoPolicy() {
    const panel = byId('routing-auto-policy');
    if (!panel) return;
    destroyRoutingSelects(byId('routing-auto-policy-stages'));
    panel.classList.add('hidden');
    autoPolicyDraft = null;
    autoPolicyProviderId = '';
  }

  function autoPolicyFlowLabels() {
    const labels = [];
    (autoPolicyDraft?.stages || []).forEach(stage => {
      const region = nodeTools?.REGION_LABELS?.[stage.region] || stage.region || '未选择地区';
      if (stage.selection_mode === 'failure' && stage.preferred_node) labels.push(`${region}首选节点`);
      if (stage.preferred_keywords?.length) labels.push(`${region}优先线路`);
      labels.push(`${region}其他节点`);
    });
    labels.push(autoPolicyDraft?.fallback === 'all' ? '全部节点兜底' : '停止并提示');
    return labels;
  }

  function renderAutoPolicyFlow() {
    const root = byId('routing-auto-policy-flow');
    if (!root) return;
    root.innerHTML = '';
    autoPolicyFlowLabels().forEach(label => {
      const item = document.createElement('b'); item.textContent = label; root.append(item);
    });
  }

  function updateAutoPolicyToleranceHelp() {
    if (!autoPolicyDraft) return;
    const tolerance = byId('routing-auto-policy-tolerance');
    const toleranceUnit = byId('routing-auto-policy-tolerance-unit');
    const hasLatencyStage = autoPolicyDraft.stages.some(stage => stage.selection_mode !== 'failure');
    const currentDelays = autoPolicyProviderNodes().map(node => Number(node.delay)).filter(value => value > 0);
    const referenceDelay = currentDelays.length ? Math.min(...currentDelays) : 100;
    const converted = Math.round(referenceDelay * Number(autoPolicyDraft.latency_tolerance || 0) / 100);
    tolerance.disabled = !hasLatencyStage;
    toleranceUnit.disabled = !hasLatencyStage;
    toleranceUnit._routingWidget?.refresh();
    byId('routing-auto-policy-tolerance-help').textContent = !hasLatencyStage
      ? '当前所有地区均为仅故障切换，不使用延迟容差。'
      : toleranceUnit.value === 'percent'
        ? `按最近测速最低值换算；当前参考 ${referenceDelay} ms，约等于 ${converted} ms。重新应用时更新。`
        : '0 表示严格最低；建议 20 ms，减少微小波动导致的频繁切换。';
  }

  function renderAutoPolicy() {
    const panel = byId('routing-auto-policy');
    if (!panel || !autoPolicyDraft) return;
    const enabled = byId('routing-auto-policy-enabled');
    enabled.checked = !!autoPolicyDraft.enabled;
    const tolerance = byId('routing-auto-policy-tolerance');
    tolerance.value = String(autoPolicyDraft.latency_tolerance ?? 20);
    const toleranceUnit = byId('routing-auto-policy-tolerance-unit');
    toleranceUnit.value = autoPolicyDraft.latency_tolerance_unit === 'percent' ? 'percent' : 'ms';
    tolerance.max = toleranceUnit.value === 'percent' ? '100' : '500';
    tolerance.step = '1';
    toleranceUnit._routingWidget?.refresh();
    updateAutoPolicyToleranceHelp();
    panel.classList.toggle('disabled-policy', !autoPolicyDraft.enabled);
    panel.querySelectorAll('[data-auto-fallback]').forEach(button => {
      button.classList.toggle('active', button.dataset.autoFallback === autoPolicyDraft.fallback);
      button.setAttribute('aria-pressed', String(button.dataset.autoFallback === autoPolicyDraft.fallback));
    });
    const root = byId('routing-auto-policy-stages');
    destroyRoutingSelects(root);
    root.innerHTML = '';
    const labels = nodeTools?.REGION_LABELS || {};
    const entries = Object.entries(labels);
    autoPolicyDraft.stages.forEach((stage, index) => {
      const row = document.createElement('div'); row.className = 'routing-auto-policy-stage';
      const order = document.createElement('div'); order.className = 'routing-auto-policy-rank'; order.title = `优先级 ${index + 1}`;
      const orderLabel = document.createElement('span'); orderLabel.textContent = '优先级';
      const orderValue = document.createElement('b'); orderValue.textContent = String(index + 1).padStart(2, '0');
      order.append(orderLabel, orderValue);
      const regionField = document.createElement('div'); regionField.className = 'field';
      const regionLabel = document.createElement('label'); regionLabel.textContent = '地区';
      const region = document.createElement('select'); region.setAttribute('aria-label', `第 ${index + 1} 个优选地区`);
      entries.forEach(([code, label]) => region.append(option(code, label)));
      region.value = stage.region;
      region.onchange = () => { stage.region = region.value; stage.preferred_node = ''; renderAutoPolicy(); };
      regionField.append(regionLabel, region);
      const modeField = document.createElement('div'); modeField.className = 'field';
      const modeLabel = document.createElement('label'); modeLabel.textContent = '组内切换';
      const mode = document.createElement('select'); mode.setAttribute('aria-label', `第 ${index + 1} 个地区组内切换方式`);
      mode.append(option('latency', '低延迟优化', '在容差外自动选择更低延迟节点'));
      mode.append(option('failure', '仅故障切换', '首选节点健康时始终保持'));
      mode.value = stage.selection_mode === 'failure' ? 'failure' : 'latency';
      mode.onchange = () => { stage.selection_mode = mode.value; renderAutoPolicy(); };
      modeField.append(modeLabel, mode);
      let nodeField = null;
      let preferredNode = null;
      if (stage.selection_mode === 'failure') {
        nodeField = document.createElement('div'); nodeField.className = 'field routing-auto-policy-node';
        const nodeLabel = document.createElement('label'); nodeLabel.textContent = '首选节点';
        preferredNode = document.createElement('select'); preferredNode.setAttribute('aria-label', `第 ${index + 1} 个地区首选节点`);
        preferredNode.append(option('', '请选择首选节点'));
        const stageNodes = autoPolicyStageNodes(stage);
        stageNodes.forEach(node => {
          const name = autoPolicyNodeText(node);
          const detail = node.alive === false ? '最近检测不可用' : Number(node.delay) > 0 ? `${node.delay} ms` : '未测速';
          preferredNode.append(option(name, name, detail));
        });
        if (stage.preferred_node && !stageNodes.some(node => autoPolicyNodeText(node) === stage.preferred_node)) {
          preferredNode.append(option(stage.preferred_node, `${stage.preferred_node}（已失效）`, '请更新订阅后重新选择'));
        }
        preferredNode.value = stage.preferred_node || '';
        preferredNode.onchange = () => { stage.preferred_node = preferredNode.value; renderAutoPolicyFlow(); };
        nodeField.append(nodeLabel, preferredNode);
      }
      const keywordFields = document.createElement('div'); keywordFields.className = 'routing-auto-policy-keywords';
      keywordFields.append(
        createKeywordEditor('优先线路关键词', stage.preferred_keywords, '例如：高速专线', renderAutoPolicyFlow),
        createKeywordEditor('地区补充关键词（可选）', stage.region_keywords, '例如：JP-Tokyo', () => {
          if (stage.selection_mode === 'failure') {
            stage.preferred_node = '';
            renderAutoPolicy();
          } else {
            renderAutoPolicyFlow();
          }
        }),
      );
      const actions = document.createElement('div'); actions.className = 'routing-auto-policy-stage-actions';
      const actionsLabel = document.createElement('span'); actionsLabel.textContent = '排序';
      const actionButtons = document.createElement('div'); actionButtons.className = 'routing-auto-policy-stage-action-buttons';
      const up = document.createElement('button'); up.type = 'button'; up.className = 'btn mini ghost'; up.textContent = '↑'; up.title = '提高优先级'; up.setAttribute('aria-label', `提高${labels[stage.region] || stage.region}优先级`); up.disabled = index === 0;
      up.onclick = () => { [autoPolicyDraft.stages[index - 1], autoPolicyDraft.stages[index]] = [stage, autoPolicyDraft.stages[index - 1]]; renderAutoPolicy(); };
      const down = document.createElement('button'); down.type = 'button'; down.className = 'btn mini ghost'; down.textContent = '↓'; down.title = '降低优先级'; down.setAttribute('aria-label', `降低${labels[stage.region] || stage.region}优先级`); down.disabled = index === autoPolicyDraft.stages.length - 1;
      down.onclick = () => { [autoPolicyDraft.stages[index], autoPolicyDraft.stages[index + 1]] = [autoPolicyDraft.stages[index + 1], stage]; renderAutoPolicy(); };
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn mini danger'; remove.textContent = '删除'; remove.onclick = () => { autoPolicyDraft.stages.splice(index, 1); renderAutoPolicy(); };
      actionButtons.append(up, down, remove);
      actions.append(actionsLabel, actionButtons);
      row.append(order, regionField, modeField);
      if (nodeField) row.append(nodeField);
      row.append(actions, keywordFields);
      root.append(row);
      enhanceRoutingSelect(region, { compact: true });
      enhanceRoutingSelect(mode, { compact: true });
      if (preferredNode) enhanceRoutingSelect(preferredNode, { compact: true });
    });
    byId('routing-auto-policy-empty').classList.toggle('hidden', autoPolicyDraft.stages.length > 0);
    byId('btn-routing-auto-policy-add').disabled = autoPolicyDraft.stages.length >= 8;
    byId('btn-routing-auto-policy-save').textContent = autoPolicyDraft.enabled
      ? runtimeStatus?.running ? '保存并应用智能优选' : '保存智能优选'
      : runtimeStatus?.running ? '保存并恢复普通优选' : '恢复普通自动优选';
    renderAutoPolicyFlow();
  }

  function openAutoPolicy(provider) {
    if (!provider) return;
    const current = clone(provider.auto_policy || {});
    autoPolicyDraft = current.stages?.length ? current : defaultAutoPolicyDraft();
    autoPolicyProviderId = provider.id;
    byId('routing-auto-policy').classList.remove('hidden');
    byId('routing-auto-policy-feedback').textContent = '';
    renderAutoPolicy();
    byId('routing-auto-policy-title')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function validateAutoPolicyDraft() {
    if (!autoPolicyDraft) return '优选策略尚未打开';
    if (autoPolicyDraft.enabled && !autoPolicyDraft.stages.length) return '至少添加一个优选地区';
    const seen = new Set();
    for (const stage of autoPolicyDraft.stages) {
      if (seen.has(stage.region)) return `地区“${nodeTools.REGION_LABELS[stage.region] || stage.region}”重复，请保留一条`;
      if (stage.region === 'OTHER' && !stage.region_keywords.length) return '“其他”地区必须填写地区补充关键词';
      if (stage.selection_mode === 'failure' && !stage.preferred_node) return `${nodeTools.REGION_LABELS[stage.region] || stage.region}使用仅故障切换时必须选择首选节点`;
      if (stage.region_keywords.length > 12 || stage.preferred_keywords.length > 12) return '每类关键词最多填写 12 个';
      if ([...stage.region_keywords, ...stage.preferred_keywords].some(word => word.length > 40)) return '单个关键词不能超过 40 个字符';
      seen.add(stage.region);
    }
    const tolerance = Number(byId('routing-auto-policy-tolerance').value);
    const unit = byId('routing-auto-policy-tolerance-unit').value;
    const maximum = unit === 'percent' ? 100 : 500;
    if (!Number.isInteger(tolerance) || tolerance < 0 || tolerance > maximum) return `延迟切换容差必须是 0～${maximum} 的整数`;
    autoPolicyDraft.latency_tolerance = tolerance;
    autoPolicyDraft.latency_tolerance_unit = unit;
    return '';
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
    const workspacePages = { providers: 'subscriptions', rules: 'rules', nodes: 'nodes' };
    if (workspacePages[name]) {
      activeTab = name;
      if (name === 'nodes') {
        const currentPage = document.querySelector('.page.active')?.id?.replace('page-', '') || 'proxy';
        window.ProxyWorkspace?.openNodes?.(pendingNodeGroup || '', {
          page: ['routing', 'subscriptions', 'rules'].includes(currentPage) ? currentPage : 'proxy',
          tab: 'overview',
        });
      } else {
        void goToPage(workspacePages[name]);
        void loadSetup();
      }
      pendingNodeGroup = '';
      return;
    }
    if (name === 'overview' && !byId('page-routing')?.classList.contains('active')) void goToPage('routing');
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
      [true, 'DNS 配置', routingConfig?.dns_mode === 'advanced'
        ? `高级模式 · ${routingConfig?.dns_enhanced_mode || 'fake-ip'} · ${(routingConfig?.nameserver_policy || []).length} 条域名策略`
        : '简单模式 · 使用内置推荐参数'],
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
    const builtinCount = builtin?.rule_count ?? 0;
    const bypass = routingConfig?.system_proxy_bypass || {};
    const bypassCount = (bypass.domains?.length || 0) + (bypass.processes?.length || 0);
    const hasCnFallback = !!bypass.include_cn_direct;
    const actual = bypassCount + (routingConfig?.traffic_mode === 'global' ? 0 : activeRules.length + builtinCount) + (hasCnFallback ? 1 : 0) + 1;
    const flow = `bypass → user → builtin${hasCnFallback ? ' → GEOIP(CN)' : ''} → MATCH`;
    byId('routing-rule-composition').textContent = `自定义绕过 ${bypassCount} 条 · 用户规则 ${routingConfig?.traffic_mode === 'global' ? 0 : activeRules.length} 条 · 内置规则 ${routingConfig?.traffic_mode === 'global' ? 0 : builtinCount} 条（${routingConfig?.builtin_rule_pack || 'off'}） · 最多 ${actual} 条；顺序固定为 ${flow}，重复项会自动合并。`;
    renderBuiltinRules();
  }

  function cnSystemProxyDomains() {
    const detail = setup?.builtin_rule_packs?.['cn-direct-v1'];
    return Array.isArray(detail?.system_proxy_domains) ? detail.system_proxy_domains : [];
  }

  function renderCnBypassSummary() {
    const target = byId('routing-bypass-cn-direct-summary');
    if (!target) return;
    const detail = setup?.builtin_rule_packs?.['cn-direct-v1'];
    if (detail?.error) {
      target.textContent = `cn-direct-v1 当前不可用：${detail.error}`;
      return;
    }
    const manual = splitMaintenanceLines(byId('routing-bypass-domains')?.value);
    const referenced = routingConfig?.system_proxy_bypass?.include_cn_direct
      ? cnSystemProxyDomains() : [];
    const merged = new Set([...manual, ...referenced].map(item => item.toLocaleLowerCase()));
    target.textContent = routingConfig?.system_proxy_bypass?.include_cn_direct
      ? `已知的 ${merged.size} 个域名后缀直接绕过系统代理；其余域名解析为中国 IP 时也会直连。`
      : `启用后引用 cn-direct-v1 的 ${cnSystemProxyDomains().length} 个域名后缀，并按中国 IP 兜底。`;
  }

  function renderDnsMode() {
    const advanced = (routingConfig?.dns_mode || 'simple') === 'advanced';
    byId('routing-dns-advanced').classList.toggle('hidden', !advanced);
    byId('routing-dns-simple-summary').classList.toggle('hidden', advanced);
    byId('btn-routing-dns-reset').textContent = advanced ? '恢复推荐参数' : '查看推荐参数';
    const fakeIp = (routingConfig?.dns_enhanced_mode || 'fake-ip') === 'fake-ip';
    byId('routing-fake-ip-range').disabled = !fakeIp || busy;
    byId('routing-fake-ip-filter').disabled = !fakeIp || busy;
    ['routing-dns-mode', 'routing-dns-enhanced'].forEach(id => byId(id)?._routingWidget?.refresh());
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
    outboundOptions(byId('routing-default'), routingConfig.default_outbound);
    const allStrategy = byId('routing-proxy-strategy');
    allStrategy.replaceChildren(...strategyOptions().map(item => option(...item)));
    allStrategy.value = routingConfig.proxy_strategy || 'url-test';
    [
      'routing-capture-mode',
      'routing-traffic-mode',
      'routing-interface',
      'routing-proxy-strategy',
      'routing-default',
      'routing-builtin-pack',
    ].forEach(id => enhanceRoutingSelect(byId(id)));
    enhanceRoutingSelect(byId('builtin-rules-type'), { compact: true });
    byId('routing-dns').value = (routingConfig.dns_servers || []).join(', ');
    byId('routing-default-dns').value = (routingConfig.default_nameserver || []).join(', ');
    byId('routing-proxy-dns').value = (routingConfig.proxy_server_nameserver || []).join(', ');
    byId('routing-direct-dns').value = (routingConfig.direct_nameserver || []).join(', ');
    byId('routing-dns-mode').value = routingConfig.dns_mode || 'simple';
    byId('routing-dns-enhanced').value = routingConfig.dns_enhanced_mode || 'fake-ip';
    byId('routing-dns-respect-rules').checked = !!routingConfig.dns_respect_rules;
    byId('routing-fake-ip-range').value = routingConfig.fake_ip_range || DNS_RECOMMENDED.fake_ip_range;
    byId('routing-fake-ip-filter').value = (routingConfig.fake_ip_filter || DNS_RECOMMENDED.fake_ip_filter).join('\n');
    byId('routing-nameserver-policy').value = (routingConfig.nameserver_policy || [])
      .map(item => `${item.domain} = ${(item.servers || []).join(', ')}`).join('\n');
    ['routing-dns-mode', 'routing-dns-enhanced'].forEach(id => enhanceRoutingSelect(byId(id)));
    renderDnsMode();
    const bypass = routingConfig.system_proxy_bypass || {};
    byId('routing-bypass-cn-direct').checked = !!bypass.include_cn_direct;
    byId('routing-bypass-domains').value = (bypass.domains || []).join('\n');
    byId('routing-bypass-processes').value = (bypass.processes || []).join('\n');
    renderCnBypassSummary();
    renderProviders();
    renderAllProxyChoice();
    renderRules();
    renderOverview();
    renderNodes();
    void loadConfigHistory();
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
      providerPreviewErrors.set(provider.id, { signature, message });
      feedback(`订阅获取失败：${message}`, 'error');
      nodeFeedback(`订阅获取失败：${message}；已保留上次节点列表。`, 'error');
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

  async function refreshProvider(providerId, allowFallback = false) {
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
      return true;
    } catch (error) {
      const message = friendlyError(error);
      if (allowFallback) {
        feedback(`常驻核心更新未完成：${message}；正在尝试备用下载路径…`, 'warning');
        nodeFeedback('常驻核心更新未完成，正在尝试备用下载路径；当前节点列表保持不变。', 'warning');
        return false;
      }
      feedback(`更新失败：${message}；已保留当前节点列表。`, 'error');
      nodeFeedback(`更新失败：${message}；已保留当前节点列表。`, 'error');
      toast({ ok: false, msg: message });
      return false;
    } finally { setBusy(false); }
  }

  async function fetchProvider(provider) {
    const state = proxyGroupState(provider.id);
    if (provider.enabled && providerIsSaved(provider) &&
        (state || runtimeStatus?.core_running)) {
      const updated = await refreshProvider(provider.id, true);
      if (updated) return true;
    }
    return previewProvider(provider);
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
        tested_at: job.status === 'completed' ? Math.floor(Date.now() / 1000) : current.tested_at,
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
    const existingCards = new Map([...grid.children]
      .filter(card => card.matches('.routing-node[data-node-key]'))
      .map(card => [card.dataset.nodeKey, card]));
    const renderedCards = new Set();
    nodes.forEach((node, index) => {
      const nodeKey = String(node.name || '');
      const card = existingCards.get(nodeKey) || document.createElement('article');
      card.dataset.nodeKey = nodeKey;
      const { preferenceName, manualSelected, runtimeSelected, selected } = nodeSelection(node);
      const nodeState = node.state || (node.tested ? 'completed' : activeTest ? 'pending' : '');
      card.className = `routing-node${selected ? ' selected' : ''}${nodeState ? ` ${nodeState}` : ''}${node.tested && node.alive === false ? ' offline' : ''}`;
      if (selected) card.setAttribute('aria-current', 'true');
      else card.removeAttribute('aria-current');
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
      action.append(badge, choose); card.replaceChildren(head, meta, action);
      const current = grid.children[index];
      if (current !== card) grid.insertBefore(card, current || null);
      renderedCards.add(card);
    });
    [...grid.children].forEach(card => {
      if (!renderedCards.has(card)) card.remove();
    });
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
    const smartPolicy = provider?.selection_mode === 'auto' && provider?.auto_policy?.enabled;
    autoSelect.textContent = smartPolicy ? '智能优选已启用' : provider?.selection_mode === 'auto' ? '优选策略' : '设置自动优选';
    autoSelect.title = !provider ? '请选择一个具体订阅后设置自动优选' : '配置地区顺序、线路关键词和最终故障回退';
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
      if (source?.auto_policy) target.auto_policy = clone(source.auto_policy);
      target.auto_update = source ? !!source.auto_update : !!target.auto_update;
    }
    if (authoritative?.default_outbound) config.default_outbound = authoritative.default_outbound;
  }

  async function saveProxyPreference(providerId, mode, nodeName = '', policy = null) {
    const provider = providerForGroup(providerId);
    if (!provider || preferenceBusy) return false;
    if (mode === 'manual') {
      const confirmed = await confirmAction({
        title: '确认目标节点',
        message: `将“${nodeName}”保存为“${provider.name}”的手动目标。运行中会回读确认实际选择；该操作不会修改默认出口。`,
        confirmText: '确认使用',
      });
      if (!confirmed) return false;
    }
    preferenceBusy = providerId; renderNodes(); renderProviders();
    const action = mode === 'manual' ? `正在保存目标节点“${nodeName}”…` : '正在启用自动优选…';
    nodeFeedback(action); feedback(action);
    try {
      const result = await backend().save_proxy_preference(
        providerId, mode, nodeName, { ...provider }, policy);
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
      return true;
    } catch (error) {
      const message = `保存代理偏好失败：${friendlyError(error)}`;
      nodeFeedback(message, 'error'); feedback(message, 'error'); toast({ ok: false, msg: message });
      return false;
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

  function providerReferencePlan(providerId, includeDisabledRules = false) {
    const value = `proxy:${providerId}`;
    const provider = (routingConfig.proxy_providers || []).find(item => item.id === providerId);
    const remaining = (routingConfig.proxy_providers || [])
      .filter(item => item.enabled && item.id !== providerId);
    const aggregateBecomesInvalid = !!provider?.enabled && remaining.length === 0;
    const affected = outbound => outbound === value || (aggregateBecomesInvalid && outbound === 'proxy');
    const rules = (routingConfig.rules || []).filter(rule =>
      (includeDisabledRules || rule.enabled !== false) && affected(rule.outbound));
    return {
      count: (affected(routingConfig.default_outbound) ? 1 : 0) + rules.length,
      replacement: remaining.length ? 'proxy' : 'block',
      replacementLabel: remaining.length ? `“全部代理订阅”（剩余 ${remaining.length} 个）` : '“阻止连接”',
      affected,
    };
  }

  function replaceProviderReferences(plan, includeDisabledRules = false) {
    if (plan.affected(routingConfig.default_outbound)) {
      routingConfig.default_outbound = plan.replacement;
    }
    (routingConfig.rules || []).forEach(rule => {
      if ((includeDisabledRules || rule.enabled !== false) && plan.affected(rule.outbound)) {
        rule.outbound = plan.replacement;
      }
    });
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
        : `${providerSelectionLabel(provider)} · ${strategyLabel(provider.strategy)} · ${downloadRouteLabel(provider)} · ${cache?.available ? `${cache.node_count} 条缓存记录` : '暂无节点缓存'} · ${provider.auto_update ? `每 ${Math.round((provider.interval || 3600) / 60)} 分钟自动更新` : '仅手动更新'}`;
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
              : `节点已持久化 · ${provider.selection_mode === 'auto' ? providerSelectionLabel(provider) : '尚未选择目标节点'}`
            : cache?.available
              ? '缓存可用，尚未生成节点快照；可重新获取或临时测速'
              : '尚未生成节点快照，请重新获取订阅'
          : state && !providerIsSaved(provider) ? '运行服务仍使用已应用配置；当前修改尚未应用'
            : state ? `运行中：${selected?.display_name || state.selected || '等待检测'} · ${providerSelectionLabel(provider)}`
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
      fetch.onclick = () => fetchProvider(provider);
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
        const plan = providerReferencePlan(provider.id);
        if (!enabledInput.checked && plan.count) {
          const confirmed = await confirmAction({ title: '停用被引用的订阅', message: `有 ${plan.count} 个生效出口正在引用“${provider.name}”。停用后将自动改为${plan.replacementLabel}，是否继续？`, confirmText: '停用并调整' });
          if (!confirmed) { enabledInput.checked = true; return; }
          replaceProviderReferences(plan);
        }
        provider.enabled = enabledInput.checked; setDirty(); renderProviders(); refreshOutboundEditors();
      };
      enabled.append(enabledInput, document.createElement('i'));
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn mini danger'; remove.textContent = '删除';
      remove.disabled = busy;
      remove.onclick = async () => {
        const plan = providerReferencePlan(provider.id, true);
        const confirmed = await confirmAction({ title: '删除代理订阅', message: plan.count ? `有 ${plan.count} 个出口正在引用“${provider.name}”。删除后将自动改为${plan.replacementLabel}，是否继续？` : `确定删除“${provider.name}”吗？`, confirmText: plan.count ? '删除并调整' : '删除' });
        if (!confirmed) return;
        replaceProviderReferences(plan, true);
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

  function renderBuiltinRules() {
    const list = byId('builtin-rules-list');
    if (!list || !routingConfig) return;
    const packId = routingConfig.builtin_rule_pack || 'off';
    const detail = setup?.builtin_rule_packs?.[packId];
    const rules = Array.isArray(detail?.rules) ? detail.rules : [];
    const query = String(byId('builtin-rules-search')?.value || '').trim().toLocaleLowerCase();
    const typeFilter = byId('builtin-rules-type')?.value || 'all';
    const visible = rules.filter(rule => {
      const kind = String(rule.type || '');
      const typeMatches = typeFilter === 'all'
        || (typeFilter === 'domain' && kind.startsWith('DOMAIN'))
        || (typeFilter === 'ip' && kind.startsWith('IP-'));
      const textMatches = !query || [kind, rule.value, rule.outbound, ...(rule.options || [])]
        .some(value => String(value || '').toLocaleLowerCase().includes(query));
      return typeMatches && textMatches;
    });
    const kindNames = {
      'DOMAIN': '精确域名',
      'DOMAIN-SUFFIX': '域名后缀',
      'DOMAIN-WILDCARD': '域名通配符',
      'IP-CIDR': 'IPv4 网段',
      'IP-CIDR6': 'IPv6 网段',
    };
    list.replaceChildren(...visible.map(rule => {
      const row = document.createElement('div');
      row.className = 'builtin-rule-row';
      row.setAttribute('role', 'listitem');
      const order = document.createElement('span');
      order.className = 'builtin-rule-order';
      order.textContent = String(rule.index || 0).padStart(2, '0');
      const kind = document.createElement('span');
      kind.className = 'builtin-rule-kind';
      kind.textContent = kindNames[rule.type] || rule.type || '未知';
      kind.title = rule.type || '';
      const value = document.createElement('span');
      value.className = 'builtin-rule-value';
      value.textContent = rule.value || '';
      value.title = rule.value || '';
      const outbound = document.createElement('span');
      outbound.className = 'builtin-rule-outbound';
      outbound.textContent = rule.outbound === 'PHYSICAL' ? '物理网络直连' : rule.outbound || '--';
      if (rule.options?.length) {
        const optionText = document.createElement('small');
        optionText.textContent = rule.options.join(', ');
        outbound.append(optionText);
      }
      row.append(order, kind, value, outbound);
      return row;
    }));
    const count = byId('builtin-rules-count');
    count.textContent = visible.length === rules.length
      ? `${rules.length} 条` : `${visible.length} / ${rules.length} 条`;
    byId('builtin-rules-description').textContent = detail?.error
      ? detail.error
      : detail?.description
        ? detail.file_editable
          ? `${detail.description}；来源于程序目录 ${detail.source_file}，修改后重启软件并重新应用配置。`
          : detail.description
        : packId === 'off' ? '当前已关闭本地规则包。'
          : '规则详情尚未加载，请刷新页面后重试。';
    const empty = byId('builtin-rules-empty');
    empty.textContent = detail?.error ? '请修正规则包文件后重新保存并应用配置。'
      : !detail && packId !== 'off' ? '规则详情尚未加载。'
        : rules.length && !visible.length ? '没有符合当前筛选条件的规则。'
          : '当前规则包不包含规则。';
    byId('builtin-rules-search').disabled = !rules.length;
    byId('builtin-rules-type').disabled = !rules.length;
    byId('builtin-rules-type')._routingWidget?.refresh();
    list.previousElementSibling?.classList.toggle('hidden', !rules.length);
    const notice = byId('builtin-rules-mode-notice');
    const globalMode = routingConfig.traffic_mode === 'global';
    notice.classList.toggle('hidden', !globalMode);
    notice.textContent = globalMode
      ? '当前为全局模式：这些规则仍保留在配置中，但本次运行只生成 MATCH，不参与实际匹配。' : '';
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
      const source = match.source === 'bypass' ? `bypass · 安全绕过（${match.rule_domain}）`
        : match.source === 'user' ? `user · 第 ${match.rule_index} 条规则（${match.rule_domain}）`
        : match.source === 'builtin' ? `builtin · ${routingConfig.builtin_rule_pack}（${match.rule_domain}）`
        : match.runtime_geoip_fallback ? 'runtime · GEOIP(CN) 后再进入 MATCH'
          : 'default · MATCH';
      target.replaceChildren();
      const strong = document.createElement('strong'); strong.textContent = match.runtime_geoip_fallback
        ? `${match.domain} → 运行时按目标 IP 判断`
        : `${match.domain} → ${match.outbound_name}`;
      const span = document.createElement('span'); span.textContent = `${source} · ${match.runtime_detail || match.detail}`;
      target.append(strong, span); target.className = `routing-test-result ${match.available ? 'success' : 'warning'}`;
    } catch (error) {
      target.textContent = friendlyError(error); target.className = 'routing-test-result error';
    }
  }

  function collectConfig() {
    return {
      ...routingConfig,
      schema_version: 7,
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
      dns_mode: byId('routing-dns-mode').value,
      dns_enhanced_mode: byId('routing-dns-enhanced').value,
      dns_respect_rules: byId('routing-dns-respect-rules').checked,
      fake_ip_range: byId('routing-fake-ip-range').value.trim(),
      fake_ip_filter: splitMaintenanceLines(byId('routing-fake-ip-filter').value),
      nameserver_policy: parseNameserverPolicy(byId('routing-nameserver-policy').value),
      system_proxy_bypass: {
        lan: true,
        include_cn_direct: byId('routing-bypass-cn-direct').checked,
        domains: splitMaintenanceLines(byId('routing-bypass-domains').value),
        processes: splitMaintenanceLines(byId('routing-bypass-processes').value),
      },
      rules: (routingConfig.rules || []).map(rule => ({ ...rule, enabled: rule.enabled !== false })),
    };
  }

  function parseNameserverPolicy(value) {
    return String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean).map(line => {
      const separator = line.indexOf('=');
      return {
        domain: (separator < 0 ? line : line.slice(0, separator)).trim(),
        servers: (separator < 0 ? '' : line.slice(separator + 1))
          .split(/[\s,]+/).map(item => item.trim()).filter(Boolean),
      };
    });
  }

  function dnsFeedback(message, tone = '') {
    const element = byId('routing-dns-feedback');
    element.textContent = message || '';
    element.className = `form-feedback${tone ? ` ${tone}` : ''}`;
  }

  async function validateDns() {
    if (busy) return;
    dnsFeedback('正在校验 DNS 地址、fake-IP 地址池和域名策略…');
    try {
      const result = await backend().validate_routing_dns(collectConfig());
      if (result?.ok === false) throw new Error(result.msg);
      dnsFeedback(result.msg || 'DNS 配置校验通过。', 'success');
    } catch (error) { dnsFeedback(`校验失败：${friendlyError(error)}`, 'error'); }
  }

  function resetDnsRecommended() {
    Object.assign(routingConfig, clone(DNS_RECOMMENDED));
    if ((routingConfig.dns_mode || 'simple') === 'simple') routingConfig.dns_mode = 'advanced';
    fillDnsFields();
    syncDirty();
    dnsFeedback('已填入推荐参数；保存应用前可再次验证。', 'success');
  }

  function fillDnsFields() {
    byId('routing-dns-mode').value = routingConfig.dns_mode || 'simple';
    byId('routing-dns-enhanced').value = routingConfig.dns_enhanced_mode || 'fake-ip';
    byId('routing-dns-respect-rules').checked = !!routingConfig.dns_respect_rules;
    byId('routing-dns').value = (routingConfig.dns_servers || []).join(', ');
    byId('routing-default-dns').value = (routingConfig.default_nameserver || []).join(', ');
    byId('routing-proxy-dns').value = (routingConfig.proxy_server_nameserver || []).join(', ');
    byId('routing-direct-dns').value = (routingConfig.direct_nameserver || []).join(', ');
    byId('routing-fake-ip-range').value = routingConfig.fake_ip_range || DNS_RECOMMENDED.fake_ip_range;
    byId('routing-fake-ip-filter').value = (routingConfig.fake_ip_filter || []).join('\n');
    byId('routing-nameserver-policy').value = (routingConfig.nameserver_policy || [])
      .map(item => `${item.domain} = ${(item.servers || []).join(', ')}`).join('\n');
    ['routing-dns-mode', 'routing-dns-enhanced'].forEach(id => byId(id)?._routingWidget?.refresh());
    renderDnsMode();
  }

  function splitMaintenanceLines(value) {
    return [...new Set(String(value || '').split(/\r?\n/)
      .map(item => item.trim()).filter(Boolean))];
  }

  function downloadJson(result) {
    if (!result?.ok) throw new Error(result?.msg || '文件生成失败');
    const blob = new Blob([result.content], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = result.filename || 'CXVPN-export.json';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function historySourceLabel(source) {
    return ({
      baseline: '应用前基线', manual: '手动应用', backup_restore: '备份恢复',
      history_restore: '历史回退', desktop_toggle: '托盘/快捷键启停',
      desktop_mode: '托盘/快捷键切换模式', desktop_node: '托盘切换节点',
    })[source] || source || '配置应用';
  }

  function renderConfigHistory(items) {
    const root = byId('routing-config-history');
    root.replaceChildren();
    if (!items?.length) {
      const empty = document.createElement('p');
      empty.className = 'routing-history-empty'; empty.textContent = '尚无应用记录；首次保存应用后会自动建立基线。';
      root.append(empty); return;
    }
    items.forEach(item => {
      const row = document.createElement('div');
      row.className = `routing-history-item${item.success ? '' : ' failed'}`;
      const date = new Date(item.created_at || '');
      const timeText = Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false });
      const title = document.createElement('strong');
      title.textContent = `${item.success ? '应用成功' : '应用失败'} · ${historySourceLabel(item.source)}`;
      const detail = document.createElement('span');
      detail.textContent = `${timeText} · ${item.provider_count || 0} 个订阅 · ${item.rule_count || 0} 条用户规则 · ${item.capture_mode === 'tun' ? 'TUN' : '系统代理'}`;
      row.append(title, detail);
      if (item.restorable) {
        const restore = document.createElement('button');
        restore.type = 'button'; restore.className = 'btn mini ghost'; restore.textContent = '回退';
        restore.onclick = () => restoreHistoryItem(item);
        row.append(restore);
      }
      root.append(row);
    });
  }

  async function loadConfigHistory() {
    if (historyLoading || !byId('routing-config-history')) return;
    historyLoading = true;
    try {
      const result = await backend().get_routing_config_history();
      if (result?.ok === false) throw new Error(result.msg);
      renderConfigHistory(result.items || []);
    } catch (error) {
      const root = byId('routing-config-history'); root.replaceChildren();
      const empty = document.createElement('p'); empty.className = 'routing-history-empty';
      empty.textContent = `历史读取失败：${friendlyError(error)}`; root.append(empty);
    } finally { historyLoading = false; }
  }

  async function restoreHistoryItem(item) {
    if (busy) return;
    const confirmed = await confirmAction({
      title: '回退到历史配置',
      message: `将恢复 ${new Date(item.created_at).toLocaleString('zh-CN', { hour12: false })} 的有效分流配置，并立即重新应用。当前配置会先写入历史，是否继续？`,
      confirmText: '回退并应用', tone: 'notice',
    });
    if (!confirmed) return;
    setBusy(true, 'apply');
    try {
      const result = await backend().restore_routing_config_history(item.id);
      if (result?.ok === false) throw new Error(result.msg);
      feedback(result.msg || '历史配置已恢复。', 'success');
      await loadSetup(true, false, true);
      await loadConfigHistory();
    } catch (error) { feedback(`回退失败：${friendlyError(error)}`, 'error'); }
    finally { setBusy(false); }
  }

  async function exportBackup() {
    if (busy) return;
    try {
      const result = await backend().export_config_backup(byId('routing-backup-urls').checked);
      downloadJson(result);
      feedback(`配置备份已导出；${result.summary?.includes_subscription_urls ? '包含订阅地址，但不含其他凭据' : '不含订阅地址和任何凭据'}。`, 'success');
    } catch (error) { feedback(`备份失败：${friendlyError(error)}`, 'error'); }
  }

  async function exportDiagnostics() {
    if (busy) return;
    try {
      const result = await backend().export_routing_diagnostics();
      downloadJson(result); feedback(`诊断包已导出：${result.summary || '已脱敏'}`, 'success');
    } catch (error) { feedback(`诊断导出失败：${friendlyError(error)}`, 'error'); }
  }

  async function restoreBackupFile(file) {
    if (!file || busy) return;
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error('备份文件不能超过 2 MB');
      const content = await file.text();
      const preview = await backend().preview_config_restore(content);
      if (preview?.ok === false) throw new Error(preview.msg);
      const summary = preview.summary || {};
      const changes = [
        `${summary.provider_count || 0} 个订阅`, `${summary.rule_count || 0} 条用户规则`,
        `${summary.routing_fields?.length || 0} 项路由设置变化`, `${summary.general_fields?.length || 0} 项通用设置变化`,
      ].join('、');
      const warnings = (summary.warnings || []).join('；');
      const confirmed = await confirmAction({
        title: '确认恢复配置备份',
        message: `已校验备份：${changes}。凭据保留当前本机值；${warnings || '代理启用状态保持不变'}。恢复后会立即预检并应用，是否继续？`,
        confirmText: '恢复并应用', tone: 'notice',
      });
      if (!confirmed) return;
      setBusy(true, 'apply');
      const result = await backend().restore_config_backup(content);
      if (result?.ok === false) throw new Error(result.msg);
      feedback(result.msg || '配置备份已恢复。', 'success');
      await loadSetup(true, false, true);
      await loadConfigHistory();
    } catch (error) { feedback(`恢复失败：${friendlyError(error)}`, 'error'); }
    finally { byId('routing-restore-file').value = ''; setBusy(false); }
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
      'routing-node-search', 'btn-routing-discard', 'routing-bypass-domains',
      'routing-bypass-processes', 'routing-bypass-cn-direct',
      'btn-routing-backup', 'btn-routing-restore',
      'btn-routing-diagnostic', 'btn-routing-history-refresh',
      'routing-dns-mode', 'routing-dns-enhanced', 'routing-dns-respect-rules',
      'routing-fake-ip-range', 'routing-fake-ip-filter', 'routing-nameserver-policy',
      'btn-routing-dns-validate', 'btn-routing-dns-reset',
    ].forEach(id => {
      const control = byId(id);
      if (control) control.disabled = value;
    });
    document.querySelectorAll('.routing-page select, .routing-workspace-page select')
      .forEach(select => select._routingWidget?.refresh());
    renderDnsMode();
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

  function acceptApplyResult(result) {
    if (!result?.config) return;
    const committed = clone(result.config);
    (committed.proxy_providers || []).forEach(normalizeProviderDefaults);
    appliedConfig = committed;
    savedConfig = clone(committed);
    runtimeStatus = clone(result.status || runtimeStatus || {});
    setup = {
      ...(setup || {}),
      config: clone(committed),
      status: clone(runtimeStatus),
    };
    if (!dirty) {
      routingConfig = clone(committed);
      fillForm();
      savedSnapshot = snapshot(collectConfig());
    }
    loaded = true;
    lastLoadedAt = Date.now();
    setStatus(runtimeStatus);
    window.ProxyWorkspace?.sync?.();
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
      if (provider) openAutoPolicy(provider);
    };
    byId('btn-routing-auto-policy-close').onclick = closeAutoPolicy;
    byId('routing-auto-policy-enabled').onchange = event => {
      if (!autoPolicyDraft) return;
      autoPolicyDraft.enabled = event.target.checked;
      renderAutoPolicy();
    };
    byId('routing-auto-policy-tolerance').oninput = event => {
      if (!autoPolicyDraft) return;
      autoPolicyDraft.latency_tolerance = Number(event.target.value);
      updateAutoPolicyToleranceHelp();
    };
    byId('routing-auto-policy-tolerance-unit').onchange = event => {
      if (!autoPolicyDraft) return;
      autoPolicyDraft.latency_tolerance_unit = event.target.value;
      const maximum = event.target.value === 'percent' ? 100 : 500;
      autoPolicyDraft.latency_tolerance = Math.min(maximum, Number(autoPolicyDraft.latency_tolerance || 0));
      renderAutoPolicy();
    };
    byId('btn-routing-auto-policy-add').onclick = () => {
      if (!autoPolicyDraft || autoPolicyDraft.stages.length >= 8) return;
      const used = new Set(autoPolicyDraft.stages.map(stage => stage.region));
      const visibleRegions = nodeTools.regionOptions(
        nodeGroups().find(item => item.id === byId('routing-node-group')?.value)?.nodes || [], 20)
        .map(item => item.code).filter(code => code !== 'all');
      const choices = [...visibleRegions, ...Object.keys(nodeTools.REGION_LABELS)];
      const region = choices.find(code => !used.has(code));
      if (!region) return;
      autoPolicyDraft.stages.push({
        region, region_keywords: [],
        preferred_keywords: ['高速专线', 'IPLC', 'IEPL'],
        selection_mode: 'latency', preferred_node: '',
      });
      renderAutoPolicy();
    };
    document.querySelectorAll('[data-auto-fallback]').forEach(button => {
      button.onclick = () => {
        if (!autoPolicyDraft) return;
        autoPolicyDraft.fallback = button.dataset.autoFallback;
        renderAutoPolicy();
      };
    });
    byId('btn-routing-auto-policy-save').onclick = async () => {
      const error = validateAutoPolicyDraft();
      const status = byId('routing-auto-policy-feedback');
      if (error) { status.textContent = error; status.className = 'form-feedback error'; return; }
      const provider = providerForGroup(byId('routing-node-group')?.value || '');
      if (!provider) { status.textContent = '当前订阅已不存在，请刷新后重试'; status.className = 'form-feedback error'; return; }
      if (runtimeStatus?.running) {
        const confirmed = await confirmAction({
          title: autoPolicyDraft.enabled ? '应用智能优选策略' : '恢复普通自动优选',
          message: '保存后将重新加载代理核心以立即应用策略，现有代理连接可能短暂中断。是否继续？',
          confirmText: '保存并应用',
        });
        if (!confirmed) return;
      }
      status.textContent = '正在保存优选策略…'; status.className = 'form-feedback';
      const saveButton = byId('btn-routing-auto-policy-save');
      saveButton.disabled = true;
      try {
        const saved = await saveProxyPreference(provider.id, 'auto', '', clone(autoPolicyDraft));
        if (saved) closeAutoPolicy();
      } finally {
        saveButton.disabled = false;
      }
    };
    byId('routing-node-search').oninput = renderNodes;
    byId('routing-node-alive').onchange = renderNodes;
    byId('routing-node-sort').onchange = renderNodes;
    byId('btn-routing-locate-current').onclick = locateCurrentNode;
    byId('btn-routing-backup').onclick = exportBackup;
    byId('btn-routing-restore').onclick = () => byId('routing-restore-file').click();
    byId('btn-routing-diagnostic').onclick = exportDiagnostics;
    byId('btn-routing-history-refresh').onclick = loadConfigHistory;
    byId('btn-routing-dns-validate').onclick = validateDns;
    byId('btn-routing-dns-reset').onclick = resetDnsRecommended;
    byId('routing-restore-file').onchange = event => restoreBackupFile(event.target.files?.[0]);
    byId('builtin-rules-search').oninput = renderBuiltinRules;
    byId('builtin-rules-type').onchange = renderBuiltinRules;
    enhanceRoutingSelect(byId('routing-node-sort'), { compact: true });
    enhanceRoutingSelect(byId('routing-auto-policy-tolerance-unit'), { compact: true });
    byId('routing-node-group').onchange = () => { nodeRegionFilter = 'all'; closeAutoPolicy(); renderNodes(); };
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
      'routing-default-dns', 'routing-proxy-dns', 'routing-direct-dns',
      'routing-bypass-domains', 'routing-bypass-processes', 'routing-bypass-cn-direct',
      'routing-fake-ip-range',
      'routing-fake-ip-filter', 'routing-nameserver-policy'].forEach(id => {
      const element = byId(id);
      element.addEventListener(id.includes('dns') || id.includes('bypass') || id === 'routing-mixed-port' ? 'input' : 'change', () => {
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
        if (id === 'routing-fake-ip-range') routingConfig.fake_ip_range = element.value.trim();
        if (id === 'routing-fake-ip-filter') routingConfig.fake_ip_filter = splitMaintenanceLines(element.value);
        if (id === 'routing-nameserver-policy') routingConfig.nameserver_policy = parseNameserverPolicy(element.value);
        if (id === 'routing-bypass-domains') routingConfig.system_proxy_bypass = {
          ...(routingConfig.system_proxy_bypass || {}), lan: true, domains: splitMaintenanceLines(element.value),
        };
        if (id === 'routing-bypass-processes') routingConfig.system_proxy_bypass = {
          ...(routingConfig.system_proxy_bypass || {}), lan: true, processes: splitMaintenanceLines(element.value),
        };
        if (id === 'routing-bypass-cn-direct') routingConfig.system_proxy_bypass = {
          ...(routingConfig.system_proxy_bypass || {}), lan: true, include_cn_direct: element.checked,
        };
        syncDirty(); renderCnBypassSummary(); renderOverview(); updateApplyButton();
      });
    });
    byId('routing-dns-mode').onchange = () => {
      routingConfig.dns_mode = byId('routing-dns-mode').value;
      syncDirty(); renderDnsMode();
    };
    byId('routing-dns-enhanced').onchange = () => {
      routingConfig.dns_enhanced_mode = byId('routing-dns-enhanced').value;
      syncDirty(); renderDnsMode();
    };
    byId('routing-dns-respect-rules').onchange = () => {
      routingConfig.dns_respect_rules = byId('routing-dns-respect-rules').checked;
      syncDirty();
    };
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
        auto_policy: {
          enabled: false, stages: [], fallback: 'reject', latency_tolerance: 20,
          latency_tolerance_unit: 'ms',
        },
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
    document.querySelector('#nav [data-page="proxy"]')?.addEventListener(
      'click', () => void loadSetup(false, true, true));
    ['subscriptions', 'rules', 'nodes'].forEach(page => {
      document.querySelector(`#nav [data-page="${page}"]`)?.addEventListener(
        'click', () => void loadSetup());
    });
    window.addEventListener('cxvpn:pagechange', event => {
      const page = event.detail?.page;
      if (page === 'subscriptions') activeTab = 'providers';
      if (page === 'rules') activeTab = 'rules';
      if (page === 'nodes') {
        activeTab = 'nodes';
        renderNodes();
      }
      closeRoutingSelect();
    });
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
    loadForProxyHome: () => loadSetup(false, true, true),
    refresh: (preserveDraft = true) => loadSetup(true, preserveDraft),
    refreshBackground: (preserveDraft = true) => loadSetup(true, preserveDraft, true),
    acceptApplyResult,
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
    enhanceSelect: enhanceRoutingSelect,
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
