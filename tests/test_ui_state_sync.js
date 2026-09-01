const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '..', 'ui', 'app.js'), 'utf8');

function between(start, end) {
  const from = app.indexOf(start);
  const to = app.indexOf(end, from);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return app.slice(from, to);
}

async function testStructuredButtonRestored() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(between('async function runBusy', 'function withTimeout'), context);
  let html = '<span class="icon">x</span><span><strong>修复</strong><small>说明</small></span>';
  const button = {
    disabled: false,
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; },
    set textContent(value) { html = String(value); },
  };
  await context.runBusy(button, '修复中…', async () => {
    assert.equal(html, '修复中…');
    assert.equal(button.disabled, true);
  });
  assert.match(html, /<strong>修复<\/strong>/);
  assert.match(html, /<small>说明<\/small>/);
  assert.equal(button.disabled, false);
}

function testIconConnectionButtonUsesSpinner() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(between('function captureConnectionButton', 'async function disconnectAll'), context);
  const classes = new Set(['connection-icon-action', 'primary']);
  const attributes = new Map([
    ['aria-label', '连接 工作 VPN'],
    ['title', '连接 工作 VPN'],
  ]);
  const button = {
    disabled: false,
    innerHTML: '<svg class="play"></svg>',
    classList: {
      contains(name) { return classes.has(name); },
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
    },
    getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
    setAttribute(name, value) { attributes.set(name, value); },
    removeAttribute(name) { attributes.delete(name); },
    set title(value) { attributes.set('title', value); },
    get title() { return attributes.get('title'); },
  };
  const original = context.captureConnectionButton(button);
  context.setConnectionButtonBusy(button, '正在连接');
  assert.equal(button.disabled, true);
  assert.match(button.innerHTML, /connection-action-spinner/);
  assert.doesNotMatch(button.innerHTML, /连接中/);
  assert.equal(attributes.get('aria-label'), '正在连接');
  context.restoreConnectionButton(button, original);
  assert.equal(button.innerHTML, '<svg class="play"></svg>');
  assert.equal(attributes.get('aria-label'), '连接 工作 VPN');
  assert.equal(classes.has('is-busy'), false);
}

async function testForceRefreshQueuesBehindBackgroundLoad() {
  const calls = [];
  const pending = [];
  const context = {
    vpnLoadPromise: null,
    vpnLoadIncludesForceRefresh: false,
    loadVpns(options) {
      calls.push(options);
      return new Promise(resolve => pending.push(resolve));
    },
  };
  vm.createContext(context);
  vm.runInContext(between('function startVpnLoad', 'async function loadVpns'), context);
  const background = context.startVpnLoad({ forceRefresh: false });
  const forced = context.startVpnLoad({ forceRefresh: true });
  assert.equal(calls.length, 1);
  pending.shift()([]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 2);
  assert.equal(calls[1].forceRefresh, true);
  pending.shift()([]);
  await Promise.all([background, forced]);
  assert.equal(context.vpnLoadPromise, null);
}

function testVpnRowsUpdateWithoutClearingTable() {
  const summary = { textContent: '' };
  const current = {
    dataset: {},
    replaceWith(replacement) { this.replacement = replacement; },
    remove() { throw new Error('current row should be retained'); },
  };
  const tbody = {
    get innerHTML() { return ''; },
    set innerHTML(_) { throw new Error('non-empty refresh must not clear table'); },
    querySelectorAll(selector) {
      return selector === 'tr[data-vpn-name]' ? [current] : [];
    },
    appendChild(row) { this.appended = row; },
  };
  const context = {
    CFG: { vpn_name: '工作 VPN', credential_status: {} },
    $(id) { return id === 'vpn-tbody' ? tbody : summary; },
    renderVpnRow(vpn) {
      return { dataset: {
        vpnName: vpn.name,
        vpnKey: context.vpnRowKey(vpn),
      } };
    },
  };
  vm.createContext(context);
  vm.runInContext(between('function vpnRowKey', 'function renderVpnRow'), context);
  const before = {
    name: '工作 VPN', server: 'vpn.test', type: 'Ikev2',
    status: 'Disconnected',
  };
  current.dataset.vpnName = before.name;
  current.dataset.vpnKey = context.vpnRowKey(before);

  context.renderVpnList([{ ...before, status: 'Connected' }]);

  assert.ok(current.replacement, 'changed VPN row should be replaced in place');
  assert.equal(tbody.appended, current.replacement);
  assert.equal(summary.textContent, '1 个已连接 · 默认：工作 VPN');
}

