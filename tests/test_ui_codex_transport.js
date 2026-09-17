const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '..', 'ui', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'ui', 'index.html'), 'utf8');

function between(start, end) {
  const from = app.indexOf(start);
  const to = app.indexOf(end, from);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return app.slice(from, to);
}

function createContext(context) {
  Object.assign(context, {
    codexOperation: '', codexLastState: {}, codexRefreshSequence: 0,
    codexBaseDirty: false, codexKeyDirty: false,
  });
  vm.createContext(context);
  vm.runInContext(between('function updateCodexControls', 'function renderCodexToggle'), context);
}

function transportElements() {
  return {
    'codex-wss-enabled': { checked: false, indeterminate: false, disabled: false },
    'codex-transport-summary': { textContent: '' },
    'codex-transport-detail': { textContent: '' },
    'codex-transport-result': { textContent: '', className: '' },
  };
}

function testCodexTransportControlExists() {
  assert.match(html, /id="codex-wss-enabled"[^>]*type="checkbox"/);
  assert.match(html, /id="codex-transport-summary"/);
  assert.match(html, /id="codex-transport-result"[^>]*aria-live="polite"/);
  assert.match(html, /id="codex-api-base-url"/);
  assert.match(html, /id="codex-api-key"[^>]*type="password"/);
  assert.match(html, /id="btn-codex-api-save"[^>]*>保存并启用</);
  assert.match(html, /id="btn-codex-api-restore"[^>]*>还原原配置</);
  assert.match(html, /id="btn-codex-restart"[^>]*>重启 Codex</);
  assert.match(html, /是否加密取决于 API 地址是否为 HTTPS/);
  for (const id of ['codex-api-base-url', 'codex-api-key', 'codex-mixed-port', 'codex-config-path']) {
    assert.ok(html.includes(`for="${id}"`));
  }
  const codexPage = html.slice(html.indexOf('<section id="page-codex"'), html.indexOf('<section id="page-browser"'));
  assert.ok(codexPage.indexOf('btn-codex-toggle') < codexPage.indexOf('<h3>模型请求传输'));
  assert.equal((codexPage.match(/id="btn-codex-toggle"/g) || []).length, 1);
  assert.doesNotMatch(codexPage, /<select/);
}

function testTransportRenderingUsesTriState() {
  const elements = transportElements();
  const context = {
    codexTransportBusy: false,
    $(id) { return elements[id]; },
  };
  createContext(context);
  vm.runInContext(between('function renderCodexTransport', 'async function refreshCodexPage'), context);

  context.renderCodexTransport({
    transport_mode: 'wss_preferred', websocket_enabled: true,
    transport_conflict: false, active_provider: 'openai',
  });
  assert.equal(elements['codex-wss-enabled'].checked, true);
  assert.equal(elements['codex-wss-enabled'].indeterminate, false);
  assert.equal(elements['codex-wss-enabled'].disabled, false);
  assert.match(elements['codex-transport-summary'].textContent, /当前传输方式：优先 WebSocket/);
  assert.equal(
    elements['codex-transport-detail'].textContent,
    '当前 Provider：openai。此开关仅影响模型请求的传输方式；修改后需重启 Codex 生效。');

  context.renderCodexTransport({
    transport_mode: 'custom_provider', websocket_enabled: null,
    transport_conflict: false, transport_restore_available: true,
    active_provider: 'cm',
  });
  assert.equal(elements['codex-wss-enabled'].indeterminate, true);
  assert.equal(elements['codex-wss-enabled'].disabled, false);
  assert.match(elements['codex-transport-summary'].textContent, /cm/);
  assert.match(elements['codex-transport-summary'].textContent, /恢复/);

  context.renderCodexTransport({
    transport_mode: 'wss_preferred', websocket_enabled: true,
    transport_conflict: false, active_provider: 'codex_local_access',
    custom_api_managed: true,
  });
  assert.match(
    elements['codex-transport-detail'].textContent,
    /修改 codex_local_access 的 supports_websockets/);
}

