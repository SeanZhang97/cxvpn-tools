const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../ui/routing.js'), 'utf8');
const flow = source.slice(source.indexOf('  async function exportBackup()'), source.indexOf('  function feedback('));

function harness(api, confirmed = true) {
  const messages = [];
  const applied = [];
  const context = {
    busy: false, dirty: true,
    backend: () => api,
    byId: () => ({ checked: false }),
    feedback: (...args) => messages.push(args),
    friendlyError: error => error.message,
    setBusy: value => { context.busy = value; },
    confirmAction: async options => { applied.push(options); return confirmed; },
    beginMutation: () => applied.push('mutation'),
    loadSetup: async () => applied.push('reload'),
  };
  vm.createContext(context);
  vm.runInContext(flow, context);
  return { context, messages, applied };
}

(async () => {
  for (const result of [{ ok: true, cancelled: true }, { ok: false, msg: '磁盘拒绝写入' }, { ok: true }]) {
    const h = harness({ export_config_backup: async () => result });
    await h.context.exportBackup();
    assert.equal(h.context.busy, false);
    assert.equal(h.messages.some(item => item[1] === 'success'), false);
    assert.equal(h.messages.length, result.cancelled ? 0 : 1);
  }
  const exported = harness({ export_config_backup: async () => ({ ok: true, path: 'D:\\配置\\备份.json' }) });
  await exported.context.exportBackup();
  assert.deepEqual(exported.messages, [['配置已导出至：D:\\配置\\备份.json', 'success']]);

  const cancelled = harness({ preview_config_restore: async () => ({ ok: true, cancelled: true }) });
  await cancelled.context.importConfig();
  assert.equal(cancelled.messages.length, 0);
  assert.equal(cancelled.applied.length, 0);
  assert.equal(cancelled.context.busy, false);

  const preview = { ok: true, path: 'D:\\配置.json', content: '{"备注":"中文-e\u0301-\u{1f1e8}\u{1f1f3}"}', summary: {} };
  for (const confirm of [false, true]) {
    let content;
    const h = harness({
      preview_config_restore: async () => preview,
      restore_config_backup: async value => { content = value; return { ok: true }; },
    }, confirm);
    await h.context.importConfig();
    assert.match(h.applied[0].message, /D:\\配置.json/);
    assert.match(h.applied[0].message, /未保存的修改将被替换/);
    assert.equal(content, confirm ? preview.content : undefined);
    assert.equal(h.applied.includes('mutation'), confirm);
    assert.equal(h.applied.includes('reload'), confirm);
    assert.equal(h.messages.length, confirm ? 1 : 0);
    assert.equal(h.context.busy, false);
  }
  const failed = harness({
    preview_config_restore: async () => preview,
    restore_config_backup: async () => ({ ok: false, msg: '保存失败，已回滚' }),
  });
  await failed.context.importConfig();
  assert.equal(failed.applied.includes('reload'), false);
  assert.equal(failed.messages[0][1], 'error');
  assert.equal(failed.context.busy, false);
  console.log('config transfer UI: cancellation, path reporting and confirmed import passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
