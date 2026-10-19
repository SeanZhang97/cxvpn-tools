const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '..', 'ui', 'app.js'), 'utf8');
function source(start, end) {
  return app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
}
function element() {
  return {
    dataset: {}, className: '', textContent: '', value: '', disabled: false,
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, replaceChildren() {}, focus() {},
  };
}
const fields = new Map();
const context = {
  CFG: { vpn_name: '', credential_status: {} },
  document: { createElement: element },
  $(id) {
    if (!fields.has(id)) fields.set(id, element());
    return fields.get(id);
  },
  concealSecret() {}, setFeedback() {}, setConnectionButtonBusy() {},
  connectionNameBoxChildren() { return []; },
};
vm.createContext(context);
vm.runInContext(source('function openModal', 'async function syncVpnUi'), context);
vm.runInContext(source('function connectionActionSvg', 'function createEmptyConnectionSlot'), context);

const name = '机房 e\u0301 🇨🇳';
function view(profile) {
  return { profile, name: profile.name, connected: false, busy: false,
    actionText: '连接', meta: '未连接', action() {} };
}
const preview = view({ name, server: '', type: '', pending_system_check: true });
const [action, more] = context.connectionActionButtons(preview);
assert.equal(more.disabled, true, '系统配置未就绪时不能编辑预览对象');
const children = {
  '[data-vpn-action]': action,
  '.connection-icon-action.more': more,
  '.connection-status-dot': element(),
  '.active-connection-name': element(),
  '.active-connection-meta': element(),
};
const row = { className: '', querySelector(selector) { return children[selector]; } };

function assertForm(profile) {
  more.onclick();
  assert.equal(context.$('m-name').value, name);
  assert.equal(context.$('m-server').value, profile.server);
  assert.equal(context.$('m-type-btn').textContent, profile.type);
  assert.equal(context.ddValue, profile.type);
  assert.equal(context.$('m-ipv4-gateway').checked, profile.ipv4_default_gateway);
  assert.equal(context.$('m-ipv6-gateway').checked, profile.ipv6_default_gateway);
  assert.equal(context.$('m-name').disabled, true);
}
const initial = { name, server: 'vpn.example.test', type: 'Ikev2',
  ipv4_default_gateway: false, ipv6_default_gateway: true };
context.updateConnectionRow(row, view(initial));
assert.equal(more.disabled, false);
assertForm(initial);

// 连接状态未变时会命中提前返回，配置及回调仍必须更新。
const changed = { ...initial, server: 'new.example.test', type: 'L2tp',
  ipv4_default_gateway: true, ipv6_default_gateway: false };
context.updateConnectionRow(row, view(changed));
assertForm(changed);
context.updateConnectionRow(row, preview);
assert.equal(more.disabled, true);
const [, directEdit] = context.connectionActionButtons(view(changed));
assert.equal(directEdit.disabled, false);
directEdit.onclick();
assert.equal(context.$('m-server').value, changed.server);
context.openModal(null);
assert.equal(context.$('m-name').value, '');
assert.equal(context.$('m-server').value, '');
assert.equal(context.ddValue, 'Automatic');
assert.equal(context.$('m-ipv4-gateway').checked, true);
assert.equal(context.$('m-ipv6-gateway').checked, true);
console.log('overview VPN edit regression passed');
