/* 专用工作区；密钥不进入 CFG、localStorage 或全局状态快照。 */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  let accounts = [], loaded = false, fetching = false, epoch = 0, timer = null;
  let anchor = null, retryAt = 0, editing = null, editorSession = 0, editorBusy = false;
  let transferBusy = false, transferMode = 'export', restoreToken = null;
  let returnFocus = null, menuAccount = null, menuTrigger = null;
  const active = () => byId('page-two-factor').classList.contains('active') && !document.hidden;
  const backend = () => window.pywebview?.api;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const icon = name => {
    const node = element('span', `otp-icon otp-icon-${name}`);
    node.setAttribute('aria-hidden', 'true');
    return node;
  };
  function checked(result) {
    if (!result || result.ok === false) throw new Error(result?.msg || '操作未完成');
    return result;
  }
  function feedback(id, message = '', tone = '') { setFeedback(byId(id), message, tone); }
  function visibleAccounts() {
    const query = byId('otp-search').value.trim().toLocaleLowerCase();
    return accounts.filter(row => `${row.issuer}\n${row.account}`.toLocaleLowerCase().includes(query));
  }
  function render() {
    closeMenu(false);
    const rows = visibleAccounts();
    const fragment = document.createDocumentFragment();
    rows.forEach(row => {
      const card = element('article', 'otp-card');
      card.dataset.id = row.id;
      card.setAttribute('role', 'listitem');
      const head = element('div', 'otp-card-head');
      const avatar = element('span', 'otp-avatar', Array.from(row.issuer)[0]?.toUpperCase() || '');
      avatar.setAttribute('aria-hidden', 'true');
      const identity = element('div', 'otp-identity');
      const issuer = element('h3', '', row.issuer), account = element('p', '', row.account);
      issuer.title = row.issuer;
      account.title = row.account;
      identity.append(issuer, account);
      const more = element('button', 'otp-more');
      more.type = 'button';
      more.setAttribute('aria-label', `${row.issuer} ${row.account} 的操作`);
      more.setAttribute('aria-haspopup', 'menu');
      more.setAttribute('aria-expanded', 'false');
      more.append(icon('more'));
      more.addEventListener('click', () => openMenu(row, more));
      head.append(avatar, identity, more);
      const code = element('div', 'otp-code', '--- ---');
      code.dataset.digits = row.digits;
      code.setAttribute('aria-label', '当前验证码');
      const foot = element('div', 'otp-card-foot');
      const progress = element('progress', 'otp-progress');
      progress.max = 1;
      progress.value = 0;
      progress.setAttribute('aria-label', '验证码剩余有效时间');
      const seconds = element('span', 'otp-seconds', '—');
      const copy = element('button', 'otp-copy');
      copy.type = 'button';
      copy.setAttribute('aria-label', `复制 ${row.issuer} ${row.account} 的验证码`);
      copy.append(icon('copy'), document.createTextNode('复制'));
      copy.addEventListener('click', () => void copyCode(row, copy));
      foot.append(progress, seconds, copy);
      card.append(head, code, foot, element('p', 'otp-card-error', row.error || ''));
      fragment.append(card);
    });
    byId('otp-grid').replaceChildren(fragment);
    byId('otp-empty').classList.toggle('hidden', rows.length > 0);
    byId('otp-empty-text').textContent = !loaded ? '正在读取账号…' : accounts.length
      ? '没有匹配的平台或账号' : '还没有账号，点击“添加账号”开始使用';
    byId('otp-count').textContent = !loaded ? '' : byId('otp-search').value.trim()
      ? `显示 ${rows.length} / ${accounts.length} 个账号` : `共 ${accounts.length} 个账号`;
    updateCountdown();
  }
  function now() { return anchor ? anchor.now + (performance.now() - anchor.monotonic) / 1000 : Date.now() / 1000; }
  function updateCountdown() {
    const current = now();
    const values = new Map(accounts.map(row => [row.id, row]));
    byId('otp-grid').querySelectorAll('.otp-card').forEach(card => {
      const row = values.get(card.dataset.id);
      const remaining = Math.max(0, (row?.expires_at || 0) - current);
      const valid = active() && remaining > 0 && !!row?.code;
      card.querySelector('.otp-code').textContent = valid
        ? row.code.replace(new RegExp(`(.{${row.digits / 2}})`), '$1 ') : '--- ---';
      card.querySelector('.otp-progress').value = valid ? remaining / row.period : 0;
      card.querySelector('.otp-seconds').textContent = valid ? `${Math.ceil(remaining)} 秒` : '—';
      const copy = card.querySelector('.otp-copy');
      if (!copy.dataset.busy) copy.disabled = !valid;
    });
  }
  function clearCodes() {
    accounts.forEach(row => { row.code = ''; row.expires_at = 0; });
    updateCountdown();
  }
  async function changed() {
    epoch++;
    loaded = false;
    anchor = null;
    clearCodes();
    await refresh();
    tick();
  }
  async function refresh() {
    if (!active() || fetching || !backend()) return;
    const requestEpoch = epoch;
    fetching = true;
    try {
      const result = checked(await backend().get_otp_accounts());
      if (requestEpoch !== epoch || !active()) return;
      const structure = rows => JSON.stringify(rows.map(({ code, expires_at, ...metadata }) => metadata));
      const needsRender = !loaded || structure(accounts) !== structure(result.accounts);
      accounts = result.accounts;
      anchor = { now: result.now, monotonic: performance.now(), wall: Date.now() };
      loaded = true;
      retryAt = 0;
      feedback('otp-feedback');
      byId('otp-retry').classList.add('hidden');
      if (needsRender) render();
      else updateCountdown();
    } catch (error) {
      if (requestEpoch !== epoch) return;
      clearCodes();
      retryAt = performance.now() + 30000;
      feedback('otp-feedback', friendlyError(error), 'error');
      byId('otp-empty-text').textContent = '读取失败，现有账号未修改';
      byId('otp-retry').classList.remove('hidden');
    } finally {
      fetching = false;
    }
  }
  function tick() {
    clearTimeout(timer);
    if (!active()) return;
    if (anchor && Math.abs((Date.now() - anchor.wall) - (performance.now() - anchor.monotonic)) > 1500) {
      anchor = null;
      clearCodes();
    }
    updateCountdown();
    const due = !loaded || !anchor || accounts.some(row => !row.error && row.expires_at <= now());
    if (due && performance.now() >= retryAt) void refresh();
    timer = setTimeout(tick, 250);
  }
  function pageChanged() {
    epoch++;
    clearTimeout(timer);
    closeMenu(false);
    if (active()) { retryAt = 0; void refresh(); tick(); }
    else { clearCodes(); anchor = null; }
  }
  async function copyCode(row, button) {
    if (button.disabled) return;
    button.disabled = true;
    button.dataset.busy = 'true';
    const requestEpoch = epoch;
    try {
      const result = checked(await backend().get_otp_code(row.id));
      if (!active() || requestEpoch !== epoch) return;
      await writeClipboardText(result.code);
      toast({ ok: true, msg: '验证码已复制' });
    } catch (error) { toast({ ok: false, msg: friendlyError(error) }); }
    finally { delete button.dataset.busy; updateCountdown(); }
  }
  function closeMenu(focus = true) {
    byId('otp-menu').classList.add('hidden');
    menuTrigger?.setAttribute('aria-expanded', 'false');
    if (focus && menuTrigger?.isConnected) menuTrigger.focus();
    menuTrigger = null;
    menuAccount = null;
  }
  function openMenu(row, trigger) {
    const wasOpen = menuTrigger === trigger;
    closeMenu(false);
    if (wasOpen) return;
    menuAccount = row;
    menuTrigger = trigger;
    trigger.setAttribute('aria-expanded', 'true');
    const menu = byId('otp-menu');
    menu.classList.remove('hidden');
    const rect = trigger.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(innerWidth - menu.offsetWidth - 8, rect.right - menu.offsetWidth))}px`;
    menu.style.top = `${Math.max(8, Math.min(innerHeight - menu.offsetHeight - 8, rect.bottom + 5))}px`;
    menu.querySelector('button').focus();
  }
  function modalOpen(id) {
    returnFocus = document.activeElement;
    byId(id).classList.remove('hidden');
  }
  async function modalClose(modal) {
    if (modal.id === 'otp-editor') {
      if (editorBusy) return;
      editorSession++;
      byId('otp-form').reset();
      concealSecret(byId('otp-secret'), byId('otp-secret-eye'));
      editing = null;
    } else {
      if (transferBusy) return;
      byId('otp-transfer-form').reset();
      restoreToken = null;
      if (backend()) void backend().cancel_otp_restore();
    }
    modal.classList.add('hidden');
    if (returnFocus?.isConnected) returnFocus.focus();
  }
  function refreshSelects() {
    ['otp-algorithm', 'otp-digits'].forEach(id => window.RoutingWorkspace?.enhanceSelect(byId(id))?.refresh());
  }
  function openEditor(row = null) {
    editorSession++;
    editing = row;
    byId('otp-form').reset();
    byId('otp-editor-title').textContent = row ? '编辑账号' : '添加账号';
    byId('otp-issuer').value = row?.issuer || '';
    byId('otp-account').value = row?.account || '';
    byId('otp-algorithm').value = row?.algorithm || 'SHA1';
    byId('otp-digits').value = row?.digits || 6;
    byId('otp-period').value = row?.period || 30;
    byId('otp-secret').required = !row;
    byId('otp-secret').placeholder = row ? '留空保留现有密钥' : '粘贴平台提供的密钥或链接';
    byId('otp-advanced').open = !!row && (row.algorithm !== 'SHA1' || row.digits !== 6 || row.period !== 30);
    concealSecret(byId('otp-secret'), byId('otp-secret-eye'));
    feedback('otp-editor-feedback');
    refreshSelects();
    modalOpen('otp-editor');
    byId('otp-issuer').focus();
  }
  function setEditorBusy(value) {
    editorBusy = value;
    byId('otp-editor').querySelectorAll('button,input,select').forEach(control => { control.disabled = value; });
    refreshSelects();
  }
  async function fillFromInput(operation) {
    const session = editorSession;
    setEditorBusy(true);
    feedback('otp-editor-feedback');
    try {
      const result = checked(await operation());
      if (result.cancelled || session !== editorSession) return;
      const row = result.account;
      if (row.issuer) byId('otp-issuer').value = row.issuer;
      if (row.account) byId('otp-account').value = row.account;
      byId('otp-secret').value = row.secret;
      byId('otp-algorithm').value = row.algorithm;
      byId('otp-digits').value = row.digits;
      byId('otp-period').value = row.period;
      refreshSelects();
      feedback('otp-editor-feedback', '已识别，请核对平台与账号后保存');
    } catch (error) { feedback('otp-editor-feedback', friendlyError(error), 'error'); }
    finally { setEditorBusy(false); }
  }
  async function save(event) {
    event.preventDefault();
    if (editorBusy) return;
    setEditorBusy(true);
    try {
      const result = checked(await backend().save_otp_account({
        id: editing?.id, revision: editing?.revision,
        issuer: byId('otp-issuer').value, account: byId('otp-account').value,
        secret: byId('otp-secret').value, algorithm: byId('otp-algorithm').value,
        digits: Number(byId('otp-digits').value), period: Number(byId('otp-period').value),
      }));
      setEditorBusy(false);
      await modalClose(byId('otp-editor'));
      toast(result);
      await changed();
    } catch (error) { feedback('otp-editor-feedback', friendlyError(error), 'error'); }
    finally { setEditorBusy(false); }
  }
  function setTransferMode(mode) {
    if (transferBusy) return;
    transferMode = mode;
    restoreToken = null;
    byId('otp-transfer-form').reset();
    byId('otp-restore-preview').classList.add('hidden');
    byId('otp-password-fields').classList.remove('hidden');
    byId('otp-password').required = true;
    byId('otp-password-confirm').required = mode === 'export';
    byId('otp-password-confirm-field').classList.toggle('hidden', mode !== 'export');
    ['export', 'import'].forEach(item => {
      byId(`otp-mode-${item}`).className = `btn${mode === item ? ' primary' : ''}`;
      byId(`otp-mode-${item}`).setAttribute('aria-pressed', String(mode === item));
    });
    byId('otp-transfer-help').textContent = mode === 'export'
      ? '设置备份密码后选择保存位置。换电脑时可用该密码恢复账号，请妥善保管。'
      : '输入备份密码后选择 .cx2fa 文件。先预览，再确认导入。';
    byId('otp-transfer-submit').textContent = mode === 'export' ? '选择位置并导出' : '选择文件并预览';
    feedback('otp-transfer-feedback');
    if (backend()) void backend().cancel_otp_restore();
  }
  function showRestorePreview(result) {
    restoreToken = result.token;
    const summary = result.summary;
    byId('otp-password').value = '';
    byId('otp-password').required = false;
    byId('otp-password-confirm').required = false;
    byId('otp-password-fields').classList.add('hidden');
    byId('otp-restore-preview').classList.remove('hidden');
    byId('otp-restore-summary').textContent = `新增 ${summary.additions.length} 个，重复 ${summary.duplicates} 个，冲突 ${summary.conflicts.length} 个`;
    const replacements = summary.replacements || [];
    if (replacements.length) byId('otp-restore-summary').textContent += `，恢复不可读账号 ${replacements.length} 个`;
    const rows = [...summary.additions.map(row => ({ ...row, status: '新增' })),
                  ...replacements.map(row => ({ ...row, status: '恢复不可读密钥' })),
                  ...summary.conflicts.map(row => ({ ...row, status: '冲突，跳过' }))];
    byId('otp-restore-items').replaceChildren(...rows.map(row => element('li', '', `${row.issuer} · ${row.account}（${row.status}）`)));
    byId('otp-transfer-submit').textContent = '确认导入';
  }
  async function transfer(event) {
    event.preventDefault();
    if (transferBusy) return;
    if (transferMode === 'export' && byId('otp-password').value !== byId('otp-password-confirm').value) {
      feedback('otp-transfer-feedback', '两次输入的密码不一致', 'error'); return;
    }
    transferBusy = true;
    byId('otp-transfer').querySelectorAll('button').forEach(button => { button.disabled = true; });
    feedback('otp-transfer-feedback', restoreToken ? '正在导入…' : '等待选择文件，可在文件窗口取消…');
    try {
      const result = checked(await (restoreToken ? backend().restore_otp_backup(restoreToken)
        : transferMode === 'export' ? backend().export_otp_backup(byId('otp-password').value)
          : backend().preview_otp_restore(byId('otp-password').value)));
      if (result.cancelled) { feedback('otp-transfer-feedback'); return; }
      if (result.pending) { feedback('otp-transfer-feedback'); showRestorePreview(result); return; }
      restoreToken = null;
      byId('otp-password').value = '';
      byId('otp-password-confirm').value = '';
      if (result.path) feedback('otp-transfer-feedback', `备份已保存：\n${result.path}`);
      else {
        transferBusy = false;
        await modalClose(byId('otp-transfer'));
        toast(result);
        await changed();
      }
    } catch (error) {
      if (restoreToken) { transferBusy = false; setTransferMode('import'); }
      feedback('otp-transfer-feedback', friendlyError(error), 'error');
    }
    finally {
      transferBusy = false;
      byId('otp-transfer').querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  function bind() {
    byId('otp-search').addEventListener('input', render);
    byId('otp-add').addEventListener('click', () => openEditor());
    byId('otp-retry').addEventListener('click', () => { retryAt = 0; void refresh(); });
    byId('otp-backup').addEventListener('click', () => { setTransferMode('export'); modalOpen('otp-transfer'); byId('otp-password').focus(); });
    byId('otp-form').addEventListener('submit', save);
    byId('otp-editor-cancel').addEventListener('click', () => void modalClose(byId('otp-editor')));
    byId('otp-transfer-cancel').addEventListener('click', () => void modalClose(byId('otp-transfer')));
    byId('otp-secret-eye').addEventListener('click', () => toggleSecret(byId('otp-secret'), byId('otp-secret-eye')));
    byId('otp-parse').addEventListener('click', () => void fillFromInput(() => backend().parse_otp_input(byId('otp-secret').value)));
    byId('otp-qr').addEventListener('click', () => void fillFromInput(() => backend().import_otp_qr()));
    byId('otp-transfer-form').addEventListener('submit', transfer);
    ['export', 'import'].forEach(mode => byId(`otp-mode-${mode}`).addEventListener('click', () => setTransferMode(mode)));
    byId('otp-menu').addEventListener('click', async event => {
      const action = event.target.closest('[data-otp-action]')?.dataset.otpAction;
      const row = menuAccount;
      if (!row || !action) return;
      closeMenu();
      if (action === 'edit') { openEditor(row); return; }
      const accepted = await confirmAction({ title: '删除 2FA 账号',
        message: `删除 ${row.issuer} 的 ${row.account}？此操作只删除本地记录，不会关闭平台的二次验证。`, confirmText: '删除账号' });
      if (!accepted) return;
      try { toast(checked(await backend().delete_otp_account(row.id, row.revision))); await changed(); }
      catch (error) { toast({ ok: false, msg: friendlyError(error) }); }
    });
    document.addEventListener('click', event => {
      if (!event.target.closest('#otp-menu,.otp-more')) closeMenu(false);
    });
    document.addEventListener('paste', event => {
      if (byId('otp-editor').classList.contains('hidden') || editorBusy) return;
      const image = [...(event.clipboardData?.items || [])].find(item => item.type.startsWith('image/'));
      if (!image) return;
      event.preventDefault();
      const file = image.getAsFile();
      if (!file || file.size > 6 * 1024 * 1024) { feedback('otp-editor-feedback', '图片为空或超过 6 MB', 'error'); return; }
      void fillFromInput(async () => {
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error('剪贴板图片读取失败'));
          reader.readAsDataURL(file);
        });
        return backend().decode_otp_image(data);
      });
    });
    document.addEventListener('keydown', event => {
      if (!byId('otp-menu').classList.contains('hidden')) {
        const buttons = [...byId('otp-menu').querySelectorAll('button')];
        const index = buttons.indexOf(document.activeElement);
        if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          event.preventDefault(); event.stopImmediatePropagation();
          buttons[event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
            : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length].focus();
        }
        if (event.key === 'Escape' || event.key === 'Tab') { closeMenu(event.key === 'Escape'); if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); } }
      }
      const modal = ['otp-editor', 'otp-transfer'].map(byId).find(node => !node.classList.contains('hidden'));
      if (!modal || !byId('confirmmodal').classList.contains('hidden')) return;
      if (event.key === 'Escape' && !document.querySelector('.routing-select-menu:not(.hidden)')) {
        event.preventDefault(); event.stopImmediatePropagation(); void modalClose(modal);
      }
      if (event.key === 'Tab') {
        const focusable = [...modal.querySelectorAll('button,input,summary,[tabindex="0"]')].filter(node => !node.disabled && node.getClientRects().length && node.tabIndex >= 0);
        const target = event.shiftKey ? focusable.at(-1) : focusable[0];
        if (document.activeElement === (event.shiftKey ? focusable[0] : focusable.at(-1))) { event.preventDefault(); target?.focus(); }
      }
    }, true);
    window.addEventListener('resize', () => closeMenu(false));
    document.addEventListener('scroll', () => closeMenu(false), true);
    window.addEventListener('cxvpn:pagechange', pageChanged);
    document.addEventListener('visibilitychange', pageChanged);
    window.addEventListener('focus', () => { if (active()) { retryAt = 0; void refresh(); } });
    window.addEventListener('pywebviewready', () => { refreshSelects(); if (active()) pageChanged(); });
  }
  window.TwoFactorWorkspace = { closeModal: modalClose };
  bind();
})();
