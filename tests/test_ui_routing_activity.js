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