async function testTransportSubmitsExplicitTargetAndRereadsState() {
  const elements = transportElements();
  const calls = [];
  const context = {
    codexTransportBusy: false,
    $(id) { return elements[id]; },
    api() {
      return { async set_codex_websocket(enabled) {
        calls.push(enabled);
        assert.equal(elements['codex-wss-enabled'].disabled, true);
        return { ok: false, warning: '冲突' };
      } };
    },
    toast() {},
    friendlyError(error) { return error.message; },
    async refreshCodexPage() { calls.push('refresh'); },
  };
  createContext(context);
  vm.runInContext(
    between('function setCodexTransportFeedback', 'function setCodexActionFeedback'),
    context);

  await context.setCodexTransport(false);

  assert.deepEqual(calls, [false, 'refresh']);
  assert.equal(context.codexTransportBusy, false);
  assert.match(elements['codex-transport-result'].textContent, /冲突/);
}

function testCustomApiRenderingShowsManagedState() {
  const elements = {
    'codex-api-summary': { textContent: '' },
    'codex-api-detail': { textContent: '' },
    'codex-api-base-url': { value: '' },
    'codex-api-key': { value: '', placeholder: '' },
    'btn-codex-api-save': { disabled: false },
    'btn-codex-api-restore': { disabled: true },
  };
  const context = {
    codexApiBusy: false,
    document: { activeElement: null },
    $(id) { return elements[id]; },
  };
  createContext(context);
  vm.runInContext(between('function renderCodexCustomApi', 'async function refreshCodexPage'), context);

  context.renderCodexCustomApi({
    custom_api_managed: true,
    custom_api_active: true,
    custom_api_restore_available: true,
    custom_api_base_url: 'https://api.example.test/v1',
    custom_api_key_configured: true,
    custom_api_key_stored: true,
    custom_api_openai_auth_required: true,
    custom_api_migration_required: false,
    custom_api_conflict: false,
    transport_snapshot_exists: false,
    transport_managed: false,
  });

  assert.equal(elements['codex-api-base-url'].value, 'https://api.example.test/v1');
  assert.equal(elements['codex-api-key'].value, '');
  assert.match(elements['codex-api-key'].placeholder, /正在读取已保存/);
  assert.equal(elements['btn-codex-api-save'].disabled, false);
  assert.equal(elements['btn-codex-api-restore'].disabled, false);
  assert.match(elements['codex-api-summary'].textContent, /当前 Provider/);
  assert.match(elements['codex-api-summary'].textContent, /登录账号会保留/);
  assert.match(elements['codex-api-detail'].textContent, /已保存到 CXVPNTools/);
  assert.match(elements['codex-api-detail'].textContent, /模型请求使用自定义 API Key/);
  assert.match(elements['codex-api-detail'].textContent, /留空可沿用密钥/);
  context.renderCodexCustomApi({
    custom_api_managed: true, custom_api_active: true,
    custom_api_openai_auth_required: true, transport_managed: true,
  });
  assert.match(elements['codex-api-summary'].textContent, /自定义 API 已启用/);

  context.renderCodexCustomApi({
    custom_api_managed: true,
    custom_api_active: true,
    custom_api_restore_available: true,
    custom_api_base_url: 'https://api.example.test/v1',
    custom_api_key_configured: true,
    custom_api_openai_auth_required: false,
    custom_api_conflict: false,
    transport_snapshot_exists: false,
    transport_managed: false,
  });
  assert.match(elements['codex-api-summary'].textContent, /再次保存并重启即可升级/);

  context.renderCodexCustomApi({
    custom_api_managed: false,
    custom_api_active: false,
    custom_api_restore_available: false,
    custom_api_base_url: '',
    custom_api_key_configured: false,
    custom_api_conflict: false,
    transport_snapshot_exists: true,
    transport_managed: true,
  });
  assert.equal(elements['btn-codex-api-save'].disabled, false);
  assert.match(elements['codex-api-summary'].textContent, /自动衔接/);
}

async function testCustomApiSaveClearsKeyAndRereadsState() {
  const elements = {
    'codex-api-base-url': { value: 'http://192.0.2.20:53142/v1' },
    'codex-api-key': { value: 'private-test-key' },
    'btn-codex-api-save': { disabled: false },
    'btn-codex-api-restore': { disabled: false },
    'btn-codex-api-eye': {},
    'codex-api-result': { textContent: '', className: '' },
  };
  const calls = [];
  const context = {
    codexApiBusy: false,
    $(id) { return elements[id]; },
    api() {
      return { async save_codex_api_config(baseUrl, apiKey) {
        calls.push([baseUrl, apiKey]);
        return { ok: true, changed: true };
      } };
    },
    concealSecret(input) { calls.push('conceal'); input.type = 'password'; },
    toast() {},
    friendlyError(error) { return error.message; },
    async refreshCodexPage() { calls.push('refresh'); },
  };
  createContext(context);
  vm.runInContext(
    between('function setCodexApiFeedback', 'function setCodexActionFeedback'),
    context);

  await context.saveCodexCustomApi();

  assert.deepEqual(calls, [
    ['http://192.0.2.20:53142/v1', 'private-test-key'],
    'conceal',
    'refresh',
  ]);
  assert.equal(elements['codex-api-key'].value, '');
  assert.equal(context.codexApiBusy, false);
  assert.match(elements['codex-api-result'].textContent, /保留登录账号/);
  assert.match(elements['codex-api-result'].textContent, /模型请求使用自定义 API/);
}

