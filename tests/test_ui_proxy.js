const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui', 'index.html'), 'utf8');
const proxy = fs.readFileSync(path.join(root, 'ui', 'proxy.js'), 'utf8');
const routing = fs.readFileSync(path.join(root, 'ui', 'routing.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'ui', 'style.css'), 'utf8');

assert.match(html, /data-page="proxy"/);
assert.match(html, /id="page-proxy"/);
assert.match(html, /data-proxy-view="home"/);
assert.match(html, /data-proxy-view="nodes"/);
assert.match(html, /id="btn-proxy-toggle"/);
assert.match(html, /id="btn-proxy-disable"/);
assert.match(html, /id="btn-proxy-nodes"/);
assert.match(html, /id="proxy-selection-warning"/);
assert.match(html, /id="proxy-draft-notice"/);
assert.match(html, /id="proxy-operation-notice"[^>]+role="alert"[^>]+aria-live="assertive"[^>]+aria-atomic="true"/);
assert.match(html, /id="btn-proxy-operation-dismiss"/);
assert.match(html, /id="proxy-subscription-health"/);
assert.match(html, /id="proxy-stream-state"/);
assert.match(html, /id="proxy-service-label" role="status" aria-live="polite" aria-atomic="true"/);
assert.match(html, /id="proxy-state-title" aria-live="polite" aria-atomic="true"/);
assert.match(html, /id="proxy-orbit" class="proxy-orbit" aria-hidden="true"/);
assert.match(html, /class="proxy-emblem" viewBox="0 0 160 160"/);
assert.match(html, /class="proxy-emblem-mark"/);
assert.doesNotMatch(html, /class="proxy-orbit-layer/);
assert.match(html, /class="proxy-status-pair" role="status" aria-live="polite" aria-atomic="true"/);
assert.match(html, /id="proxy-upload-rate"/);
assert.match(html, /id="proxy-download-rate"/);
assert.match(html, /<script src="routing_telemetry\.js"><\/script>/);
assert.match(html, /id="btn-proxy-review-draft"/);
assert.match(html, />可选节点<\/dt><dd id="proxy-node-count"/);
assert.match(html, /id="btn-proxy-choose-node"/);
assert.match(html, /id="routing-node-grid"/);
assert.match(html, /<script src="proxy\.js"><\/script>/);

assert.match(proxy, /refreshBackground/);
assert.match(proxy, /apply_routing/);
assert.match(proxy, /set_routing_enabled/);
assert.match(proxy, /traffic_mode/);
assert.match(proxy, /default_outbound/);
assert.match(proxy, /openNodes/);
assert.match(proxy, /openSubscriptions/);
assert.match(proxy, /function servicePhase/);
assert.match(proxy, /function targetProvider/);
assert.match(proxy, /function providerNodes/);
assert.match(proxy, /function preferenceNodeName/);
assert.match(proxy, /function nodeMatchesPreference/);
assert.match(proxy, /function testAgeLabel/);
assert.match(proxy, /function targetNodeTestSummary/);
assert.match(proxy, /function runtimeUsesPreference/);
assert.match(proxy, /group\.selected === `\[\$\{provider\.name\}\] \$\{selectedNode\}`/);
assert.match(proxy, /nodeMatchesPreference\(item, provider, provider\.selected_node/);
assert.match(proxy, /group\?\.nodes\?\.find\(item => nodeMatchesPreference\(item, provider, provider\.selected_node\)\)\s*\|\|\s*persistedNodes\.find/);
assert.match(proxy, /function selectionGuard/);
assert.match(proxy, /function renderSubscriptionHealth/);
assert.match(proxy, /subscriptionMetadata/);
assert.match(proxy, /function reconcileTelemetry/);
assert.match(proxy, /function renderOperationNotice/);
assert.match(proxy, /代理未开启，系统路由已恢复/);
assert.match(proxy, /set_routing_telemetry_active/);
assert.doesNotMatch(proxy, /get_routing_observability/);
assert.match(proxy, /updateTelemetryState/);
assert.match(proxy, /selection_mode/);
assert.match(proxy, /selected_node/);
assert.match(proxy, /手动模式，请先选择目标节点/);
assert.match(proxy, /不在最新节点清单中/);
assert.match(proxy, /重试启动/);
assert.match(proxy, /state\.runtimeStatus/);
assert.match(proxy, /service_update_required/);
assert.match(proxy, /proxyOrbit\?\.classList\.toggle\('is-active', running\)/);
assert.match(proxy, /tone: 'notice'/);
assert.match(proxy, /function effectiveConfigSummary/);
assert.match(proxy, /高级分流未保存草稿不会自动生效/);
assert.match(proxy, /本次不会带入这些草稿/);
assert.match(proxy, /btn-proxy-review-draft/);
assert.match(proxy, /正在启动代理…/);
const toggleFlow = proxy.slice(
  proxy.indexOf('async function changeProxyState'),
  proxy.indexOf('async function changeMode'));
assert.doesNotMatch(toggleFlow, /await latestSetup\(\)/);
assert.match(toggleFlow, /set_routing_enabled/);
assert.doesNotMatch(toggleFlow, /正在快速|快速开启|快速关闭/);
assert.match(proxy, /acceptApplyResult/);
assert.match(proxy, /refreshBackground/);
assert.doesNotMatch(toggleFlow, /latest\.status\?\.service_backend/);
assert.match(proxy, /\['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'\]/);
assert.match(routing, /window\.RoutingWorkspace/);
assert.match(routing, /openNodes: \(groupId = ''\)/);
assert.match(routing, /useDraft \? routingConfig : appliedConfig, groupId, true/);
assert.match(routing, /data-page="proxy"/);
assert.match(routing, /loadForProxyHome: \(\) => loadSetup\(false, true, true\)/);
assert.match(proxy, /loadForProxyHome/);
assert.doesNotMatch(proxy, /setOperationNotice\(failureTitle, detail\);\s*toast\(\{ ok: false, msg: detail \}\)/);

assert.match(css, /\.proxy-connect-card/);
assert.match(css, /@keyframes proxy-emblem-rise/);
assert.match(css, /@keyframes proxy-emblem-ignite/);
assert.match(css, /\.proxy-orbit\.is-active \.proxy-emblem-mark/);
assert.match(css, /@keyframes proxy-emblem-feed-soft/);
assert.match(css, /\.proxy-dashboard-grid/);
assert.match(css, /\.proxy-subpage-heading/);
assert.match(css, /\.proxy-selection-warning/);
assert.match(css, /\.proxy-draft-notice/);
assert.match(css, /\.proxy-operation-notice/);
assert.match(css, /\.proxy-subscription-health/);
assert.match(css, /\.proxy-stream-state/);
assert.match(css, /\.proxy-traffic-foot/);

console.log('network proxy home and node page: ok');

// 页面切换和异步加载会按默认出口刷新首页，不能覆盖明确点击的订阅。
const vm = require('node:vm');
const nodeNavigation = proxy.slice(proxy.indexOf('  async function openNodes('), proxy.indexOf('  function openHome('));
async function checkNodeNavigation(requested, fallback) {
  const opened = [];
  const changes = [];
  const select = {
    options: ['alpha', 'beta', 'all'].map(value => ({ value })),
    value: 'all',
    dispatchEvent(event) { changes.push([event.type, this.value]); },
  };
  const context = vm.createContext({
    selectedGroupId: fallback, returnTarget: null,
    byId: () => select, setText() {}, syncNodeStats() {},
    Event: class { constructor(type) { this.type = type; } },
    async goToPage(page) {
      assert.equal(page, 'nodes');
      context.selectedGroupId = 'beta';
    },
    window: { RoutingWorkspace: {
      async load() {
        await Promise.resolve();
        context.selectedGroupId = 'all';
      },
      openNodes(id) { opened.push(id); },
    } },
  });
  vm.runInContext(nodeNavigation, context);
  await context.openNodes(requested, { page: 'subscriptions' });
  const expected = requested || fallback;
  assert.deepEqual(opened, [expected]);
  assert.equal(select.value, expected);
  assert.deepEqual(changes, [['change', expected]]);
  assert.equal(context.returnTarget.page, 'subscriptions');
}
(async () => {
  await checkNodeNavigation('alpha', 'beta');
  await checkNodeNavigation('beta', 'alpha');
  await checkNodeNavigation('all', 'alpha');
  await checkNodeNavigation('', 'alpha');
  console.log('subscription node navigation survives page and loading refresh: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
