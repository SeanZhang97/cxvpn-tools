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
}

function testTransportRenderingUsesTriState() {
  const elements = transportElements();
  const context = {
    codexTransportBusy: false,
    $(id) { return elements[id]; },
  };
  vm.createContext(context);
  vm.runInContext(between('function renderCodexTransport', 'async function refreshCodexPage'), context);

  context.renderCodexTransport({
    transport_mode: 'wss_preferred', websocket_enabled: true,
    transport_conflict: false, active_provider: 'openai',
  });
  assert.equal(elements['codex-wss-enabled'].checked, true);
  assert.equal(elements['codex-wss-enabled'].indeterminate, false);
  assert.equal(elements['codex-wss-enabled'].disabled, false);
  assert.match(elements['codex-transport-summary'].textContent, /优先使用 WSS/);

  context.renderCodexTransport({
    transport_mode: 'custom_provider', websocket_enabled: null,
    transport_conflict: false, active_provider: 'cm',
  });
  assert.equal(elements['codex-wss-enabled'].indeterminate, true);
  assert.equal(elements['codex-wss-enabled'].disabled, true);
  assert.match(elements['codex-transport-summary'].textContent, /cm/);
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
  vm.createContext(context);
  vm.runInContext(
    between('function setCodexTransportFeedback', 'function setCodexActionFeedback'),
    context);

  await context.setCodexTransport(false);

  assert.deepEqual(calls, [false, 'refresh']);
  assert.equal(context.codexTransportBusy, false);
  assert.match(elements['codex-transport-result'].textContent, /冲突/);
}

async function main() {
  testCodexTransportControlExists();
  testTransportRenderingUsesTriState();
  await testTransportSubmitsExplicitTargetAndRereadsState();
  console.log('ui codex transport tests passed');
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
