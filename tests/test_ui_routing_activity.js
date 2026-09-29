const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui', 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'ui', 'routing_activity.js'), 'utf8');
const workspace = fs.readFileSync(path.join(root, 'ui', 'routing_workspace.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'ui', 'routing_activity.css'), 'utf8');

for (const id of [
  'page-connections', 'connections-body', 'connections-search',
  'proxy-log-panel', 'runtime-log-panel', 'proxy-log-list',
]) assert.match(html, new RegExp(`id="${id}"`));
assert.match(source, /wait_routing_activity/);
assert.match(source, /set_routing_activity_view/);
assert.match(source, /visibilitychange/);
assert.match(source, /close_routing_connection/);
assert.match(source, /RoutingWorkspace\?\.enhanceSelect/);
assert.match(workspace, /routing-panel-providers/);
assert.match(workspace, /routing-panel-rules/);
assert.match(workspace, /routing-nodes-mount/);
assert.match(css, /\.connection-table/);
assert.match(css, /\.proxy-log-list/);

const listeners = {};
const context = {
  window: {
    addEventListener: (name, callback) => { listeners[name] = callback; },
  },
  document: {},
  console,
};
vm.createContext(context);
vm.runInContext(source, context);
const tools = context.window.RoutingActivityTools;
assert.equal(tools.formatBytes(1536), '1.50 KB');
const connections = {
  active: [
    { id: 'a', metadata: { host: 'b.example', process: 'browser' }, upload: 10, download: 5, start: '2026-01-01T01:00:00Z', chains: ['JP'] },
    { id: 'b', metadata: { host: 'a.example', process: 'client' }, upload: 30, download: 5, start: '2026-01-01T02:00:00Z', chains: ['US'] },
  ],
};
assert.deepEqual(
  Array.from(tools.filteredConnections(connections, 'active', 'browser', 'start-desc'), item => item.id),
  ['a']);
assert.deepEqual(
  Array.from(tools.filteredConnections(connections, 'active', '', 'traffic-desc'), item => item.id),
  ['b', 'a']);
assert.equal(tools.chainText({ chains: ['PHYSICAL'] }), '物理网络');
assert.equal(tools.chainText({ chains: ['PHYSICAL', 'VPN-1'] }), '物理网络 → VPN-1');
assert.equal(tools.chainText({ chains: ['VPN-1'] }), 'VPN-1', 'a VPN chain must not be relabeled as physical');
assert.equal(tools.chainText({ chains: [] }), 'DIRECT');
assert.equal(tools.chainText({ chains: ['订阅 PHYSICAL e\u0301 🇯🇵'] }), '订阅 PHYSICAL e\u0301 🇯🇵',
  'only the exact physical adapter marker may be translated');
const fallbackConnections = {
  active: [
    { ...connections.active[0], id: 'physical', chains: ['PHYSICAL'] },
    { ...connections.active[1], id: 'vpn', chains: ['VPN-1'] },
  ],
  closed: [{ ...connections.active[0], id: 'closed-physical', chains: ['PHYSICAL'] }],
};
for (const query of ['物理网络', 'physical', 'PHYSICAL']) {
  assert.deepEqual(
    Array.from(tools.filteredConnections(fallbackConnections, 'active', query, 'start-desc'), item => item.id),
    ['physical'], 'search must accept both the displayed label and raw adapter name');
}
assert.deepEqual(
  Array.from(tools.filteredConnections(fallbackConnections, 'closed', '物理网络', 'start-desc'), item => item.id),
  ['closed-physical']);
const activityElements = new Map();
const activityElement = id => {
  if (!activityElements.has(id)) activityElements.set(id, {
    value: '', classList: { toggle() {} }, closest() { return this; },
  });
  return activityElements.get(id);
};
const renderingContext = {
  document: { getElementById: activityElement },
  window: { addEventListener() {} },
};
vm.createContext(renderingContext);
vm.runInContext(source.replace('  window.RoutingActivityTools = {', `
  window.renderActivityTest = snapshot => { state.connections = snapshot; renderConnections(); };
  window.RoutingActivityTools = {`), renderingContext);
renderingContext.window.renderActivityTest({ active: [{
  ...connections.active[0],
  chains: ['PHYSICAL', '<img src=x onerror=alert(1)>', '节点 e\u0301 🇯🇵'],
}], closed: [] });
const connectionHtml = activityElement('connections-body').innerHTML;
assert.match(connectionHtml, /class="connection-chain" title="PHYSICAL → &lt;img/,
  'tooltip must preserve and escape the raw chain for diagnostics');
assert.match(connectionHtml, />物理网络 → &lt;img src=x onerror=alert\(1\)&gt; → 节点 e\u0301 🇯🇵<\/td>/);
assert.doesNotMatch(connectionHtml, /<img/);
const logs = { items: [
  { type: 'info', payload: 'first' },
  { type: 'error', payload: 'second' },
] };
assert.deepEqual(
  Array.from(tools.filteredLogs(logs, 'error', '', 'newest'), item => item.payload),
  ['second']);
assert.deepEqual(
  Array.from(tools.filteredLogs(logs, 'all', '', 'oldest'), item => item.payload),
  ['second', 'first']);

console.log('routing activity workspace: ok');
