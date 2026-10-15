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

function makeNode(tag = 'div') {
  const classes = new Set();
  const attributes = new Map();
  return {
    ...eventSurface(), tag, children: [], dataset: {}, style: {}, textContent: '',
    className: '', title: '', tabIndex: 0, disabled: false,
    clientWidth: 560, offsetWidth: 0, offsetHeight: 0,
    rect: { left: 100, top: 200, width: 600, height: 172 },
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, enabled) {
        if (enabled === undefined) enabled = !classes.has(name);
        if (enabled) classes.add(name); else classes.delete(name);
      },
    },
    append(...kids) { this.children.push(...kids); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = [...kids]; },
    setAttribute(name, content) { attributes.set(name, String(content)); },
    getAttribute(name) { return attributes.get(name); },
    getBoundingClientRect() { return { ...this.rect }; },
  };
}

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const node = makeNode();
    node.id = id;
    elements.set(id, node);
  }
  return elements.get(id);
}

// uPlot 替身：捕获配置与数据，模拟鼠标/键盘驱动的 cursor 钩子。
function MockUPlot(opts, data, container) {
  this.opts = opts;
  this.data = data;
  this.container = container;
  this.cursor = { left: null, top: null, idx: null };
  this.over = element('uplot-over');
  this.ctx = {};
  this.bbox = { top: 0, height: 172 };
  this.sizes = [];
  MockUPlot.instance = this;
}
MockUPlot.paths = { spline: () => 'spline-path' };
MockUPlot.prototype.setData = function setData(data) { this.data = data; };
MockUPlot.prototype.setSize = function setSize(size) { this.sizes.push(size); };
MockUPlot.prototype.valToPos = function valToPos(value, scale) {
  if (scale === 'x') {
    const times = this.data[0] || [];
    const base = times.length ? times[0] : value;
    return (value - base) * 10 + 8;
  }
  return 40;
};
MockUPlot.prototype.setCursor = function setCursor(cursor) {
  this.cursor = { ...this.cursor, ...cursor };
  const left = this.cursor.left;
  const times = this.data[0] || [];
  if (left == null || left < 0 || !times.length) {
    this.cursor.idx = null;
  } else {
    let best = 0;
    times.forEach((time, index) => {
      if (Math.abs(this.valToPos(time, 'x') - left)
        < Math.abs(this.valToPos(times[best], 'x') - left)) best = index;
    });
    this.cursor.idx = best;
  }
  this.opts.hooks.setCursor.forEach(fn => fn(this));
};
MockUPlot.prototype.fireCursor = function fireCursor(idx, left, top) {
  this.cursor = { left, top, idx };
  this.opts.hooks.setCursor.forEach(fn => fn(this));
};

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
  uPlot: MockUPlot,
  RoutingWorkspace: {
    state: () => state,
    nodeSummary: () => ({}),
    isMetadataNode: node => /剩余|套餐|到期|官网/.test(String(node?.display_name || node?.name || '')),
  },
  pywebview: { api: { set_routing_telemetry_active(active) {
    telemetrySubscriptions.push(active);
    return Promise.resolve();
  } } },
};
const document = {
  ...eventSurface(), visibilityState: 'visible',
  getElementById: element,
  createElement: tag => makeNode(tag),
  createTextNode: text => ({ textContent: String(text) }),
  querySelector: selector => (selector === '[data-proxy-view="home"]' ? home : null),
  querySelectorAll: () => [],
};
const context = vm.createContext({ window, document, Date: ClockDate, console });
vm.runInContext(fs.readFileSync(path.join(root, 'ui/routing_telemetry.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(root, 'ui/proxy.js'), 'utf8'), context);
window.emit('pywebviewready');
const workspace = window.ProxyWorkspace;

const tooltip = element('proxy-rate-tooltip');
const empty = element('proxy-rate-empty');
const container = element('proxy-rate-chart');
const visible = () => tooltip.classList.contains('show');
const reading = () => ({
  time: element('proxy-rate-tooltip-time').textContent,
  node: element('proxy-rate-tooltip-node').textContent,
  up: element('proxy-rate-tooltip-up-value').textContent,
  down: element('proxy-rate-tooltip-down-value').textContent,
  left: tooltip.style.left,
  top: tooltip.style.top,
});

// 无采样时不建提示；连接后创建 uPlot 实例并展示空态文案。
container.emit('focus');
assert.equal(visible(), false);
workspace.updateTelemetryState({ phase: 'connected' });
const chart = MockUPlot.instance;
assert.ok(chart, '连接后应创建 uPlot 实例');
const rows = () => [...chart.data].map(row => [...row]);
assert.deepEqual(rows(), [[], [], []]);
assert.equal(empty.classList.contains('hidden'), false);
assert.equal(empty.textContent, '正在等待实时速率数据…');
assert.equal(chart.opts.height, 172);
assert.equal(chart.opts.legend.show, false);
assert.equal(chart.opts.series[1].stroke, '#fbbf72');
assert.equal(chart.opts.series[2].stroke, '#22d3ee');
assert.equal(chart.opts.series[1].paths, 'spline-path');
assert.deepEqual([...chart.opts.scales.y.range(null, 0, 2048)], [0, 2048 * 1.18]);

// 采样对齐写入三条数组；节点名含组合字符与区域指示符（国旗）必须原样保留。
const originalNode = '直连-美国05 e\u0301 \u{1F1FA}\u{1F1F8}';
element('proxy-node-name').textContent = originalNode;
workspace.updateTelemetry({ up: 1024, down: 2048 });
now += 20000;
workspace.updateTelemetry({ up: 4096, down: 3072 });
now += 20000;
workspace.updateTelemetry({ up: 8192, down: 6144 });
assert.equal(chart.data[0].length, 3);
assert.deepEqual([...chart.data[1]], [1024, 4096, 8192]);
assert.deepEqual([...chart.data[2]], [2048, 3072, 6144]);
assert.equal(chart.data[0][2] - chart.data[0][0], 40);
assert.equal(empty.classList.contains('hidden'), true);
assert.equal(element('proxy-upload-rate').textContent, '8.00 KB/s');
assert.equal(element('proxy-download-rate').textContent, '6.00 KB/s');
assert.equal(element('proxy-live-up').textContent, '8.00 KB/s');
assert.equal(element('proxy-live-down').textContent, '6.00 KB/s');

// 悬停：十字线钩子驱动详情浮层，时间与速率按采样点取值。
chart.fireCursor(1, 100, 50);
assert.equal(visible(), true);
assert.match(reading().time, /^\d{2}:\d{2}:\d{2}$/);
assert.equal(reading().node, originalNode);
assert.equal(reading().up, '上行 4.00 KB/s');
assert.equal(reading().down, '下行 3.00 KB/s');
assert.equal(reading().left, '114px');
assert.equal(reading().top, '68px');

// 右缘自动翻转：浮层不溢出图表区域。
chart.fireCursor(0, 560, 30);
assert.equal(reading().up, '上行 1.00 KB/s');
assert.equal(reading().left, '376px');
assert.equal(reading().top, '48px');

// 游标离开（idx/left 为空）即关闭浮层。
chart.fireCursor(null, -1, -1);
assert.equal(visible(), false);

// 键盘：聚焦查看最新采样，Escape / blur / 窗口失焦都关闭。
container.emit('focus');
assert.equal(visible(), true);
assert.equal(reading().up, '上行 8.00 KB/s');
assert.equal(reading().node, originalNode);
container.emit('keydown', { key: 'Escape' });
assert.equal(visible(), false);
container.emit('focus');
assert.equal(visible(), true);
container.emit('blur');
assert.equal(visible(), false);
container.emit('focus');
window.emit('blur');
assert.equal(visible(), false);

// 实时窗口只保留 60 秒：持续推流后数据定长且实时数字更新。
for (let tick = 0; tick < 66; tick += 1) {
  now += 1000;
  workspace.updateTelemetry({ up: 1024 * 100, down: 1024 * 200 });
}
assert.equal(chart.data[0].length, 61);
assert.equal(element('proxy-live-up').textContent, '100 KB/s');
assert.equal(element('proxy-live-down').textContent, '200 KB/s');
assert.equal(element('proxy-upload-rate').textContent, '100 KB/s');

// 断连清空图表与历史，空态回归，实时数归零。
chart.fireCursor(1, 100, 50);
assert.equal(visible(), true);
workspace.updateTelemetryState({ phase: 'reconnecting' });
assert.equal(visible(), false);
assert.deepEqual(rows(), [[], [], []]);
assert.equal(element('proxy-upload-rate').textContent, '0 B/s');
assert.equal(element('proxy-live-up').textContent, '0 B/s');
assert.equal(empty.classList.contains('hidden'), false);
assert.equal(empty.textContent, '代理未开启 · 启动后显示实时速率');
chart.fireCursor(0, 100, 50);
assert.equal(visible(), false);

// 恢复连接后继续采样。
workspace.updateTelemetryState({ phase: 'connected' });
workspace.updateTelemetry({ up: 2048, down: 0 });
assert.equal(chart.data[0].length, 1);
assert.equal(element('proxy-live-down').textContent, '0 B/s');

// 离开页面、窗口隐藏都会退订遥测并关闭浮层。
workspace.sync();
element('page-proxy').classList.remove('active');
window.emit('cxvpn:pagechange');
assert.equal(telemetrySubscriptions.at(-1), false);
element('page-proxy').classList.add('active');
workspace.sync();
assert.equal(telemetrySubscriptions.at(-1), true);
document.visibilityState = 'hidden';
document.emit('visibilitychange');
assert.equal(telemetrySubscriptions.at(-1), false);
document.visibilityState = 'visible';
workspace.sync();

// 订阅概览：节点质量分桶与优选节点来自持久化节点（元数据条目被过滤）。
state.setup = {
  config: {
    enabled: true,
    default_outbound: 'proxy:p1',
    capture_mode: 'system-proxy',
    traffic_mode: 'rule',
    rules: [],
    business_rules: [],
    system_proxy_bypass: {},
    proxy_providers: [{
      id: 'p1', name: '机场A', enabled: true,
      selection_mode: 'manual',
      selected_node: '直连-美国05 e\u0301 \u{1F1FA}\u{1F1F8}',
    }],
  },
  provider_nodes: {
    p1: {
      updated_at: 1,
      nodes: [
        { name: '[机场A] 日本 03', display_name: '日本 03', delay: 42, alive: true, tested: true },
        { name: '[机场A] 直连-美国05', display_name: '直连-美国05 e\u0301 \u{1F1FA}\u{1F1F8}', delay: 150, alive: true, tested: true },
        { name: '[机场A] 新加坡 01', display_name: '新加坡 01', delay: 300, alive: true, tested: true },
        { name: '[机场A] 欧洲 02', display_name: '欧洲 02', delay: 600, alive: true, tested: true },
        { name: '[机场A] 故障 01', display_name: '故障 01', delay: 0, alive: false, tested: true },
        { name: '[机场A] 无延迟 01', display_name: '无延迟 01', delay: 0, alive: true, tested: true },
        { name: '[机场A] 新节点 01', display_name: '新节点 01' },
        { name: '剩余流量：100GB', display_name: '剩余流量：100GB' },
      ],
    },
  },
  status: { running: true },
};
state.groups = [];
state.runtimeStatus = { running: true };
workspace.sync();

const quality = element('node-quality');
assert.equal(quality.classList.contains('hidden'), false);
assert.equal(element('node-quality-summary').textContent, '4 / 7 可用');
const bar = element('node-quality-bar');
assert.equal(bar.children.length, 5);
assert.equal(bar.children[0].className, 'q-fast');
assert.equal(bar.children[0].style.flexGrow, String(1 / 7));
assert.equal(bar.children[3].className, 'q-bad');
assert.equal(bar.children[3].style.flexGrow, String(2 / 7));
assert.equal(bar.getAttribute('aria-label'), '节点延迟质量分布：4/7 可用');
const legend = element('node-quality-legend');
assert.equal(legend.children.length, 5);
assert.deepEqual(legend.children.map(item => item.children.at(-1).textContent), ['1', '1', '2', '2', '1']);

const tops = element('top-nodes-list');
assert.equal(tops.children.length, 3);
const [first, second, third] = tops.children;
assert.equal(first.children[0].textContent, '1');
assert.equal(first.children[1].textContent, '日本 03');
assert.equal(first.children.at(-1).textContent, '42 ms');
assert.equal(first.children.length, 3);
assert.equal(second.children[1].textContent, '直连-美国05 e\u0301 \u{1F1FA}\u{1F1F8}');
assert.equal(second.children[1].title, '直连-美国05 e\u0301 \u{1F1FA}\u{1F1F8}');
assert.equal(second.children.length, 4);
assert.equal(second.children[2].className, 'top-node-current');
assert.equal(second.children[2].textContent, '使用中');
assert.equal(second.children.at(-1).textContent, '150 ms');
assert.equal(third.children[1].textContent, '新加坡 01');
assert.equal(typeof second.onclick, 'function');
assert.equal(typeof element('btn-top-nodes-all').onclick, 'function');

console.log('proxy uPlot rate chart, tooltip lifecycle, node quality and top nodes: ok');
