/* routing_nodes.js - 节点工作台的纯筛选与排序工具 */
(() => {
  const REGION_LABELS = Object.freeze(Object.fromEntries(
    Object.entries(window.RoutingRegionCatalog).map(([code, [label]]) => [code, label])));
  const REGION_PRIORITY = Object.freeze([
    'HK', 'TW', 'JP', 'SG', 'US', 'KR', 'GB', 'DE', 'FR', 'CA', 'AU', 'RU', 'IN', 'BR', 'CN', 'OTHER',
  ]);
  const REGION_HINTS = [];
  const REGION_CODES = [];
  Object.entries(window.RoutingRegionCatalog).forEach(([code, [, aliases]]) => {
    aliases.forEach(alias => {
      const text = normalizedText(alias);
      const escaped = text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s*');
      const pattern = /^[\x00-\x7F]+$/.test(text)
        ? `(?:^|[^A-Za-z0-9])${escaped}(?=$|[^A-Za-z0-9])` : escaped;
      const hint = [code, new RegExp(pattern, /^[A-Z]{2}$/.test(text) ? '' : 'i'), text.length];
      (/^[A-Z]{2,3}$/.test(text) ? REGION_CODES : REGION_HINTS).push(hint);
    });
  });
  // 先匹配完整长名称，避免印度抢先命中印度尼西亚、几内亚抢先命中巴布亚新几内亚。
  REGION_HINTS.sort((left, right) => right[2] - left[2]);

  function nodeText(node) {
    return String(node?.display_name || node?.name || '未命名节点').trim();
  }

  function normalizedText(value) {
    return String(value || '').normalize('NFKD').replace(/\p{M}/gu, '');
  }

  function flagRegionCode(value) {
    const match = String(value || '').replace(/^\[[^\]]+\]\s*/, '')
      .match(/([\u{1F1E6}-\u{1F1FF}]{2})/u);
    if (!match) return '';
    const code = [...match[1]].map(char => String.fromCharCode(char.codePointAt(0) - 0x1F1E6 + 65)).join('');
    return REGION_LABELS[code] ? code : '';
  }

  function splitNodeLabel(node) {
    const raw = nodeText(node);
    const match = raw.match(/^(\[[^\]]+\]\s*)?([\u{1F1E6}-\u{1F1FF}]{2})\s*/u);
    if (!match) return { text: raw, country: '' };
    const prefix = match[1] || '';
    return { text: `${prefix}${raw.slice(match[0].length)}`.trim() || raw, country: flagRegionCode(raw) };
  }

  function regionCode(node) {
    const raw = nodeText(node);
    const flag = flagRegionCode(raw);
    if (flag) return flag;
    const searchable = normalizedText(raw.replace(/^\[[^\]]+\]\s*/, ''));
    return REGION_HINTS.find(([, pattern]) => pattern.test(searchable))?.[0]
      || REGION_CODES.find(([, pattern]) => pattern.test(searchable))?.[0] || 'OTHER';
  }

  function regionOptions(nodes) {
    const counts = new Map();
    (nodes || []).forEach(node => {
      const code = regionCode(node);
      counts.set(code, (counts.get(code) || 0) + 1);
    });
    const priority = code => {
      const index = REGION_PRIORITY.indexOf(code);
      return index < 0 ? REGION_PRIORITY.length : index;
    };
    const regions = [...counts.entries()]
      .sort((left, right) => right[1] - left[1] || priority(left[0]) - priority(right[0]) || left[0].localeCompare(right[0]))
      .map(([code, count]) => ({ code, label: REGION_LABELS[code] || code, count }));
    return [{ code: 'all', label: '全部', count: (nodes || []).length }, ...regions];
  }

  function multiplierValue(node) {
    const match = nodeText(node).match(/(?:^|[^\d])(\d+(?:\.\d+)?)\s*(?:倍|x)(?:\D|$)/i);
    const value = match ? Number(match[1]) : Number.POSITIVE_INFINITY;
    return Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
  }

  function delayRank(node) {
    const delay = Number(node?.delay);
    if (node?.alive === true && Number.isFinite(delay) && delay > 0) return [0, delay];
    if (node?.alive === true) return [1, Number.POSITIVE_INFINITY];
    const tested = node?.tested === true || node?.alive === false || (Number.isFinite(delay) && delay > 0);
    return tested ? [3, Number.POSITIVE_INFINITY] : [2, Number.POSITIVE_INFINITY];
  }

  function sortNodes(nodes, mode = 'default', selectedNames = []) {
    const selected = new Set((selectedNames || []).map(value => String(value || '').trim()).filter(Boolean));
    const decorated = (nodes || []).map((node, index) => ({ node, index }));
    const isSelected = node => selected.has(String(node?.name || '').trim())
      || selected.has(String(node?.display_name || '').trim());
    const byName = (left, right) => nodeText(left).localeCompare(nodeText(right), 'zh-CN', { numeric: true, sensitivity: 'base' });
    decorated.sort((left, right) => {
      if (mode === 'current') {
        const selectedDelta = Number(isSelected(right.node)) - Number(isSelected(left.node));
        if (selectedDelta) return selectedDelta;
      }
      if (mode === 'delay') {
        const leftRank = delayRank(left.node);
        const rightRank = delayRank(right.node);
        const delta = leftRank[0] - rightRank[0] || leftRank[1] - rightRank[1];
        if (delta) return delta;
        return byName(left.node, right.node) || left.index - right.index;
      }
      if (mode === 'multiplier') {
        const delta = multiplierValue(left.node) - multiplierValue(right.node);
        if (delta) return delta;
        return byName(left.node, right.node) || left.index - right.index;
      }
      if (mode === 'name') return byName(left.node, right.node) || left.index - right.index;
      return left.index - right.index;
    });
    return decorated.map(item => item.node);
  }

  function trafficBytes(value, unit) {
    const amount = Number(value);
    const exponent = { KB: 1, MB: 2, GB: 3, TB: 4 }[String(unit || '').toUpperCase()];
    return Number.isFinite(amount) && exponent ? amount * (1024 ** exponent) : null;
  }

  function subscriptionMetadata(nodes, now = Date.now()) {
    const result = { items: [], remaining: null, total: null, reset: '', expiry: '', expiryDays: null, tone: '' };
    (nodes || []).forEach(node => {
      const text = nodeText(node);
      let match = text.match(/(?:剩余流量|流量剩余|剩余)\s*[：:]?\s*(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)\b/i);
      if (match && result.remaining === null) {
        result.remaining = trafficBytes(match[1], match[2]);
        result.items.push({ label: '剩余流量', value: `${match[1]} ${match[2].toUpperCase()}` });
      }
      match = text.match(/(?:总流量|流量总计|总计)\s*[：:]?\s*(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)\b/i);
      if (match && result.total === null) result.total = trafficBytes(match[1], match[2]);
      match = text.match(/(?:距离下次重置剩余|下次重置|流量重置)\s*[：:]?\s*(\d+)\s*(天|小时|时)/);
      if (match && !result.reset) {
        result.reset = `${match[1]} ${match[2]}`;
        result.items.push({ label: '距离重置', value: result.reset });
      }
      match = text.match(/(?:套餐到期|有效期)\s*[：:]?\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
      if (match && !result.expiry) {
        result.expiry = `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}`;
        const expiryTime = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
        const current = new Date(now);
        const today = Date.UTC(current.getUTCFullYear(), current.getUTCMonth(), current.getUTCDate());
        result.expiryDays = Math.ceil((expiryTime - today) / 86400000);
        result.items.push({ label: '套餐到期', value: result.expiry });
      }
    });
    let severity = 0;
    if (result.remaining !== null) {
      if (result.total) {
        const ratio = result.remaining / result.total;
        severity = ratio <= .05 ? 2 : ratio <= .15 ? 1 : severity;
      } else {
        severity = result.remaining <= 1024 ** 3 ? 2 : result.remaining <= 5 * 1024 ** 3 ? 1 : severity;
      }
    }
    if (result.expiryDays !== null) severity = Math.max(severity, result.expiryDays <= 7 ? 2 : result.expiryDays <= 30 ? 1 : 0);
    result.tone = severity === 2 ? 'danger' : severity === 1 ? 'warning' : '';
    return result;
  }

  function nodeOwner(node, providers) {
    const matches = (providers || []).filter(provider => node.provider_id
      ? provider.id === node.provider_id
      : String(node.name || '').startsWith(`[${provider.name}] `));
    return matches.length === 1 ? matches[0] : null;
  }

  function aggregateGroup(config, groups) {
    const providers = (config?.proxy_providers || []).filter(item => item.enabled);
    if (!providers.length) return null;
    const runtime = groups.find(item => item.id === 'all');
    const runtimeNodes = new Map((runtime?.nodes || []).map(node => [node.name, node]));
    const nodes = new Map();
    for (const provider of providers) {
      const group = groups.find(item => item.id === provider.id);
      const source = group?.nodes || (runtime?.nodes || []).filter(node => nodeOwner(node, providers)?.id === provider.id);
      for (const node of source) {
        const prefix = `[${provider.name}] `;
        const raw = group?.preview ? String(node.display_name || node.name || '')
          : String(node.name || '').startsWith(prefix) ? node.name.slice(prefix.length)
          : String(node.display_name || '');
        if (!raw) continue;
        const name = prefix + raw;
        const latest = runtimeNodes.get(name);
        const useLatest = latest && (latest.state === 'testing' || latest.state === 'pending'
          || Number(latest.tested_at || 0) > Number(node.tested_at || 0));
        nodes.set(name, { ...node, ...(useLatest ? latest : {}), name, display_name: raw, provider_id: provider.id });
      }
    }
    return { ...runtime, id: 'all', name: '全部代理订阅', nodes: [...nodes.values()],
      strategy: config.aggregate_selection?.mode === 'manual' ? 'select' : config.proxy_strategy || 'url-test',
      preview: !runtime, aggregate: true };
  }

  window.RoutingNodeTools = Object.freeze({
    REGION_LABELS, nodeText, splitNodeLabel, regionCode, regionOptions, multiplierValue, sortNodes,
    subscriptionMetadata, nodeOwner, aggregateGroup,
  });
})();