async function testCustomApiKeyIsEchoedAsPassword() {
  const elements = {
    'codex-api-key': { value: '', type: 'password' },
    'btn-codex-api-eye': {},
  };
  const calls = [];
  const context = {
    codexApiKeyBusy: false,
    document: { activeElement: null },
    $(id) { return elements[id]; },
    api() {
      return { async read_codex_api_key() {
        calls.push('read');
        return { ok: true, key_configured: true, api_key: 'private-echo-key' };
      } };
    },
    concealSecret(input) { calls.push('conceal'); input.type = 'password'; },
  };
  createContext(context);
  vm.runInContext(
    between('function renderCodexCustomApi', 'async function refreshCodexPage'),
    context);

  await context.refreshCodexApiKey({ custom_api_key_configured: true });

  assert.equal(elements['codex-api-key'].value, 'private-echo-key');
  assert.equal(elements['codex-api-key'].type, 'password');
  assert.deepEqual(calls, ['read', 'conceal']);
  assert.equal(context.codexApiKeyBusy, false);
}

async function testRestartCodexRequiresConfirmationAndShowsResult() {
  const elements = {
    'btn-codex-restart': { disabled: false, textContent: '重启 Codex' },
    'codex-action-result': { className: '', textContent: '' },
  };
  const calls = [];
  const context = {
    $(id) { return elements[id]; },
    async confirmAction(options) {
      calls.push(['confirm', options.title]);
      return true;
    },
    api() {
      return { async restart_codex() {
        calls.push('restart');
        assert.equal(elements['btn-codex-restart'].disabled, true);
        return { ok: true, msg: 'Codex 已重新启动' };
      } };
    },
    toast(result) { calls.push(['toast', result.msg]); },
    friendlyError(error) { return error.message; },
    async refreshCodexPage() {},
  };
  createContext(context);
  vm.runInContext(
    between('async function restartCodexApp', 'function bindCodexPage'),
    context);

  await context.restartCodexApp();

  assert.deepEqual(calls, [
    ['confirm', '重启 Codex'],
    'restart',
    ['toast', 'Codex 已重新启动'],
  ]);
  assert.equal(elements['btn-codex-restart'].disabled, false);
  assert.equal(elements['btn-codex-restart'].textContent, '重启 Codex');
  assert.equal(elements['codex-action-result'].textContent, 'Codex 已重新启动');
}

