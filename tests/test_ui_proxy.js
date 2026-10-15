const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui', 'index.html'), 'utf8');
const proxy = fs.readFileSync(path.join(root, 'ui', 'proxy.js'), 'utf8');
const routing = fs.readFileSync(path.join(root, 'ui', 'routing.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'ui', 'style.css'), 'utf8');
assert.match(proxy, /Array\.isArray\(result\.warnings\)/);
assert.match(proxy, /tone: warnings\.length \? 'warning' : 'success'/);
assert.match(proxy, /result\.msg \|\| message, \.\.\.warnings/);

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
assert.doesNotMatch(proxy, /代理未开启，系统路由已恢复/);
assert.match(proxy, /代理配置应用失败，请检查当前运行状态/);
assert.match(proxy, /status\.configuration_pending/);
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

// 接管与策略控制台：结构、交互与样式必须成套存在，旧模式卡不得回归。
assert.match(html, /class="hero-mode-chips"/);
assert.match(html, /id="hero-chip-capture"/);
assert.match(html, /id="hero-chip-traffic"/);
assert.match(html, /id="hero-chip-outbound"/);
assert.match(html, /class="mode-console" aria-labelledby="mode-console-title"/);
assert.match(html, /id="proxy-mode-badge" role="status"/);
assert.match(html, /data-proxy-seg="capture"/);
assert.match(html, /data-proxy-seg="traffic"/);
assert.match(html, /class="mode-tile" data-value="system-proxy" role="radio"/);
assert.match(html, /class="mode-tile" data-value="tun" role="radio"/);
assert.match(html, /class="mode-tile" data-value="rule" role="radio"/);
assert.match(html, /class="mode-tile" data-value="global" role="radio"/);
assert.match(html, /class="tile-check"/);
assert.match(html, /class="tile-icon"/);
assert.match(html, /class="tile-text"/);
assert.match(html, /class="badge recommend">推荐/);
assert.match(html, /class="badge advanced">高级/);
assert.match(html, /class="mode-risk hidden" id="proxy-tun-notice"/);
assert.match(html, /id="proxy-tun-notice-text"/);
assert.match(html, /class="strip-dot" id="proxy-strip-dot"/);
assert.match(html, /id="proxy-strip-text"/);
assert.doesNotMatch(html, /proxy-mode-card/);
assert.doesNotMatch(html, /data-proxy-mode/);
assert.doesNotMatch(html, /proxy-mode-switch/);
assert.doesNotMatch(html, /proxy-capture-status/);
assert.doesNotMatch(html, /proxy-default-outbound/);
assert.doesNotMatch(html, /proxy-mode-status/);
assert.doesNotMatch(html, /proxy-mode-detail/);
assert.doesNotMatch(html, /proxy-routing-dimensions/);

assert.match(proxy, /function shownModes/);
assert.match(proxy, /function appliedCaptureMode/);
assert.match(proxy, /function renderModeConsole/);
assert.match(proxy, /async function changeCapture/);
assert.match(proxy, /function changeSeg/);
assert.match(proxy, /bindSegKeyboard/);
assert.match(proxy, /selectedCapture/);
assert.match(proxy, /tun_conflicts/);
// TUN 黄色预告只在决策窗口出现：未运行且 TUN 选择待生效；运行中隐藏，避免与红色冲突提示长期并存。
assert.match(proxy, /else if \(pendingCapture && shownCapture === 'tun'\)/);
assert.match(proxy, /检测到其他 TUN/);
assert.match(proxy, /set_routing_enabled\(\s*nextEnabled, nextEnabled \? selectedMode : '', nextEnabled \? selectedCapture : ''\)/);
assert.match(proxy, /set_routing_enabled\(true, '', capture\)/);
assert.match(proxy, /已暂存：/);
assert.match(proxy, /开启代理后生效/);
assert.match(proxy, /有待生效变更/);
assert.match(proxy, /授权并切换/);
assert.match(proxy, /接管方式切换未完成/);
assert.match(proxy, /除首页当前流量模式与接管方式外/);
assert.doesNotMatch(proxy, /setModeUi/);
assert.doesNotMatch(proxy, /data-proxy-mode/);
assert.doesNotMatch(proxy, /bindModeKeyboard/);
assert.doesNotMatch(proxy, /proxy-capture-status/);
assert.doesNotMatch(proxy, /proxy-default-outbound/);
assert.ok(proxy.indexOf('async function changeProxyState') < proxy.indexOf('async function changeMode'));
assert.ok(proxy.indexOf('async function changeMode') < proxy.indexOf('async function changeCapture'));
// 运行中切换规则/全局是轻操作：即时生效，不允许弹确认框打断。
const modeFlow = proxy.slice(proxy.indexOf('async function changeMode'), proxy.indexOf('async function changeCapture'));
assert.doesNotMatch(modeFlow, /confirmAction/);
assert.match(modeFlow, /set_routing_enabled\(true, mode\)/);
// 运行中切换接管方式是重操作：TUN 强确认（danger），回系统代理中确认（notice）。
const captureFlow = proxy.slice(proxy.indexOf('async function changeCapture'), proxy.indexOf('async function openNodes'));
assert.match(captureFlow, /confirmAction/);
assert.match(captureFlow, /tone: 'danger'/);
assert.match(captureFlow, /tone: 'notice'/);
assert.match(captureFlow, /请先关闭对应客户端的 TUN 模式/);
assert.match(captureFlow, /set_routing_enabled\(true, '', capture\)/);

assert.match(css, /\.mode-console/);
assert.match(css, /\.mode-badge\.running/);
assert.match(css, /\.mode-badge\.pending/);
assert.match(css, /\.mode-tiles/);
assert.match(css, /\.mode-tile\.selected/);
assert.match(css, /\.mode-tile\.pending/);
assert.match(css, /\.mode-tile\.disabled/);
assert.match(css, /\.tile-check/);
assert.match(css, /\.tile-icon/);
assert.match(css, /\.badge\.recommend/);
assert.match(css, /\.badge\.advanced/);
assert.match(css, /\.mode-risk\.danger/);
assert.match(css, /\.mode-strip/);
assert.match(css, /\.strip-dot\.on/);
assert.match(css, /\.strip-dot\.pending/);
assert.match(css, /\.hero-mode-chips/);
assert.match(css, /\.hero-chip\.pending/);
assert.doesNotMatch(css, /\.proxy-mode-card/);
assert.doesNotMatch(css, /\.proxy-mode-switch/);
assert.doesNotMatch(css, /\.proxy-routing-dimensions/);

// 运行概览双卡：订阅概览新增节点质量与优选节点；速率图由本地 uPlot 驱动，手绘 SVG 不得回归。
assert.match(html, /<link rel="stylesheet" href="vendor\/uPlot\.min\.css">/);
assert.match(html, /<script src="vendor\/uPlot\.iife\.min\.js"><\/script>/);
assert.ok(html.indexOf('vendor/uPlot.iife.min.js') < html.indexOf('<script src="proxy.js">'));
assert.match(html, /class="node-quality hidden" id="node-quality"/);
assert.match(html, /id="node-quality-summary"/);
assert.match(html, /class="quality-bar" role="img" aria-label="节点延迟质量分布" id="node-quality-bar"/);
assert.match(html, /id="node-quality-legend"/);
assert.match(html, /class="top-nodes hidden" id="top-nodes"/);
assert.match(html, /id="btn-top-nodes-all"/);
assert.match(html, /id="top-nodes-list"/);
assert.match(html, /id="proxy-rate-chart-wrap"/);
assert.match(html, /id="proxy-rate-chart" class="proxy-rate-chart" tabindex="0" role="application"/);
assert.match(html, /class="proxy-rate-empty" id="proxy-rate-empty"/);
assert.match(html, /id="proxy-live-up"/);
assert.match(html, /id="proxy-live-down"/);
assert.match(html, /class="tt-row tt-up"/);
assert.match(html, /class="tt-row tt-down"/);
assert.doesNotMatch(html, /proxy-rate-hover-target/);
assert.doesNotMatch(html, /proxy-upload-chart/);
assert.doesNotMatch(html, /proxy-rate-crosshair/);
assert.doesNotMatch(html, /proxy-rate-legend-up/);
assert.doesNotMatch(html, /<svg id="proxy-rate-chart"/);

assert.match(proxy, /window\.uPlot\.paths\.spline\(\)/);
assert.match(proxy, /function ensureRateChart/);
assert.match(proxy, /function renderNodeQuality/);
assert.match(proxy, /function renderTopNodes/);
assert.match(proxy, /function dashboardNodePool/);
assert.match(proxy, /function qualityBucketOf/);
assert.match(proxy, /function inspectLatestRateSample/);
assert.match(proxy, /setCursor/);
assert.match(proxy, /setData/);
assert.match(proxy, /ResizeObserver/);
assert.match(proxy, /RoutingWorkspace\?\.isMetadataNode/);
assert.match(proxy, /btn-top-nodes-all/);
assert.match(proxy, /代理未开启 · 启动后显示实时速率/);
assert.match(proxy, /正在等待实时速率数据…/);
assert.doesNotMatch(proxy, /ratePath/);
assert.doesNotMatch(proxy, /inspectRateAt/);
assert.doesNotMatch(proxy, /endRateInspection/);
assert.doesNotMatch(proxy, /rateInspection/);
assert.match(routing, /isMetadataNode,/);

assert.match(css, /\.node-quality/);
assert.match(css, /\.quality-bar \.q-fast/);
assert.match(css, /\.quality-bar \.q-untested/);
assert.match(css, /\.quality-legend/);
assert.match(css, /\.top-nodes/);
assert.match(css, /\.top-node-row/);
assert.match(css, /\.top-node-current/);
assert.match(css, /\.proxy-rate-empty/);
assert.match(css, /\.u-cursor-x/);
assert.match(css, /\.proxy-rate-tooltip\.show/);
assert.match(css, /\.proxy-rate-tooltip \.tt-row/);
assert.doesNotMatch(css, /\.proxy-rate-line/);
assert.doesNotMatch(css, /\.proxy-rate-grid/);
assert.doesNotMatch(css, /\.proxy-rate-hover-target/);
assert.doesNotMatch(css, /\.proxy-rate-crosshair/);
assert.doesNotMatch(css, /\.proxy-rate-tooltip\.is-below/);
// 双卡等高：网格拉伸 + 卡片纵向 flex + 订阅条与卡脚自动沉降。
assert.match(css, /\.proxy-dashboard-grid \{\s*grid-template-columns: minmax\(0,\.92fr\) minmax\(0,1\.08fr\);\s*align-items: stretch/);
assert.match(css, /\.proxy-subscription-health \{\s*display: grid;[^}]*margin-top: auto/);
assert.match(css, /\.proxy-traffic-foot \{\s*margin-top: auto/);

// 默认出口快速切换：卡片即切换器，浮层 fixed 定位逃出卡片 overflow 裁剪。
assert.match(html, /class="outbound-switch" id="outbound-switch"/);
assert.match(html, /id="proxy-current-node" class="proxy-current-row outbound-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" aria-controls="outbound-pop"/);
assert.match(html, /id="proxy-outbound-icon"/);
assert.match(html, /class="ob-chev"/);
assert.match(html, /class="ob-spin"/);
assert.match(html, /class="outbound-pop hidden" id="outbound-pop" role="listbox" aria-label="选择默认出口"/);
assert.match(html, /class="ob-opts" id="outbound-options"/);
assert.match(html, /id="btn-outbound-nodes"/);
assert.match(html, /id="btn-outbound-rules"/);
assert.doesNotMatch(html, /默认出口节点/);

assert.match(proxy, /function outboundChoices/);
assert.match(proxy, /function outboundChoiceOf/);
assert.match(proxy, /function renderOutboundOptions/);
assert.match(proxy, /function placeOutboundPop/);
assert.match(proxy, /function setOutboundOpen/);
assert.match(proxy, /async function changeDefaultOutbound/);
assert.match(proxy, /function withTimeout/);
assert.match(proxy, /默认出口切换请求超时/);
assert.match(proxy, /finally \{\s*outboundApplying = false/);
assert.match(proxy, /set_routing_default_outbound/);
assert.match(proxy, /OUTBOUND_ICONS/);
assert.match(proxy, /默认出口已切换：/);
assert.match(proxy, /正在切换出口…/);
assert.match(proxy, /任何代理与 VPN 都不走/);
assert.match(proxy, /阻止连接/);
assert.match(proxy, /window\.addEventListener\('resize', placeOutboundPop\)/);
assert.match(proxy, /openTab\?\.\('rules'\)/);
assert.doesNotMatch(proxy, /默认出口不使用代理节点/);
const apiSource = fs.readFileSync(path.join(root, 'api.py'), 'utf8');
assert.match(apiSource, /def set_routing_default_outbound/);
assert.match(apiSource, /默认出口无效/);
assert.match(apiSource, /'proxy_outbound'/);

assert.match(css, /\.outbound-switch/);
assert.match(css, /\.outbound-pop \{\s*position: fixed; z-index: 3200/);
assert.match(css, /\.ob-opt\.is-current/);
assert.match(css, /\.ob-opt\.kb-focus/);
assert.match(css, /\.ob-pop-foot/);
assert.match(css, /\.ob-chev/);
// .proxy-current-row span（0,1,1）会压过裸 .ob-spin（0,1,0）导致转圈常显，基础规则必须带限定。
assert.match(css, /\.proxy-current-row \.ob-spin \{\s*display: none/);
assert.doesNotMatch(css, /^\s*\.ob-spin \{/m);
assert.match(css, /\.outbound-trigger\.applying \.ob-spin \{\s*display: block/);
assert.match(css, /\.outbound-trigger\.applying \.ob-chev \{\s*display: none/);
assert.match(css, /\.outbound-trigger\.is-block/);
assert.match(css, /\.outbound-trigger\.is-vpn/);

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

// 默认出口快速切换：选项与高级分流默认出口语义对齐，含失效出口兜底。
const outboundSlice = proxy.slice(proxy.indexOf('  function enabledProviders'), proxy.indexOf('  function renderOutboundOptions'));
{
  const context = vm.createContext({});
  vm.runInContext(`${outboundSlice}\nthis.api = { outboundChoices, outboundChoiceOf };`, context);
  const api = context.api;
  const providers = [
    { id: 'alpha', name: '机场A', enabled: true, selection_mode: 'manual', selected_node: '直连-美国05 é 🇺🇸' },
    { id: 'beta', name: '机场B', enabled: true, selection_mode: 'auto' },
    { id: 'gamma', name: '停用', enabled: false },
  ];
  const config = { default_outbound: 'proxy:alpha', proxy_providers: providers };
  const setup = { vpns: [{ name: 'company', status: 'Connected' }, { name: 'old', status: 'Disconnected' }] };
  const choices = api.outboundChoices(config, setup);
  assert.deepEqual([...choices].map(item => item.value),
    ['physical', 'proxy', 'proxy:alpha', 'proxy:beta', 'vpn:company', 'vpn:old', 'block']);
  assert.equal(choices[1].desc, '2 个已启用订阅 · 使用全部订阅策略');
  assert.equal(choices[2].desc, '手动节点 · 直连-美国05 é 🇺🇸');
  assert.equal(choices[3].desc, '自动优选');
  assert.equal(choices[4].desc, 'Windows VPN · 已连接');
  assert.equal(choices[5].desc, 'Windows VPN · 未连接');
  assert.equal(choices[6].desc, '直接拒绝，不回退到其他出口');
  // 当前出口指向已停用订阅时按不可用出口展示，不崩溃也不静默丢失。
  const stale = api.outboundChoiceOf({ default_outbound: 'proxy:gamma', proxy_providers: providers }, setup);
  assert.equal(stale.kind, 'provider');
  assert.match(stale.desc, /不可用/);
  const empty = api.outboundChoiceOf({ default_outbound: '', proxy_providers: [] }, {});
  assert.equal(empty.value, 'physical');
  const vpnCurrent = api.outboundChoiceOf({ default_outbound: 'vpn:company', proxy_providers: [] }, setup);
  assert.equal(vpnCurrent.kind, 'vpn');
  console.log('outbound quick switch choices: ok');
}

