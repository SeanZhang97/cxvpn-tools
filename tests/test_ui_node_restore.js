const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const context = { console, setTimeout, clearTimeout,
  document: { getElementById: () => null, addEventListener() {} },
  window: { addEventListener() {} } };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root, 'ui/routing_nodes.js'), 'utf8'), context);
const source = fs.readFileSync(path.join(root, 'ui/routing.js'), 'utf8');
vm.runInContext(source.replace('  window.RoutingWorkspace = {', `
  window.restoreTest = {
    reset() {
      setup = {}; savedConfig = null; loaded = false; latestRoutingRevision = 0; dirty = false;
      providerPreviews.clear();
      fillForm = reapplyTestJobs = setStatus = showConflicts = renderProviders =
        renderAllProxyChoice = renderOverview = renderNodes = () => {};
      collectConfig = () => routingConfig;
      setDirty = value => { dirty = value; };
    },
    hydrateSetup, nodeGroups, providerNodesRecord,
  };
  window.RoutingWorkspace = {`), context);
const test = context.window.restoreTest;
const provider = { id: 'alpha', name: '订阅 e\u0301 🇯🇵', enabled: true,
  url: 'https://example.test/sub', selection_mode: 'auto' };
const nodes = Array.from({ length: 44 }, (_, i) => ({ name: `节点 e\u0301 🇯🇵 ${i}`, tested: true, delay: 30, alive: true }));
const response = (record, groups = [], changedProvider = provider) => ({
  ok: true, config: { enabled: false, proxy_providers: [changedProvider] },
  status: { running: false }, provider_nodes: { alpha: record }, proxy_groups: groups,
});
test.reset();
test.hydrateSetup(response({ nodes, updated_at: 100 }));
assert.equal(test.nodeGroups().find(group => group.id === 'alpha').nodes.length, 44,
  'cold bootstrap restores all local nodes without fetching');
test.hydrateSetup(response({ nodes: [], updated_at: 0 }, [{ id: 'alpha', nodes: [] }]));
assert.equal(test.nodeGroups().find(group => group.id === 'alpha').nodes.length, 44,
  'empty runtime and background snapshot must not erase restored nodes');
test.hydrateSetup(response({ nodes: [], updated_at: 0 }, [], { ...provider, url: 'https://example.test/new' }));
assert.equal(test.providerNodesRecord('alpha').nodes.length, 0,
  'changed URL must not inherit old subscription nodes');
assert.doesNotMatch(source, /backend\(\)\.save_config\(collectConfig\(\)\)/,
  'routing-only payload must not overwrite application config');
console.log('node bootstrap / empty refresh / URL isolation: ok');
