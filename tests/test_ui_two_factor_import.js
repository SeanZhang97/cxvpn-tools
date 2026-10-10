const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function surface() {
  const listeners = new Map();
  return {
    addEventListener(name, handler) { listeners.set(name, [...(listeners.get(name) || []), handler]); },
    emit(name, event = {}) { return Promise.all((listeners.get(name) || []).map(handler => handler(event))); },
  };
}
const nodes = new Map();
function node(id = '') {
  if (nodes.has(id)) return nodes.get(id);
  const classes = new Set();
  const value = {
    ...surface(), id, value: '', textContent: '', required: false, disabled: false, children: [],
    dataset: {}, style: {}, isConnected: true,
    classList: {
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
    },
    setAttribute() {},
    reset() {
      const fields = id === 'otp-form' ? ['otp-issuer', 'otp-account', 'otp-secret']
        : id === 'otp-transfer-form' ? ['otp-password', 'otp-password-confirm'] : [];
      fields.forEach(name => { node(name).value = ''; });
    },
    focus() { document.activeElement = this; },
    replaceChildren(...items) { this.children = items; }, append(...items) { this.children.push(...items); },
    querySelectorAll() {
      if (id === 'otp-transfer') return [...nodes.values()].filter(n => /^otp-(mode-|google-(?:files|reset)$|password|transfer-(?:cancel|submit)$)/.test(n.id));
      return id === 'otp-editor' ? [...nodes.values()].filter(n => /^otp-(issuer|account|secret|algorithm|digits|period|parse|qr|save|editor-cancel)$/.test(n.id)) : [];
    },
  };
  if (id) nodes.set(id, value);
  return value;
}
const document = { ...surface(), hidden: false, activeElement: null,
  getElementById: node, createElement: () => node(), createDocumentFragment: () => node(),
  querySelector: () => null,
};
const html = fs.readFileSync(path.join(__dirname, '..', 'ui/index.html'), 'utf8');
for (const [, id] of html.matchAll(/id="(otp-[^"]+)"/g)) node(id);
node('otp-editor').classList.add('hidden');
node('otp-transfer').classList.add('hidden');
let calls = [], fileResult, editorResult, savedPayload, delayedResets = [];
let delayCancel = true;
const partial = { ok: true, pending: false, batch: {
  received: 1, total: 2, missing: [2], accounts: [{ issuer: 'Google', account: '公开测试' }], skipped: [],
} };
const complete = { ok: true, pending: true, token: 'test-token', batch: {
  ...partial.batch, received: 2, missing: [], skipped: [{ issuer: 'HOTP', account: 'test', reason: 'HOTP 计数验证码暂不支持' }],
}, summary: { additions: partial.batch.accounts, duplicates: 1, conflicts: [{ issuer: 'Conflict', account: 'test' }] } };
const backend = {
  cancel_otp_restore() {
    calls.push('cancel');
    return delayCancel ? new Promise(resolve => delayedResets.push(resolve)) : Promise.resolve({ ok: true });
  },
  async preview_otp_google_images() { calls.push('files'); return fileResult; },
  async preview_otp_google_image(value) { calls.push('paste:' + value); return complete; },
  async confirm_otp_google_import(token) { calls.push('confirm:' + token); return { ok: true }; },
  async import_otp_qr() { calls.push('editor-qr'); return editorResult; },
  async decode_otp_image() { calls.push('editor-paste'); return editorResult; },
  async save_otp_account(payload) { savedPayload = payload; return { ok: true }; },
};
const window = { ...surface(), pywebview: { api: backend } };
class FileReader {
  readAsDataURL() { this.result = 'data:image/png;base64,synthetic'; this.onload(); }
}
const context = vm.createContext({ window, document, FileReader, Date, performance,
  setTimeout, clearTimeout, console, friendlyError: error => error.message,
  setFeedback: (target, message) => { target.textContent = message; }, toast() {},
  concealSecret() {},
});
const source = fs.readFileSync(path.join(__dirname, '..', 'ui/two_factor.js'), 'utf8');
vm.runInContext(source.replace('window.TwoFactorWorkspace = { closeModal: modalClose }',
  'window.TwoFactorWorkspace = { closeModal: modalClose, openEditorForTest: openEditor }'), context);
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };
const click = id => node(id).emit('click');