function fullContext(overrides = {}) {
  const elements = {};
  for (const id of ['btn-codex-toggle', 'btn-codex-api-save', 'btn-codex-api-restore',
    'btn-codex-restart', 'codex-api-base-url', 'codex-api-key', 'btn-codex-api-eye',
    'codex-sync-summary', 'codex-sync-detail', 'codex-mixed-port', 'codex-config-path',
    'codex-api-summary', 'codex-api-detail', 'codex-api-result', 'codex-proxy-result',
    'codex-action-result', ...Object.keys(transportElements())]) {
    elements[id] = {value: '', textContent: '', disabled: false, type: 'password'};
  }
  const context = {
    $(id) { return elements[id]; },
    document: {activeElement: null},
    toast() {}, friendlyError(error) { return error.message; },
    concealSecret(input) { input.type = 'password'; },
    async confirmAction() { return true; },
    ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(between('let codexViewText', 'function bindSecretToggles'), context);
  return {context, elements};
}

async function testDraftsSurviveRefreshAndLateKeyResponse() {
  let finishRead;
  const {context, elements} = fullContext({api() { return {
    read_codex_api_key: () => new Promise(resolve => { finishRead = resolve; }),
  }; }});
  const pending = context.refreshCodexApiKey({custom_api_key_configured: true});
  vm.runInContext('codexBaseDirty = true; codexKeyDirty = true;', context);
  elements['codex-api-base-url'].value = 'https://draft.example/v1';
  elements['codex-api-key'].value = '';
  context.renderCodexCustomApi({custom_api_base_url: 'https://saved.example/v1'});
  finishRead({ok: true, key_configured: true, api_key: 'test-late-key'});
  await pending;
  assert.equal(elements['codex-api-base-url'].value, 'https://draft.example/v1');
  assert.equal(elements['codex-api-key'].value, '');
}

async function testSaveSerializesAllMutationsAndPreservesFailedDraft() {
  let finishSave;
  let saves = 0;
  let restarts = 0;
  const {context, elements} = fullContext({api() { return {
    save_codex_api_config: () => { saves++; return new Promise(resolve => { finishSave = resolve; }); },
    restart_codex: async () => { restarts++; },
    get_codex_status: async () => ({custom_api_base_url: 'https://old.example/v1', websocket_enabled: false}),
  }; }});
  vm.runInContext('codexBaseDirty = true; codexKeyDirty = true;', context);
  elements['codex-api-base-url'].value = 'https://draft.example/v1';
  elements['codex-api-key'].value = 'test-draft-key';
  const pending = context.saveCodexCustomApi();
  for (const id of ['btn-codex-restart', 'btn-codex-api-restore', 'codex-wss-enabled', 'codex-api-key']) {
    assert.equal(elements[id].disabled, true);
  }
  await context.saveCodexCustomApi();
  await context.restartCodexApp();
  assert.equal(saves, 1);
  assert.equal(restarts, 0);
  finishSave({ok: false, warning: '模拟冲突'});
  await pending;
  assert.equal(elements['codex-api-base-url'].value, 'https://draft.example/v1');
  assert.equal(elements['codex-api-key'].value, 'test-draft-key');
  assert.equal(elements['btn-codex-restart'].disabled, false);
  assert.equal(elements['btn-codex-api-save'].disabled, false);
}

async function testStaleRefreshAndFailureDisableWrites() {
  const reads = [];
  const {context, elements} = fullContext({api() { return {
    get_codex_status: () => new Promise((resolve, reject) => reads.push({resolve, reject})),
  }; }});
  const first = context.refreshCodexPage();
  const second = context.refreshCodexPage();
  reads[1].resolve({custom_api_base_url: 'https://new.example/v1', websocket_enabled: true});
  await second;
  reads[0].resolve({custom_api_base_url: 'https://old.example/v1'});
  await first;
  assert.equal(elements['codex-api-base-url'].value, 'https://new.example/v1');
  const failed = context.refreshCodexPage();
  reads[2].reject(new Error('offline'));
  await failed;
  assert.equal(elements['btn-codex-api-save'].disabled, true);
  assert.match(elements['codex-api-summary'].textContent, /状态读取失败/);
}

async function testRestartPartialFailureIsWarningAndRestoreExplainsScope() {
  let confirmation;
  const {context, elements} = fullContext({
    async confirmAction(options) { confirmation = options.message; return true; },
    api() { return {
      restart_codex: async () => ({ok: false, app_started: true, warning: 'Codex 已启动，但同步失败'}),
      restore_codex_api_config: async () => ({ok: true, changed: true}),
      get_codex_status: async () => ({websocket_enabled: true}),
    }; },
  });
  await context.restartCodexApp();
  assert.equal(elements['codex-action-result'].className, 'form-feedback warning');
  assert.equal(elements['codex-action-result'].textContent, 'Codex 已启动，但同步失败');
  await context.restoreCodexCustomApi();
  assert.match(confirmation, /不是逐条恢复/);
  assert.match(elements['codex-api-result'].textContent, /已恢复原 Provider/);
}

async function main() {
  testCodexTransportControlExists();
  testTransportRenderingUsesTriState();
  await testTransportSubmitsExplicitTargetAndRereadsState();
  testCustomApiRenderingShowsManagedState();
  await testCustomApiSaveClearsKeyAndRereadsState();
  await testCustomApiKeyIsEchoedAsPassword();
  await testRestartCodexRequiresConfirmationAndShowsResult();
  await testDraftsSurviveRefreshAndLateKeyResponse();
  await testSaveSerializesAllMutationsAndPreservesFailedDraft();
  await testStaleRefreshAndFailureDisableWrites();
  await testRestartPartialFailureIsWarningAndRestoreExplainsScope();
  console.log('ui codex transport tests passed');
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
