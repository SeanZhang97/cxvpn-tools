const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = { window: {}, console, queueMicrotask, document: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../ui/vpn_order.js'), 'utf8'), context);
const order = context.window.VpnOrder;
const plain = value => JSON.parse(JSON.stringify(value));
const names = ['中文 e\u0301 🇨🇳', 'B', 'A'];
assert.deepEqual(plain(order.sort(names.map(name => ({ name })), ['B', names[0]])).map(x => x.name), ['B', names[0], 'A']);
assert.deepEqual(plain(order.sort([{ name: 'B', status: 'Connected' }, { name: 'A' }], [])),
  [{ name: 'A' }, { name: 'B', status: 'Connected' }]);
assert.deepEqual(plain(order.move(['A', 'B', 'C'], 'C', 'A', false)), ['C', 'A', 'B']);
assert.deepEqual(plain(order.move(['A', 'B', 'C'], 'A', 'C', true)), ['B', 'C', 'A']);

function harness(save) {
  const handlers = {};
  const rows = ['A', 'B', 'C'].map(name => {
    const row = { dataset: { vpnName: name }, classList: { add() {}, remove() {} },
      getBoundingClientRect: () => ({ top: 0, height: 40 }) };
    const button = { setAttribute() {}, focus() {}, closest: () => row };
    row.querySelector = () => button;
    row.closest = () => row;
    row.button = button;
    return row;
  });
  const root = {
    addEventListener(name, handler) { handlers[name] = handler; },
    querySelectorAll() { return rows; }, querySelector() { return null; },
    insertBefore(row) { rows.splice(rows.indexOf(row), 1); rows.push(row); },
    contains() { return true; },
  };
  let ready = true;
  const errors = [];
  let refreshes = 0;
  const controller = order.attach(root, { ready: () => ready, save,
    error: error => errors.push(error), refresh: () => { refreshes++; } });
  function key(name, key) {
    handlers.keydown({ key, preventDefault() {}, target: { closest: () => rows.find(row => row.dataset.vpnName === name).button } });
  }
  return { handlers, rows, controller, errors, key, names: () => rows.map(row => row.dataset.vpnName),
    setReady(value) { ready = value; }, refreshes: () => refreshes };
}
async function run() {
  let complete;
  const saved = [];
  const h = harness(names => { saved.push(plain(names)); return new Promise(resolve => { complete = resolve; }); });
  h.setReady(false); h.key('A', 'Enter'); assert.equal(h.controller.active(), false);
  h.setReady(true); h.key('A', 'Enter'); h.key('A', 'End');
  assert.deepEqual(h.names(), ['B', 'C', 'A']);
  h.key('A', 'Escape'); assert.deepEqual(h.names(), ['A', 'B', 'C']);
  assert.equal(saved.length, 0);
  h.key('C', ' '); h.key('C', 'Home'); h.key('C', 'Enter');
  assert.equal(h.controller.active(), true, '保存中仍阻止状态刷新重排');
  const otherPage = harness(async () => {});
  otherPage.key('A', 'Enter');
  assert.equal(otherPage.controller.active(), false, '另一页不得在保存期间开启排序');
  assert.deepEqual(saved, [['C', 'A', 'B']]);
  complete(); await new Promise(setImmediate);
  assert.equal(h.controller.active(), false);
  assert.deepEqual(h.names(), ['C', 'A', 'B']);
  otherPage.key('A', 'Enter');
  assert.equal(otherPage.controller.active(), true);
  otherPage.key('A', 'Escape');
  const failed = harness(async () => { throw new Error('write failed'); });
  failed.key('A', 'Enter'); failed.key('A', 'ArrowDown'); failed.key('A', 'Enter');
  await new Promise(setImmediate);
  assert.deepEqual(failed.names(), ['A', 'B', 'C']);
  assert.equal(failed.errors.length, 1);
  assert.equal(failed.controller.active(), false);
  const dragSaved = [];
  const drag = harness(async names => dragSaved.push(plain(names)));
  const transfer = { setData() {}, setDragImage() {} };
  drag.handlers.dragstart({ target: { closest: () => drag.rows[2].button }, dataTransfer: transfer, preventDefault() {} });
  drag.handlers.drop({ target: drag.rows[0], clientY: 2, preventDefault() {} });
  drag.handlers.dragend();
  await new Promise(setImmediate);
  assert.deepEqual(dragSaved, [['C', 'A', 'B']]);
  assert.deepEqual(drag.names(), ['C', 'A', 'B']);
  const app = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
  assert.match(app, /list = VpnOrder.sort\(list, CFG.vpn_order\)/);
  assert.match(app, /if \(root._vpnOrder.active\(\)\) return/);
  assert.match(app, /if \(tbody._vpnOrder.active\(\)\) return/);
  assert.match(app, /row.children\[0\].prepend\(VpnOrder.handle\(vpn.name\)\)/);
  assert.match(app, /if \(overview._vpnRefresh\) overview._vpnRefresh\(\)/);
  assert.doesNotMatch(app, /if \(left.name === CFG.vpn_name\) return -1/);
  console.log('VPN order sort, keyboard, drag, save and rollback: ok');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
