const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'routing.js'), 'utf8');
const backend = {};
const notices = [];
const feedbacks = [];
const context = { console, setTimeout: () => 1, clearTimeout() {},
  toast: result => notices.push(result),
  normalizeResult: result => result,
  friendlyError: error => error.message,
  recordFeedback: (message, tone) => feedbacks.push({ message, tone }),
  document: { getElementById: () => null, addEventListener() {}, hidden: false },
  window: { addEventListener() {}, pywebview: { api: backend } },
};
vm.createContext(context);
const expose = `
  window.test = {
    initialize() {
      setup = { config: { enabled: false }, status: { running: false }, proxy_groups: [] };
      appliedConfig = clone(setup.config); savedConfig = clone(setup.config);
      routingConfig = clone(setup.config); loaded = true; dirty = true;
      latestRoutingRevision = 0; setupGeneration = 0;
      fillForm = hydrateProviderNodes = reapplyTestJobs = setStatus = showConflicts =
        renderProviders = renderAllProxyChoice = renderOverview = renderNodes = feedback = () => {};
    },
    prepareApply() {
      routingConfig = { enabled: false }; setup = { status: {} }; busy = false;
      collectConfig = () => clone(routingConfig);
      routingNeedsUac = () => false;
      setBusy = value => { busy = value; };
      setDirty = value => { dirty = value; };
      loadSetup = async () => {};
      feedback = recordFeedback;
      acceptApplyResult = result => { routingConfig = clone(result.config); };
    },
    loadSetup, beginMutation, acceptApplyResult, mergeRuntimeGroups, reapplyTestJobs, apply,
    get: () => ({ setup, appliedConfig, latestRoutingRevision }),
  };
`;
vm.runInContext(source.replace('  window.RoutingWorkspace = {', expose + '\n  window.RoutingWorkspace = {'), context);
const api = context.window.test;
const data = value => JSON.parse(JSON.stringify(value));

(async () => {
  api.initialize();
  let resolveOld;
  backend.get_routing_setup = () => new Promise(resolve => { resolveOld = resolve; });
  const previousRead = api.loadSetup(true, true, true);
  api.beginMutation();
  api.acceptApplyResult({ config: { enabled: true }, status: { running: true }, routing_revision: 2 });
  resolveOld({ ok: true, config: { enabled: false }, status: { running: false }, routing_revision: 0 });
  await previousRead;
  assert.equal(api.get().appliedConfig.enabled, true, 'old setup must not undo a successful toggle');
  api.acceptApplyResult({ config: { enabled: false }, routing_revision: 1 });
  assert.equal(api.get().appliedConfig.enabled, true, 'old mutation response must also be rejected');
  api.mergeRuntimeGroups([{ id: 'alpha', selected: 'A', nodes: [{ name: 'A' }, { name: '节点 e\u0301 🇯🇵' }] }]);
  api.mergeRuntimeGroups([{ id: 'alpha', selected: '节点 e\u0301 🇯🇵', partial: true, nodes: [] }]);
  const group = data(api.get().setup.proxy_groups[0]);
  assert.equal(group.selected, '节点 e\u0301 🇯🇵');
  assert.equal(group.nodes.length, 2, 'confirmed selection without overview must preserve node list');
  assert.match(source, /if \(result\?\.unchanged\)/);
  assert.match(source, /card\._contentSignature !== contentSignature/);
  for (const warnings of [[], ['节点待机不可用，代理保持关闭']]) {
    api.prepareApply();
    const msg = '配置已保存，代理保持关闭';
    backend.apply_routing = async () => ({ ok: true, config: { enabled: false }, msg, warnings });
    await api.apply();
    const tone = warnings.length ? 'warning' : 'success';
    const message = [msg, ...warnings].join('；');
    assert.deepEqual(data(notices.at(-1)), { ok: true, tone, msg: message });
    assert.deepEqual(feedbacks.at(-1), { message, tone });
  }
  console.log('routing stale response rejection and confirmed group merge: ok');
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
