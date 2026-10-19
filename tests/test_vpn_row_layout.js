const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
const context = {
  CFG: {}, vpnRowKey: () => '', VpnOrder: { handle: () => ({}) },
  document: { createElement: () => ({ dataset: {}, classList: { toggle() {} }, children: [{ prepend() {} }] }) },
  esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
};
vm.createContext(context);
const rowStart = source.indexOf('function renderVpnRow');
vm.runInContext(source.slice(rowStart, source.indexOf('  const actions =', rowStart)) + 'return row;}', context);
vm.runInContext(source.slice(source.indexOf('function gatewaySummary'), source.indexOf('function findVpnConnectionButton')), context);
for (const ip of ['', '10.0.248.173', '中文e\u0301🇨🇳<test>']) {
  const row = context.renderVpnRow({ name: '测试', server: 'vpn.example.test', type: 'Ikev2',
    ip_address: ip, prefix_length: 32, ipv4_default_gateway: false, ipv6_default_gateway: false });
  assert.equal((row.innerHTML.match(/<br>/g) || []).length, 1, '已连接和未连接均只有一次换行');
  assert.match(row.innerHTML, /vpn-server-detail/);
  assert.match(row.innerHTML, /默认网关：均关闭（分流）/);
  if (ip) assert.ok(row.innerHTML.includes(context.esc(ip) + '/32 · 默认网关'));
  else assert.doesNotMatch(row.innerHTML, / · 默认网关/);
  assert.doesNotMatch(row.innerHTML, /<test>/);
}
console.log('VPN server column two-line layout and escaping: ok');
