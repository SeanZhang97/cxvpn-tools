const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
let now = Date.UTC(2026, 9, 9, 2, 24, 0);
class ClockDate extends Date {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
}

function eventSurface() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    emit(type, event = {}) { (listeners.get(type) || []).forEach(listener => listener(event)); },
  };
}

const elements = new Map();
function element(id) {
  if (elements.has(id)) return elements.get(id);
  const classes = new Set();
  const attributes = new Map();
  const value = {
    ...eventSurface(), id, dataset: {}, style: {}, textContent: '',
    rect: { left: 100, top: 200, width: 600, height: 112 },
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, enabled) {
        if (enabled === undefined) enabled = !classes.has(name);
        if (enabled) classes.add(name); else classes.delete(name);
      },
    },
    setAttribute(name, content) { attributes.set(name, String(content)); },
    getAttribute(name) { return attributes.get(name); },
    getBoundingClientRect() { return { ...this.rect }; },
  };
  elements.set(id, value);
  return value;
}

const chart = element('proxy-rate-chart');
chart.parentElement = element('chart-wrap');
const target = element('proxy-rate-hover-target');
const tooltip = element('proxy-rate-tooltip');
tooltip.rect = { left: 0, top: 0, width: 146, height: 78 };
tooltip.classList.add('hidden');
const home = element('proxy-home');
home.classList.add('active');
element('page-proxy').classList.add('active');
const telemetrySubscriptions = [];
const state = {
  loaded: true,
  setup: { config: { enabled: true, default_outbound: 'physical', proxy_providers: [] } },
  runtimeStatus: { running: true },
};
const window = {
  ...eventSurface(),
  RoutingWorkspace: { state: () => state, nodeSummary: () => ({}) },
  pywebview: { api: { set_routing_telemetry_active(active) {
    telemetrySubscriptions.push(active);
    return Promise.resolve();
  } } },
};
const document = {
  ...eventSurface(), visibilityState: 'visible',
  getElementById: element,
  querySelector: selector => selector === '[data-proxy-view="home"]' ? home : null,
  querySelectorAll: () => [],
};
const context = vm.createContext({ window, document, Date: ClockDate, console });
vm.runInContext(fs.readFileSync(path.join(root, 'ui/routing_telemetry.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(root, 'ui/proxy.js'), 'utf8'), context);
window.emit('pywebviewready');
const workspace = window.ProxyWorkspace;
const paths = () => ['proxy-upload-chart', 'proxy-download-chart',
  'proxy-upload-chart-fill', 'proxy-download-chart-fill'].map(id => element(id).getAttribute('d'));
const reading = () => ({
  time: element('proxy-rate-tooltip-time').textContent,
  node: element('proxy-rate-tooltip-node').textContent,
  up: element('proxy-rate-tooltip-up-value').textContent,
  down: element('proxy-rate-tooltip-down-value').textContent,
  left: tooltip.style.left, top: tooltip.style.top,
  crosshair: element('proxy-rate-crosshair').getAttribute('x1'),
  upX: element('proxy-rate-hover-up').getAttribute('cx'),
  upY: element('proxy-rate-hover-up').getAttribute('cy'),
});
const hover = ratio => target.emit('mousemove', { clientX: chart.rect.left + chart.rect.width * ratio });
const visible = () => !tooltip.classList.contains('hidden');

// 无采样时不产生空提示；先收集三个具有不同速率的真实接口输入。
hover(0.5);
assert.equal(visible(), false);
workspace.updateTelemetryState({ phase: 'connected' });
const originalNode = '直连-美国05 e\u0301 \u{1F1FA}\u{1F1F8}';
element('proxy-node-name').textContent = originalNode;
workspace.updateTelemetry({ up: 1024, down: 2048 });
now += 20000;
workspace.updateTelemetry({ up: 4096, down: 3072 });
now += 20000;
workspace.updateTelemetry({ up: 8192, down: 6144 });
target.emit('mouseenter', { clientX: chart.rect.left + chart.rect.width * 0.4 });
assert.equal(visible(), true);
const frozenPaths = paths();
const frozenReading = reading();
assert.equal(frozenReading.node, originalNode);
assert.equal(frozenReading.up, '上行 1.00 KB/s');
assert.equal(frozenReading.down, '下行 2.00 KB/s');

// 读数超过 60 秒仍保留原时间、节点与位置，实时数字继续更新。
element('proxy-node-name').textContent = '日本-新节点';
for (let tick = 0; tick < 66; tick++) {
  now += 1000;
  workspace.updateTelemetry({ up: 1024 * (100 + tick), down: 1024 * (200 + tick) });
}
assert.equal(visible(), true, '实时刷新不能关闭正在查看的提示框');
assert.deepEqual(paths(), frozenPaths, '查看期间时间窗与纵轴应保持稳定');
assert.deepEqual(reading(), frozenReading, '旧采样移出实时窗口后仍应能继续阅读');
assert.equal(element('proxy-upload-rate').textContent, '165 KB/s');
assert.equal(element('proxy-download-rate').textContent, '265 KB/s');
hover(0.99);
assert.equal(reading().up, '上行 8.00 KB/s', '移动鼠标应查看固定时间窗内的其他采样');
assert.equal(reading().node, originalNode);

// 缩放只重排浮层；离开后恢复最新曲线，焦点态同样可稳定阅读。
chart.rect.width = chart.parentElement.rect.width = 300;
window.emit('resize');
assert.deepEqual(paths(), frozenPaths);
const left = Number.parseFloat(tooltip.style.left);
assert.ok(left >= 77 && left <= 223);
chart.emit('mouseleave');
assert.equal(visible(), false);
assert.notDeepEqual(paths(), frozenPaths);
target.emit('focus');
assert.equal(visible(), true);
assert.equal(reading().up, '上行 165 KB/s');
const focusReading = reading();
now += 1000;
workspace.updateTelemetry({ up: 777 * 1024, down: 888 * 1024 });
assert.deepEqual(reading(), focusReading);
target.emit('blur');
assert.equal(visible(), false);
hover(1);
assert.equal(reading().up, '上行 777 KB/s');
target.emit('keydown', { key: 'Escape' });
assert.equal(visible(), false);
hover(1);
window.emit('blur');
assert.equal(visible(), false);

// 离开页面、窗口隐藏及通道重连都释放旧快照。
workspace.sync();
hover(1);
element('page-proxy').classList.remove('active');
window.emit('cxvpn:pagechange');
assert.equal(visible(), false);
assert.equal(telemetrySubscriptions.at(-1), false);
element('page-proxy').classList.add('active');
workspace.sync();
hover(1);
document.visibilityState = 'hidden';
document.emit('visibilitychange');
assert.equal(visible(), false);
document.visibilityState = 'visible';
workspace.sync();
hover(1);
workspace.updateTelemetryState({ phase: 'reconnecting' });
assert.equal(visible(), false);
assert.equal(element('proxy-upload-rate').textContent, '0 B/s');
assert.deepEqual(paths().slice(0, 2), ['M0 92L360 92', 'M0 92L360 92']);
hover(1);
assert.equal(visible(), false);

// 左侧边界 x=0 是有效坐标，不能被鼠标位置替代。
workspace.updateTelemetryState({ phase: 'connected' });
workspace.updateTelemetry({ up: 2048, down: 0 });
now += 60000;
hover(0.01);
assert.equal(element('proxy-rate-crosshair').getAttribute('x1'), '0.0');
assert.equal(element('proxy-rate-hover-up').getAttribute('cx'), '0.0');

console.log('proxy rate inspection: stable snapshot, live sampling, resize and lifecycle: ok');
