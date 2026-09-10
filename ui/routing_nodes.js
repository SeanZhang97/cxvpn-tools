/* routing_nodes.js - 节点工作台的纯筛选与排序工具 */
(() => {
  const REGION_LABELS = Object.freeze({
    HK: '香港', TW: '台湾', JP: '日本', SG: '新加坡', US: '美国', KR: '韩国',
    GB: '英国', DE: '德国', FR: '法国', CA: '加拿大', AU: '澳大利亚',
    RU: '俄罗斯', IN: '印度', BR: '巴西', CN: '中国', OTHER: '其他',
  });
  const REGION_PRIORITY = Object.freeze([
    'HK', 'TW', 'JP', 'SG', 'US', 'KR', 'GB', 'DE', 'FR', 'CA', 'AU', 'RU', 'IN', 'BR', 'CN', 'OTHER',
  ]);
  const REGION_HINTS = Object.freeze([
    ['HK', /香港|hong\s*kong|\bhkg?\b/i],
    ['TW', /台湾|台灣|taiwan|taipei|台北|\btw\b/i],
    ['JP', /日本|东京|東京|大阪|名古屋|japan|tokyo|osaka|\bjp\b/i],
    ['SG', /新加坡|狮城|獅城|singapore|\bsg\b/i],
    ['US', /美国|美國|洛杉矶|洛杉磯|西雅图|西雅圖|纽约|紐約|united\s*states|los\s*angeles|seattle|new\s*york|\bus\b/i],
    ['KR', /韩国|韓國|首尔|首爾|korea|seoul|\bkr\b/i],
    ['GB', /英国|英國|伦敦|倫敦|united\s*kingdom|london|\buk\b|\bgb\b/i],
    ['DE', /德国|德國|法兰克福|法蘭克福|germany|frankfurt|\bde\b/i],
    ['FR', /法国|法國|巴黎|france|paris|\bfr\b/i],
    ['CA', /加拿大|多伦多|多倫多|canada|toronto|\bca\b/i],
    ['AU', /澳大利亚|澳大利亞|澳洲|悉尼|australia|sydney|\bau\b/i],
    ['RU', /俄罗斯|俄羅斯|莫斯科|russia|moscow|\bru\b/i],
    ['IN', /印度|孟买|孟買|india|mumbai|\bin\b/i],
    ['BR', /巴西|圣保罗|聖保羅|brazil|sao\s*paulo|\bbr\b/i],
    ['CN', /中国|中國|大陆|大陸|china|\bcn\b/i],
  ]);

  function nodeText(node) {
    return String(node?.display_name || node?.name || '未命名节点').trim();
  }

  function normalizedText(value) {
    return String(value || '').normalize('NFKD').replace(/\p{M}/gu, '');
  }

  function flagRegionCode(value) {
    const match = String(value || '').match(/^(?:\[[^\]]+\]\s*)?([\u{1F1E6}-\u{1F1FF}]{2})\s*/u);
    if (!match) return '';
    return [...match[1]].map(char => String.fromCharCode(char.codePointAt(0) - 0x1F1E6 + 65)).join('');
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
    const searchable = normalizedText(raw);
    return REGION_HINTS.find(([, pattern]) => pattern.test(searchable))?.[0] || 'OTHER';
  }

  function regionOptions(nodes, limit = 7) {
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
      .slice(0, Math.max(1, Number(limit) || 7))
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
