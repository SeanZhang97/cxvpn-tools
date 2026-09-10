const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'routing.js'), 'utf8');
let backendCalls = 0;
const context = {
  console, setTimeout, clearTimeout,
  document: { getElementById: () => null, addEventListener() {} },
  window: { addEventListener() {}, pywebview: { api: {
    save_proxy_preference() { backendCalls++; },
  } } },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'ui', 'routing_nodes.js'), 'utf8'), context);
vm.runInContext(source.replace('  window.RoutingWorkspace = {', `
  window.selectionTest = {
    initialize(config, groups, running = true) {
      setup = { proxy_groups: groups };
      appliedConfig = clone(config); routingConfig = clone(config);
      runtimeStatus = { running }; preferenceBusy = '';
      nodeFeedback = () => {};
    },
    setDraftOutbound(value) { routingConfig.default_outbound = value; },
    nodeSelectionState, saveProxyPreference, nodeGroups, mergePreferenceIntoConfig,
    aggregateProviderIsPinned, preferredNodeGroupId,
  };
  window.RoutingWorkspace = {`), context);
const api = context.window.selectionTest;
const raw = '中文 e\u0301 🇯🇵';
const alpha = { id: 'alpha', name: '订阅甲', enabled: true, selection_mode: 'manual', selected_node: raw };
const beta = { ...alpha, id: 'beta', name: '订阅乙' };
const a = { name: `[${alpha.name}] ${raw}`, display_name: raw };
const b = { name: `[${beta.name}] ${raw}`, display_name: raw };
const groups = [{ id: 'all', selected: b.name }, { id: 'alpha', selected: a.name }, { id: 'beta', selected: b.name }];
const config = { default_outbound: 'proxy', proxy_providers: [alpha, beta] };
const state = (node, id, provider, preview = false) => api.nodeSelectionState(node, { id, preview }, provider);

(async () => {
  api.initialize(config, groups);
  assert.equal(state(a, 'alpha', alpha).manualSelected, true);
  assert.equal(state(b, 'beta', beta).manualSelected, true);
  assert.equal(state(a, 'alpha', alpha).runtimeSelected, true);
  assert.equal(state(a, 'alpha', alpha).defaultSelected, false);
  assert.equal(state(b, 'beta', beta).defaultSelected, true);
  assert.equal(state(b, 'all', null).defaultSelected, true);
  api.setDraftOutbound('proxy:alpha');
  assert.equal(state(a, 'alpha', alpha).defaultSelected, false, 'unsaved default must not replace applied route');
  assert.equal(state({ name: raw, display_name: raw }, 'alpha', alpha, true).defaultSelected, false);
  assert.equal(state({ name: raw, display_name: raw }, 'beta', beta, true).defaultSelected, true,
    'same raw node name in two providers must retain provider identity');

  api.initialize({ ...config, default_outbound: 'proxy:alpha' }, groups);
  assert.equal(state(a, 'alpha', alpha).defaultSelected, true);
  assert.equal(state(b, 'beta', beta).defaultSelected, false);
  for (const outbound of ['physical', 'block', 'vpn:公司']) {
    api.initialize({ ...config, default_outbound: outbound }, groups);
    assert.equal(state(a, 'alpha', alpha).defaultSelected, false);
    assert.equal(state(b, 'beta', beta).defaultSelected, false);
  }
  api.initialize(config, groups, false);
  assert.equal(state(b, 'beta', beta).defaultSelected, false);
  assert.equal(state(b, 'beta', beta).runtimeSelected, false);
  assert.equal(state(b, 'beta', beta).manualSelected, true);
  api.initialize(config, []);
  assert.equal(state({ name: raw }, 'alpha', alpha, true).runtimeSelected, false,
    'persisted preview selection is not runtime evidence');

  api.initialize({ ...config, proxy_providers: [{ ...alpha, enabled: false }] }, groups);
  assert.equal(await api.saveProxyPreference('alpha', 'manual', raw), false);
  assert.equal(backendCalls, 0, 'disabled selection must not submit a mutation');
  console.log('routing subscription selection and default outlet states: ok');
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