function testVpnProfilesUseOneGlobalSnapshot() {
  const rendered = [];
  const context = {
    VPN_LIST: [{ name: '旧状态', status: 'Disconnected' }],
    vpnListReady: false,
    renderVpnList(list) { rendered.push(list); },
  };
  vm.createContext(context);
  vm.runInContext(between('function setVpnProfiles', 'function vpnRowKey'), context);
  const snapshot = [{ name: '工作 VPN', status: 'Connected' }];

  assert.equal(context.setVpnProfiles(snapshot), true);
  assert.equal(context.vpnListReady, true);
  assert.equal(context.VPN_LIST[0].status, 'Connected');
  assert.equal(rendered.length, 0, 'hidden page should update store without rendering');

  context.setVpnProfiles(snapshot, { render: true });
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0][0].status, 'Connected');
}

function testSecretToggleUsesEyeIconsAndAccessibleLabels() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(between('function toggleSecret', 'function normalizeResult'), context);
  const classes = new Set();
  const attributes = new Map();
  const input = { type: 'password' };
  const button = {
    dataset: { secretLabel: 'VPN 密码' },
    innerHTML: '',
    title: '',
    classList: {
      toggle(name, enabled) {
        if (enabled) classes.add(name);
        else classes.delete(name);
      },
    },
    setAttribute(name, value) { attributes.set(name, value); },
  };

  context.toggleSecret(input, button);
  assert.equal(input.type, 'text');
  assert.match(button.innerHTML, /M3\.5 15\.5s3-4 8\.5-4/);
  assert.equal(attributes.get('aria-label'), '隐藏 VPN 密码');
  assert.equal(attributes.get('aria-pressed'), 'true');
  assert.equal(classes.has('is-revealed'), true);

  context.toggleSecret(input, button);
  assert.equal(input.type, 'password');
  assert.match(button.innerHTML, /M2\.8 12s3\.2-5 9\.2-5/);
  assert.equal(attributes.get('aria-label'), '显示 VPN 密码');
  assert.equal(attributes.get('aria-pressed'), 'false');
  assert.equal(classes.has('is-revealed'), false);

  input.type = 'text';
  context.concealSecret(input, button);
  assert.equal(input.type, 'password');
  assert.match(button.innerHTML, /M2\.8 12s3\.2-5 9\.2-5/);
}

function testRenewalActionButtonsStaySynchronized() {
  const makeButton = () => {
    const classes = new Set();
    const attributes = new Map();
    return {
      textContent: '', title: '',
      classList: {
        toggle(name, enabled) {
          if (enabled) classes.add(name);
          else classes.delete(name);
        },
      },
      setAttribute(name, value) { attributes.set(name, value); },
      classes,
      attributes,
    };
  };
  const overview = makeButton();
  const browser = makeButton();
  const context = {
    renewalActive: false,
    renewalCancelPending: false,
    $(id) { return id === 'btn-renew' ? overview : browser; },
  };
  vm.createContext(context);
  vm.runInContext(between('function renewalActionButtons', 'async function handleRenewalAction'), context);

  context.renderRenewalActionButtons();
  assert.equal(overview.textContent, '立即处理授权');
  assert.equal(browser.textContent, '执行自动化');
  assert.equal(browser.classes.has('accent'), true);

  context.renewalActive = true;
  context.renderRenewalActionButtons();
  assert.equal(overview.textContent, '中断授权流程');
  assert.equal(browser.textContent, '中断自动化');
  assert.equal(browser.classes.has('danger'), true);

  context.renewalCancelPending = true;
  context.renderRenewalActionButtons();
  assert.equal(overview.textContent, '正在中断…');
  assert.equal(browser.textContent, '正在中断…');
  assert.equal(browser.attributes.get('aria-label'), '正在中断…');
}

function testMissingCredentialsResumeOnlyTheInterruptedConnection() {
  const connectFlow = between('async function connectVpn', 'async function disconnectVpn');
  const credentialFlow = between('async function openCredential', 'async function deleteVpn');
  const saveFlow = between('async function saveCredential', 'async function saveCxSettings');
  assert.match(connectFlow, /retryConnection:\s*true/);
  assert.match(connectFlow, /mode:\s*selectedMode/);
  assert.match(credentialFlow, /options\.retryConnection/);
  assert.match(saveFlow, /const retry = credentialConnectRetry/);
  assert.match(saveFlow, /await connectVpn\(retry\.name, null, retry\)/);
}

Promise.resolve()
  .then(testStructuredButtonRestored)
  .then(testIconConnectionButtonUsesSpinner)
  .then(testForceRefreshQueuesBehindBackgroundLoad)
  .then(testVpnRowsUpdateWithoutClearingTable)
  .then(testVpnProfilesUseOneGlobalSnapshot)
  .then(testSecretToggleUsesEyeIconsAndAccessibleLabels)
  .then(testRenewalActionButtonsStaySynchronized)
  .then(testMissingCredentialsResumeOnlyTheInterruptedConnection)
  .then(() => console.log('ui busy state and forced VPN refresh: ok'));