(async () => {
  await click('otp-backup');
  await click('otp-mode-google');
  fileResult = partial;
  await click('otp-google-files');
  await settle();
  assert.deepEqual(calls, ['cancel'], 'reading must wait for old cancellation');
  delayedResets.shift()({ ok: true });
  await settle();
  assert.deepEqual(calls, ['cancel', 'cancel']);
  delayedResets.shift()({ ok: true });
  await settle();
  delayCancel = false;
  assert.deepEqual(calls, ['cancel', 'cancel', 'files']);
  assert.equal(node('otp-transfer-submit').disabled, true, 'missing page cannot be committed');
  assert.match(node('otp-google-progress').textContent, /还缺第 2 页/);
  assert.equal(node('otp-password').required, false);
  assert.equal(node('otp-password-fields').classList.contains('hidden'), true);

  fileResult = complete;
  await click('otp-google-files');
  await settle();
  assert.equal(node('otp-transfer-submit').disabled, false);
  assert.match(node('otp-restore-summary').textContent, /新增 1 个，重复 1 个，冲突 1 个，不支持 1 个/);
  assert.ok(node('otp-restore-items').children.some(n => n.textContent.includes('HOTP')));
  const summary = node('otp-restore-summary').textContent;
  fileResult = { ok: true, cancelled: true };
  await click('otp-google-files');
  await settle();
  assert.equal(node('otp-restore-summary').textContent, summary);
  assert.equal(node('otp-transfer-submit').disabled, false, 'cancelling extra selection keeps valid preview');
  fileResult = { ok: false, msg: '批次不一致' };
  await click('otp-google-files');
  await settle();
  assert.match(node('otp-transfer-feedback').textContent, /批次不一致/);
  assert.equal(node('otp-transfer-submit').disabled, false);

  await click('otp-google-reset');
  await settle();
  assert.equal(node('otp-transfer-submit').disabled, true);
  assert.equal(node('otp-google-progress').textContent, '');
  let prevented = false;
  await document.emit('paste', { preventDefault() { prevented = true; }, clipboardData: { items: [
    { type: 'image/png', getAsFile: () => ({ size: 10 }) },
  ] } });
  await settle();
  assert.equal(prevented, true);
  assert.ok(calls.includes('paste:data:image/png;base64,synthetic'));
  await node('otp-transfer-form').emit('submit', { preventDefault() {} });
  await settle();
  assert.ok(calls.includes('confirm:test-token'));
  assert.equal(node('otp-transfer').classList.contains('hidden'), true);
  await click('otp-backup');
  await click('otp-mode-import');
  assert.equal(node('otp-password').required, true, 'existing backup password requirement survives');
  assert.equal(node('otp-google-fields').classList.contains('hidden'), true);

  await click('otp-transfer-cancel');
  await settle();
  node('otp-add').focus();
  await click('otp-add');
  node('otp-issuer').value = '尚未保存的平台';
  node('otp-account').value = '尚未保存的账号';
  node('otp-secret').value = 'PUBLIC-TEST-SECRET';
  editorResult = { ...partial, kind: 'google' };
  await click('otp-qr');
  await settle();
  assert.equal(node('otp-editor').classList.contains('hidden'), true);
  assert.equal(node('otp-transfer').classList.contains('hidden'), false);
  assert.equal(node('otp-transfer-submit').disabled, true);
  assert.match(node('otp-google-progress').textContent, /还缺第 2 页/);
  assert.equal(calls.at(-1), 'editor-qr', 'adopting detected Google QR must not cancel its new batch');
  await click('otp-transfer-cancel');
  await settle();
  assert.equal(node('otp-editor').classList.contains('hidden'), false);
  assert.equal(node('otp-issuer').value, '尚未保存的平台');
  assert.equal(node('otp-account').value, '尚未保存的账号');
  assert.equal(node('otp-secret').value, 'PUBLIC-TEST-SECRET');
  assert.equal(document.activeElement, node('otp-qr'));

  editorResult = { ...complete, kind: 'google' };
  await document.emit('paste', { preventDefault() {}, clipboardData: { items: [
    { type: 'image/png', getAsFile: () => ({ size: 10 }) },
  ] } });
  await settle();
  assert.ok(calls.includes('editor-paste'));
  assert.equal(node('otp-transfer-submit').disabled, false);
  assert.equal(calls.at(-1), 'editor-paste');
  await node('otp-transfer-form').emit('submit', { preventDefault() {} });
  await settle();
  assert.equal(node('otp-editor').classList.contains('hidden'), true);
  assert.equal(node('otp-transfer').classList.contains('hidden'), true);
  assert.equal(node('otp-secret').value, '', 'successful bulk import closes and clears the original editor');
  assert.equal(document.activeElement, node('otp-add'));

  await click('otp-add');
  editorResult = { ok: true, kind: 'totp', account: {
    issuer: '普通平台', account: 'public@example.com', secret: 'PUBLIC-TEST-SECRET',
    algorithm: 'SHA256', digits: 8, period: 60,
  } };
  await click('otp-qr');
  await settle();
  assert.equal(node('otp-editor').classList.contains('hidden'), false);
  assert.equal(node('otp-transfer').classList.contains('hidden'), true);
  assert.equal(node('otp-issuer').value, '普通平台');
  assert.equal(node('otp-secret').value, 'PUBLIC-TEST-SECRET');
  assert.equal(node('otp-algorithm').value, 'SHA256');
  assert.equal(node('otp-digits').value, 8);
  editorResult = { ok: true, cancelled: true };
  await click('otp-qr');
  await settle();
  assert.equal(node('otp-issuer').value, '普通平台');
  editorResult = { ok: false, msg: '无法识别' };
  await click('otp-qr');
  await settle();
  assert.equal(node('otp-issuer').value, '普通平台');
  assert.match(node('otp-editor-feedback').textContent, /无法识别/);
  assert.equal(node('otp-qr').disabled, false);

  await click('otp-editor-cancel');
  const existing = { id: 'existing-row', revision: 7, issuer: '原平台', account: '原账号',
    algorithm: 'SHA1', digits: 6, period: 30 };
  window.TwoFactorWorkspace.openEditorForTest(existing);
  editorResult = { ...partial, kind: 'google' };
  await click('otp-qr');
  await settle();
  await click('otp-transfer-cancel');
  await settle();
  assert.equal(node('otp-editor-title').textContent, '编辑账号');
  assert.equal(node('otp-secret').required, false);
  await node('otp-form').emit('submit', { preventDefault() {} });
  assert.equal(savedPayload.id, existing.id);
  assert.equal(savedPayload.revision, existing.revision, 'cancelling Google import must preserve edited record revision');
  assert.equal(savedPayload.issuer, existing.issuer);
  assert.equal(savedPayload.secret, '', 'Google export must not overwrite the edited record secret');
  console.log('2FA Google import UI behavior passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
