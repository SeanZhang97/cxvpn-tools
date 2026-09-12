/* app.js - UI 逻辑，通过 window.pywebview.api 调后端 */
let CFG = null;
let editing = null; // null=新增，否则为被编辑的 VPN 名
let modalCreatedName = null; // 新增弹窗内已创建、但凭据仍待同步的条目
let capId = 0;
let capPoints = [];
let curPage = 'overview';
let ddValue = 'Automatic';
let credName = null;
let credentialConnectRetry = null;
let smsUiId = 0;
let renewalActive = false;
let renewalCancelPending = false;
let uiStateVersion = 0;
let uiStateSubscriptionStopped = false;
let uiStateRetryMs = 1000;
let lastLogsVersion = -1;
let blockingModalSync = Promise.resolve();
let connectionBusy = false;
let confirmResolve = null;
let confirmReturnFocus = null;
let connectChoiceResolve = null;
let VPN_STATUS = { connections: [], connected_names: [], route_conflicts: [] };
let VPN_LIST = [];
let vpnListReady = false;
let vpnLoadPromise = null;
let vpnLoadIncludesForceRefresh = false;
let lastRenewalStatus = null;
let repairWasActive = false;
let repairStateReady = false;
let lastIpRouteSignature = null;

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

// 让纯浏览器预览也能切换页面；真实 WebView 就绪后由 bind() 接管完整交互。
function bindPreviewNavigation() {
  document.querySelectorAll('#nav [data-page]').forEach(item => {
    item.onclick = () => { void goToPage(item.dataset.page); };
  });
}

window.addEventListener('DOMContentLoaded', bindPreviewNavigation);

window.addEventListener('pywebviewready', async () => {
  try {
    CFG = await api().get_config();
    bind();
    fillForms();
    showConfiguredVpnPreview();
    void refreshIpInfo();
    await api().ui_ready();
    void ensureVpnsLoaded({ background: true });
    await loadDesktopSettings();
    await syncBrowserVisibility();
    await startUiStateSubscription();
    await initializeAppUpdate();
    // 后端缓存窗口为 5 分钟；仅总览页需要网络出口数据，隐藏页面不重复触发探测。
    setInterval(() => {
      if (curPage === 'overview') void refreshIpInfo();
    }, 300000);
  } catch (e) {
    console.error(e);
    toast({ ok: false, msg: `界面初始化失败：${friendlyError(e)}` });
  }
});

window.addEventListener('cxvpn:desktop-action', async () => {
  try {
    CFG = await api().get_config();
    fillForms();
    await window.RoutingWorkspace?.refresh?.(true);
  } catch (error) {
    toast({ ok: false, msg: `桌面快捷操作已执行，但界面同步失败：${friendlyError(error)}` });
  }
});

function bind() {
  bindInputModality();
  bindSecretToggles();
  bindNavigationAndOverview();
  bindCodexPage();
  bindVpnManagement();
  bindSettingsAndBrowser();
  bindVerificationFlows();
}

let codexViewText = '';
let codexProxyOn = false;

function renderCodexToggle(state) {
  const toggle = $('btn-codex-toggle');
  if (!toggle) return;
  const residual = Array.isArray(state.residual_fields) && state.residual_fields.length > 0;
  codexProxyOn = state.synced_fields > 0 || residual;
  toggle.textContent = codexProxyOn ? '关闭 Codex 代理' : '开启 Codex 代理';
  toggle.className = `btn ${codexProxyOn ? 'accent' : 'primary'}`;
}

async function refreshCodexPage() {
  const summary = $('codex-sync-summary');
  const port = $('codex-mixed-port');
  const pathInput = $('codex-config-path');
  const detail = $('codex-sync-detail');
  if (!summary || !port || !api()?.get_codex_status) return;
  try {
    const state = await api().get_codex_status();
    if (!state || typeof state !== 'object') {
      summary.textContent = 'Codex 状态接口返回异常。';
      return;
    }
    renderCodexToggle(state);
    port.value = state.mixed_port == null ? '' : String(state.mixed_port);
    if (pathInput) pathInput.value = state.config_path || '';
    let text;
    if (state.parse_error) {
      text = `Codex 配置文件存在但无法解析（${state.parse_error}），开启代理前请先修复。`;
    } else if (!state.config_exists) {
      text = '尚未发现 Codex 配置文件；点击「开启 Codex 代理」会创建。';
    } else if (!state.synced_fields) {
      if (Array.isArray(state.residual_fields) && state.residual_fields.length) {
        text = `Codex 配置中检测到 ${state.residual_fields.length} 个代理字段残留（快照缺失）。可点击「开启 Codex 代理」重建快照，或「关闭 Codex 代理」直接移除。`;
      } else {
        text = 'Codex 配置文件已找到；点击「开启 Codex 代理」写入代理配置。';
      }
    } else if (Array.isArray(state.deviated_fields) && state.deviated_fields.length) {
      text = `Codex 代理已开启（${state.synced_fields} 个托管字段）；其中 ${state.deviated_fields.length} 个字段被手动修改，关闭代理时将跳过。`;
    } else if (state.last_mixed_port && state.mixed_port !== state.last_mixed_port) {
      text = `Codex 代理上次开启端口为 ${state.last_mixed_port}，当前端口已变为 ${state.mixed_port}；可重新开启。`;
    } else {
      text = `Codex 代理已开启（端口 ${state.last_mixed_port}）。已运行的 Codex 需要重启才会加载。`;
    }
    if (state.system_proxy_mode && state.routing_enabled) {
      text += ' 代理有效运行时本工具也会自动校正。';
    }
    summary.textContent = text;
    if (detail) {
      detail.textContent = state.snapshot_exists
        ? `用户数据目录保留了 ${state.snapshot_fields} 个托管字段的快照；关闭 Codex 代理时会删除这些字段。`
        : 'Codex 代理尚未开启；开启后会在用户数据目录保留快照，用于关闭时代理字段移除。';
    }
  } catch (error) {
    summary.textContent = `无法读取 Codex 配置状态：${friendlyError(error)}`;
  }
}

function setCodexActionFeedback(payload) {
  if (!payload) return;
  const feedback = (text, tone) => {
    const target = $('codex-action-result');
    if (!target) return;
    target.className = `form-feedback ${tone}`;
    target.textContent = text;
  };
  const warnings = payload.warnings || [];
  if (!payload.ok) {
    toast({ ok: false, msg: payload.warning || '操作失败' });
    feedback(payload.warning || '操作失败', 'error');
  } else if (warnings.length) {
    toast({ ok: false, msg: `已跳过 ${warnings.length} 个被手动修改的字段` });
    feedback(`完成：已跳过 ${warnings.length} 个被手动修改的字段（保留您的修改）。`, 'warning');
  } else {
    toast({ ok: true, msg: '操作完成' });
    feedback(
      payload.restart_required_after_change
        ? '完成。已运行的 Codex 需要重启才会重新加载配置。'
        : '完成。', 'ok');
  }
}

async function runCodexAction(action, confirmOptions) {
  const resultElement = $('codex-action-result');
  if (resultElement) resultElement.textContent = '';
  if (confirmOptions) {
    const confirmed = await confirmAction(confirmOptions);
    if (!confirmed) return;
  }
  try {
    const result = await action();
    setCodexActionFeedback(result);
  } catch (error) {
    toast({ ok: false, msg: `操作失败：${friendlyError(error)}` });
  } finally {
    void refreshCodexPage();
  }
}

async function openCodexViewer() {
  const modal = $('codemodal');
  if (!modal) return;
  try {
    const result = await api().read_codex_config();
    const pathLine = $('codex-view-path');
    const content = $('codex-view-content');
    if (!result.ok) {
      if (content) content.textContent = result.warning || '无法读取配置文件';
    } else if (!result.exists) {
      if (content) content.textContent = 'Codex 配置文件尚未创建；点击「开启 Codex 代理」后会自动创建。';
    } else {
      codexViewText = result.text || '';
      if (content) content.textContent = codexViewText;
    }
    if (pathLine) pathLine.textContent = result.path || '';
    modal.classList.remove('hidden');
  } catch (error) {
    toast({ ok: false, msg: `读取配置文件失败：${friendlyError(error)}` });
  }
}

function bindCodexPage() {
  window.addEventListener('cxvpn:pagechange', event => {
    if (event.detail?.page === 'codex') void refreshCodexPage();
  });
  const toggle = $('btn-codex-toggle');
  if (toggle) toggle.onclick = () => {
    if (codexProxyOn) {
      void runCodexAction(
        () => api().restore_codex_proxy(),
        {
          title: '关闭 Codex 代理',
          message: '将删除 Codex 配置中由本工具写入且未被手动修改的代理字段（用户手动改过的字段会保留）。需要重启 Codex 才会生效。',
          confirmText: '关闭',
          tone: 'notice',
          kicker: 'CODEX CONFIG',
        });
    } else {
      void runCodexAction(() => api().sync_codex_proxy());
    }
  };
  const view = $('btn-codex-view');
  if (view) view.onclick = () => void openCodexViewer();
}

function bindSecretToggles() {
  document.querySelectorAll('.eye').forEach(button => {
    const input = button.closest('.inp-wrap')?.querySelector('input');
    if (!input) return;
    setSecretToggleState(button, input.type !== 'password');
    button.onclick = () => toggleSecret(input, button);
  });
}

function bindInputModality() {
  document.addEventListener('keydown', event => {
    if (event.key === 'Tab') document.body.classList.add('keyboard-navigation');
  }, true);
  document.addEventListener('pointerdown', () => {
    document.body.classList.remove('keyboard-navigation');
  }, true);
}

function bindNavigationAndOverview() {
  document.querySelectorAll('#nav [data-page]').forEach(item => {
    item.onclick = () => goToPage(item.dataset.page);
  });
  const routingEntry = document.querySelector('#nav [data-page="routing"]');
  if (routingEntry) routingEntry.onclick = () => {
    void goToPage('routing');
    setTimeout(() => window.RoutingWorkspace?.openTab?.('overview'), 0);
  };

  $('btn-connect').onclick = async () => {
    if (!CFG.vpn_name) {
      await goToPage('vpns');
      toast({ ok: false, tone: 'warning', msg: '请先新增或选择默认 VPN' });
      return;
    }
    await connectVpn(CFG.vpn_name, $('btn-connect'));
  };

  $('btn-disconnect').onclick = async () => {
    if (CFG.vpn_name) await disconnectVpn(CFG.vpn_name, $('btn-disconnect'));
  };
  $('btn-disconnect-all').onclick = () => disconnectAll($('btn-disconnect-all'));

  $('btn-renew').onclick = handleRenewalAction;

  $('btn-repair').onclick = async () => {
    const confirmed = await confirmAction({
      title: '确认修复 VPN 服务',
      message: '修复会断开当前全部 VPN 连接，并重启 PolicyAgent、IKEEXT、RasMan 系统服务。请先保存依赖当前网络的工作。',
      confirmText: '断开并修复'
    });
    if (!confirmed) return;
    const button = $('btn-repair');
    button.disabled = true;
    $('repair-title').textContent = '正在启动修复';
    $('repair-detail').textContent = '请响应 Windows UAC 授权';
    try {
      const result = normalizeResult(await api().repair_vpn(), '修复任务已启动');
      toast(result);
      if (!result.ok) button.disabled = false;
      else repairWasActive = true;
    } catch (e) {
      button.disabled = false;
      toast({ ok: false, msg: `修复启动失败：${friendlyError(e)}` });
    } finally {
      setTimeout(refreshUiStateOnce, 150);
    }
  };

  $('ov-auto').onchange = () => saveToggle(
    $('ov-auto'), 'auto_renew', '已开启自动续期', '已关闭自动续期');
  $('ov-autoconn').onchange = () => saveToggle(
    $('ov-autoconn'), 'auto_connect', '已开启自动连接/重连', '已关闭自动连接，仅手动连接');
  $('ov-startup').onchange = () => saveStartupToggle($('ov-startup'));
  $('ov-tray').onchange = () => saveToggle(
    $('ov-tray'), 'close_to_tray', '关闭窗口后将驻留系统托盘', '关闭窗口时将直接退出程序');

  $('btn-refresh-ip').onclick = () => void refreshIpInfo(true);
  document.querySelectorAll('.ip-copy').forEach(button => {
    button.onclick = () => copyIpAddress(button);
  });
}

function renewalActionButtons() {
  return [
    { button: $('btn-renew'), idle: '立即处理授权', active: '中断授权流程' },
    { button: $('btn-br-automation'), idle: '执行自动化', active: '中断自动化' },
  ].filter(item => item.button);
}

function renderRenewalActionButtons() {
  renewalActionButtons().forEach(({ button, idle, active }) => {
    const label = renewalCancelPending ? '正在中断…' : renewalActive ? active : idle;
    button.textContent = label;
    button.classList.toggle('accent', !renewalActive);
    button.classList.toggle('danger', renewalActive);
    button.setAttribute('aria-label', label);
    button.title = label;
  });
}

async function handleRenewalAction() {
  const cancelling = renewalActive;
  const actions = renewalActionButtons().map(item => item.button);
  actions.forEach(button => {
    button.disabled = true;
    button.textContent = cancelling ? '正在中断…' : '正在启动…';
  });
  try {
    const result = cancelling
      ? normalizeResult(await api().cancel_renew(), '已请求中断授权处理')
      : normalizeResult(await api().renew_now(), '授权处理任务已启动');
    if (result.ok) {
      if (cancelling) renewalCancelPending = true;
      else renewalActive = true;
    }
    toast(result);
  } catch (e) {
    toast({ ok: false, msg: `操作失败：${friendlyError(e)}` });
  } finally {
    actions.forEach(button => { button.disabled = false; });
    renderRenewalActionButtons();
    setTimeout(refreshUiStateOnce, 150);
  }
}

function bindVpnManagement() {
  $('btn-refresh-vpn').onclick = async () => {
    await runBusy($('btn-refresh-vpn'), '刷新中…', () =>
      reloadVpns({ announce: true, forceRefresh: true }));
  };
  $('btn-add-vpn').onclick = () => openModal(null);
  $('btn-modal-cancel').onclick = () => closeModal($('modal'));
  $('btn-modal-ok').onclick = saveVpnModal;

  $('m-type-btn').onclick = (e) => {
    e.stopPropagation();
    $('m-type-list').classList.toggle('hidden');
  };
  document.querySelectorAll('#m-type-list a').forEach(item => {
    item.onclick = () => {
      ddValue = item.dataset.v;
      $('m-type-btn').textContent = ddValue;
      $('m-type-list').classList.add('hidden');
      updateVpnModalFields();
    };
  });
  document.addEventListener('click', () => $('m-type-list').classList.add('hidden'));

  $('btn-cred-cancel').onclick = () => closeModal($('credmodal'));
  $('btn-cred-ok').onclick = saveCredential;
  $('btn-connect-switch').onclick = () => finishConnectChoice('switch');
  $('btn-connect-parallel').onclick = () => finishConnectChoice('parallel');
  $('btn-connect-cancel').onclick = () => finishConnectChoice(null);
}

function bindSettingsAndBrowser() {
  $('btn-br-automation').onclick = handleRenewalAction;
  $('btn-br-open').onclick = async () => {
    await runBusy($('btn-br-open'), '启动中…', async () => {
      try {
        toast(normalizeResult(await api().open_browser(), '已请求打开内置浏览器'));
        setTimeout(refreshBrState, 800);
      } catch (e) {
        toast({ ok: false, msg: `浏览器启动失败：${friendlyError(e)}` });
      }
    });
  };
  $('btn-br-reload').onclick = async () => {
    await runBusy($('btn-br-reload'), '刷新中…', async () => {
      try {
        const response = await withTimeout(
          api().browser_reload(), 25000, '浏览器刷新超时，请重试');
        toast(normalizeResult(response, '浏览器已刷新'));
      } catch (e) {
        toast({ ok: false, msg: `刷新失败：${friendlyError(e)}` });
      }
    });
  };

  $('btn-save-cx').onclick = saveCxSettings;
  bindSmartSelect('sms-method', updateSmsMethodUi);
  bindSmartSelect('sms-email-provider', () => applyEmailProviderPreset(true));
  document.addEventListener('click', () => closeSmartSelects());
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeSmartSelects();
  });
  $('btn-save-sms').onclick = saveSmsSettings;
  $('btn-test-sms-email').onclick = testSmsEmailSettings;
  $('btn-save-vlm').onclick = saveVlmSettings;
  $('btn-test-vlm').onclick = testVlmSettings;
  $('app-auto-update').onchange = saveAppUpdateToggle;
  $('btn-app-update').onclick = () => checkAppUpdate(true);
  $('btn-copy-logs').onclick = copyLogs;
  $('btn-confirm-cancel').onclick = () => finishConfirm(false);
  $('btn-confirm-ok').onclick = () => finishConfirm(true);
}

async function saveAppUpdateToggle() {
  const input = $('app-auto-update');
  const previous = CFG.app_update || { auto_check: true, last_check: 0 };
  CFG.app_update = { ...previous, auto_check: input.checked };
  input.disabled = true;
  try {
    if (await api().save_config(CFG) === false) throw new Error('后端未保存配置');
    toast({ ok: true, msg: input.checked ? '自动更新检查已开启' : '自动更新检查已关闭' });
  } catch (error) {
    CFG.app_update = previous;
    input.checked = previous.auto_check !== false;
    toast({ ok: false, msg: `保存失败：${friendlyError(error)}` });
  } finally {
    input.disabled = false;
  }
}

async function initializeAppUpdate() {
  try {
    const version = await api().get_app_version();
    $('app-version-text').textContent = `当前版本：${version.version}`;
    const settings = CFG.app_update || {};
    $('app-auto-update').checked = settings.auto_check !== false;
    if (settings.auto_check !== false &&
        (!settings.last_check || Date.now() / 1000 - settings.last_check > 86400)) {
      void checkAppUpdate(false);
    }
  } catch (error) {
    $('app-version-text').textContent = '当前版本：未知';
  }
}

async function checkAppUpdate(manual) {
  const button = $('btn-app-update');
  await runBusy(button, '检查中…', async () => {
    setFeedback($('app-update-result'), '正在连接 GitHub Releases…', 'loading');
    try {
      const result = await api().check_app_update(!!manual);
      if (!result.ok) {
        setFeedback($('app-update-result'), result.msg || '检查更新失败', 'bad');
        return;
      }
      if (result.newer) {
        setFeedback($('app-update-result'),
          `发现新版本 ${result.latest_version}，请打开 Release 页面下载安装包。`, 'ok');
        if (result.release_url) toast({ ok: true, msg: `发现新版本 ${result.latest_version}` });
        if (manual && result.release_url) await api().open_app_release(result.release_url);
      } else {
        setFeedback($('app-update-result'),
          result.msg || `当前已是最新版本（${result.current_version}）`, 'ok');
      }
    } catch (error) {
      setFeedback($('app-update-result'), `检查更新失败：${friendlyError(error)}`, 'bad');
    }
  });
}

const EMAIL_PROVIDER_HOSTS = {
  qq: 'imap.qq.com',
  '163': 'imap.163.com',
  gmail: 'imap.gmail.com',
  icloud: 'imap.mail.me.com'
};

function closeSmartSelects(except = null) {
  document.querySelectorAll('.smart-select.open').forEach(root => {
    if (root === except) return;
    root.classList.remove('open');
    root.querySelector('.smart-select-menu').classList.add('hidden');
    root.querySelector('.smart-select-trigger').setAttribute('aria-expanded', 'false');
  });
}

function bindSmartSelect(id, onChange) {
  const select = $(id);
  const root = select.closest('.smart-select');
  const trigger = root.querySelector('.smart-select-trigger');
  const current = root.querySelector('.smart-select-current');
  const menu = root.querySelector('.smart-select-menu');
  const options = [...menu.querySelectorAll('[data-value]')];

  const render = () => {
    const selected = options.find(option => option.dataset.value === select.value) || options[0];
    const title = selected.querySelector('strong').textContent;
    const detail = selected.querySelector('small')?.textContent || '';
    current.replaceChildren();
    const strong = document.createElement('strong');
    strong.textContent = title;
    current.appendChild(strong);
    if (detail) {
      const small = document.createElement('small');
      small.textContent = detail;
      current.appendChild(small);
    }
    options.forEach(option => {
      const active = option === selected;
      option.classList.toggle('selected', active);
      option.setAttribute('aria-selected', String(active));
    });
  };
  root._renderSmartSelect = render;

  trigger.onclick = event => {
    event.stopPropagation();
    const opening = !root.classList.contains('open');
    closeSmartSelects(root);
    root.classList.toggle('open', opening);
    menu.classList.toggle('hidden', !opening);
    trigger.setAttribute('aria-expanded', String(opening));
  };
  options.forEach(option => {
    option.onclick = event => {
      event.stopPropagation();
      select.value = option.dataset.value;
      render();
      closeSmartSelects();
      if (onChange) onChange();
      trigger.focus();
    };
  });
  select.addEventListener('change', () => {
    render();
    if (onChange) onChange();
  });
  render();
}

function syncSmartSelect(id) {
  const root = $(id).closest('.smart-select');
  if (root && root._renderSmartSelect) root._renderSmartSelect();
}

function updateSmsMethodUi() {
  const emailMode = $('sms-method').value === 'email';
  $('sms-email-fields').classList.toggle('hidden', !emailMode);
  $('btn-test-sms-email').classList.toggle('hidden', !emailMode);
  $('sms-method-hint').textContent = emailMode
    ? '适用于手机端邮件转发方案：短信到达后发送固定主题邮件，软件通过 IMAP SSL 拉取正文并提取验证码。'
    : '通过手机与 Windows“手机连接”的消息同步，从 Windows 通知中心实时读取验证码。';
}

function syncSmsModalCopy() {
  const emailMode = CFG?.sms?.method === 'email';
  $('sms-kicker').textContent = emailMode ? 'EMAIL VERIFICATION' : 'SMS VERIFICATION';
  $('sms-title').textContent = emailMode ? '邮箱验证码' : '短信验证码';
  $('sms-lead').textContent = emailMode
    ? '正在通过 IMAP 邮箱捕获验证码，识别成功后会自动继续。'
    : '正在等待“手机连接”同步验证码，捕获成功后会自动继续。';
  $('sms-callout').textContent = emailMode
    ? '邮件暂未到达时，可直接输入收到的验证码。关闭弹框不会停止后台自动捕获。'
    : '未连接手机时，可直接输入收到的验证码。关闭弹框不会停止后台自动捕获。';
  $('sms-code').placeholder = emailMode ? '邮件验证码' : '短信验证码';
}

function applyEmailProviderPreset(force = false) {
  const provider = $('sms-email-provider').value;
  const host = EMAIL_PROVIDER_HOSTS[provider];
  if (host && (force || !$('sms-email-host').value.trim())) {
    $('sms-email-host').value = host;
    $('sms-email-port').value = '993';
  }
}

function readSmsSettingsForm() {
  const method = $('sms-method').value;
  const port = Number($('sms-email-port').value || 993);
  const email = {
    provider: $('sms-email-provider').value,
    host: $('sms-email-host').value.trim(),
    port,
    username: $('sms-email-user').value.trim(),
    password: $('sms-email-password').value,
    mailbox: $('sms-email-mailbox').value.trim() || 'INBOX',
    subject: $('sms-email-subject').value.trim(),
    sender: $('sms-email-sender').value.trim(),
    poll_interval: 3
  };
  if (method === 'email') {
    if (!email.host || !email.username || !email.password) {
      return { ok: false, msg: '请完整填写 IMAP 服务器、邮箱账号和授权码' };
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return { ok: false, msg: 'IMAP SSL 端口需在 1–65535 之间' };
    }
  }
  return { ok: true, sms: { method, email } };
}

async function persistSmsSettings(sms) {
  const previous = CFG.sms;
  CFG.sms = sms;
  try {
    const saved = await api().save_config(CFG);
    if (saved === false) throw new Error('后端未保存配置');
  } catch (e) {
    CFG.sms = previous;
    throw e;
  }
}

async function saveSmsSettings() {
  const form = readSmsSettingsForm();
  if (!form.ok) {
    setFeedback($('sms-settings-result'), form.msg, 'bad');
    return;
  }
  await runBusy($('btn-save-sms'), '保存中…', async () => {
    setFeedback($('sms-settings-result'), '正在保存…', 'loading');
    try {
      await persistSmsSettings(form.sms);
      const label = form.sms.method === 'email' ? '邮箱转发' : 'Windows 手机连接';
      const msg = `验证码接收方式已切换为：${label}`;
      setFeedback($('sms-settings-result'), msg, 'ok');
      toast({ ok: true, msg });
    } catch (e) {
      setFeedback($('sms-settings-result'), `保存失败：${friendlyError(e)}`, 'bad');
    }
  });
}

async function testSmsEmailSettings() {
  const form = readSmsSettingsForm();
  if (!form.ok) {
    setFeedback($('sms-settings-result'), form.msg, 'bad');
    return;
  }
  await runBusy($('btn-test-sms-email'), '测试中…', async () => {
    setFeedback($('sms-settings-result'), '正在保存并只读连接邮箱…', 'loading');
    try {
      await persistSmsSettings(form.sms);
      const result = normalizeResult(await api().test_sms_email(), '邮箱连接正常');
      setFeedback($('sms-settings-result'), result.msg, result.ok ? 'ok' : 'bad');
      toast(result);
    } catch (e) {
      setFeedback($('sms-settings-result'), `测试失败：${friendlyError(e)}`, 'bad');
    }
  });
}

function bindVerificationFlows() {
  $('btn-sms-submit').onclick = async () => {
    const value = $('sms-code').value.trim();
    if (!value) {
      $('sms-code').focus();
      return;
    }
    await api().submit_sms_code(value);
    await hideBlockingBrowserModal($('smsmodal'));
  };
  $('btn-sms-close').onclick = async () => {
    await api().dismiss_sms_ui();
    await hideBlockingBrowserModal($('smsmodal'));
  };

  $('cap-img').onclick = (e) => {
    if (capPoints.length >= 3) return;
    const image = $('cap-img');
    const rect = image.getBoundingClientRect();
    const sx = 320 / rect.width;
    const sy = 240 / rect.height;
    const x = (e.clientX - rect.left) * sx;
    const y = (e.clientY - rect.top) * sy;
    if (y > 160) return;
    capPoints.push([Math.round(x), Math.round(y)]);
    const mark = document.createElement('span');
    mark.textContent = capPoints.length;
    mark.style.left = `${x / sx}px`;
    mark.style.top = `${y / sy}px`;
    $('cap-marks').appendChild(mark);
  };
  $('btn-cap-reset').onclick = () => {
    capPoints = [];
    $('cap-marks').innerHTML = '';
  };
  $('btn-cap-submit').onclick = async () => {
    if (capPoints.length !== 3) {
      toast({ ok: false, msg: '请按顺序选择 3 个图标' });
      return;
    }
    await api().submit_manual_clicks(capPoints);
    capPoints = [];
    await hideBlockingBrowserModal($('capmodal'));
  };

  document.addEventListener('keydown', async (e) => {
    if (e.key !== 'Escape') return;
    if (!$('m-type-list').classList.contains('hidden')) {
      $('m-type-list').classList.add('hidden');
      return;
    }
    const visible = [...document.querySelectorAll('.modal:not(.hidden)')].pop();
    if (visible) await closeModal(visible);
  });
}

async function goToPage(page) {
  const item = document.querySelector(`#nav [data-page="${page}"]`);
  const panel = $(`page-${page}`);
  if (!panel) return;
  const activePage = item ? page : page === 'routing' ? 'proxy' : page;
  document.querySelectorAll('#nav [data-page]').forEach(candidate => {
    const selected = candidate.dataset.page === activePage;
    candidate.classList.toggle('active', selected);
    if (selected) candidate.setAttribute('aria-current', 'page');
    else candidate.removeAttribute('aria-current');
  });
  document.querySelectorAll('.page').forEach(candidate => candidate.classList.remove('active'));
  panel.classList.add('active');
  curPage = page;
  window.dispatchEvent(new CustomEvent('cxvpn:pagechange', { detail: { page } }));
  await syncBrowserVisibility();
  if (page === 'vpns') {
    await ensureVpnsLoaded();
    if (vpnListReady) renderVpnList(VPN_LIST);
  }
  if (page === 'browser') await refreshBrState();
  if (page === 'logs' && window.RoutingActivityWorkspace?.runtimeLogsVisible?.()) await updateLogs();
}

async function saveToggle(input, key, enabledMessage, disabledMessage) {
  const previous = !!CFG[key];
  const next = input.checked;
  CFG[key] = next;
  input.disabled = true;
  try {
    const result = await api().save_config(CFG);
    if (result === false) throw new Error('后端未保存配置');
    toast({ ok: true, msg: next ? enabledMessage : disabledMessage });
  } catch (e) {
    CFG[key] = previous;
    input.checked = previous;
    toast({ ok: false, msg: `保存失败，已恢复原设置：${friendlyError(e)}` });
  } finally {
    input.disabled = false;
  }
}

async function loadDesktopSettings() {
  const input = $('ov-startup');
  try {
    const state = await api().get_desktop_settings();
    input.checked = !!state.startup_enabled;
    if (typeof state.close_to_tray === 'boolean') {
      CFG.close_to_tray = state.close_to_tray;
      $('ov-tray').checked = state.close_to_tray;
    }
    input.disabled = false;
  } catch (e) {
    input.checked = false;
    input.disabled = true;
    input.title = `无法读取开机自启状态：${friendlyError(e)}`;
  }
}

async function saveStartupToggle(input) {
  const next = input.checked;
  const previous = !next;
  input.disabled = true;
  try {
    const result = normalizeResult(
      await api().set_startup_enabled(next),
      next ? '已开启开机自启' : '已关闭开机自启');
    if (!result.ok) throw new Error(result.msg);
    toast(result);
  } catch (e) {
    input.checked = previous;
    toast({ ok: false, msg: `设置失败，已恢复原状态：${friendlyError(e)}` });
  } finally {
    input.disabled = false;
  }
}

async function connectVpn(name, button, options = {}) {
  if (connectionBusy || !name) return { skipped: true };
  connectionBusy = true;
  let finalResult = null;
  const original = captureConnectionButton(button);
  setConnectionButtonBusy(button, '正在连接');
  try {
    let selectedMode = options.mode || '';
    let result = normalizeResult(
      await api().connect_vpn(name, selectedMode, !!options.afterAuthorization), `${name} 已连接`);
    if (result.needs_mode_choice) {
      setConnectionButtonBusy(button, '等待选择');
      selectedMode = await chooseConnectionMode(result);
      if (!selectedMode) return { cancelled: true };
      setConnectionButtonBusy(button, selectedMode === 'switch' ? '正在切换' : '正在连接');
      result = normalizeResult(
        await api().connect_vpn(name, selectedMode, !!options.afterAuthorization), `${name} 已连接`);
    }
    if (result.route_warning) {
      toast({ ...result, tone: 'warning',
        msg: `${result.msg}；${result.route_warning}` });
    } else {
      toast(result);
    }
    if (!result.ok && result.needs_credentials) {
      await openCredential(name, {
        retryConnection: true,
        mode: selectedMode,
        afterAuthorization: !!options.afterAuthorization
      });
    }
    if (result.ok && (CFG.credential_status || {})[name]) {
      CFG = await api().get_config();
      fillForms();
    }
    finalResult = result;
  } catch (e) {
    finalResult = { ok: false, msg: `连接失败：${friendlyError(e)}` };
    toast(finalResult);
  } finally {
    restoreConnectionButton(button, original);
    await finishConnectionAction();
  }
  return finalResult;
}

async function disconnectVpn(name, button) {
  if (connectionBusy || !name) return;
  connectionBusy = true;
  const original = captureConnectionButton(button);
  setConnectionButtonBusy(button, '正在断开');
  try {
    toast(normalizeResult(
      await api().disconnect_vpn(name), `${name} 已断开`));
  } catch (e) {
    toast({ ok: false, msg: `断开失败：${friendlyError(e)}` });
  } finally {
    restoreConnectionButton(button, original);
    await finishConnectionAction();
  }
}

function captureConnectionButton(button) {
  if (!button) return null;
  return {
    html: button.innerHTML,
    label: button.getAttribute('aria-label'),
    title: button.getAttribute('title'),
  };
}

function setConnectionButtonBusy(button, label) {
  if (!button) return;
  button.disabled = true;
  if (button.classList.contains('connection-icon-action')) {
    button.classList.add('is-busy');
    button.innerHTML = '<span class="connection-action-spinner" aria-hidden="true"></span>';
    button.setAttribute('aria-label', label);
    button.title = label;
    return;
  }
  button.textContent = `${label}…`;
}

function restoreConnectionButton(button, original) {
  if (!button || !original) return;
  button.classList.remove('is-busy');
  button.innerHTML = original.html;
  if (original.label === null) button.removeAttribute('aria-label');
  else button.setAttribute('aria-label', original.label);
  if (original.title === null) button.removeAttribute('title');
  else button.title = original.title;
}

async function disconnectAll(button) {
  if (connectionBusy) return;
  const confirmed = await confirmAction({
    title: '断开全部 VPN？',
    message: '将断开当前所有 VPN 连接，正在访问的内网资源可能立即中断。',
    confirmText: '断开全部'
  });
  if (!confirmed) return;
  connectionBusy = true;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '断开中…';
  try {
    toast(normalizeResult(await api().disconnect_all(), '已断开全部 VPN'));
  } catch (e) {
    toast({ ok: false, msg: `断开失败：${friendlyError(e)}` });
  } finally {
    button.textContent = original;
    await finishConnectionAction();
  }
}

function chooseConnectionMode(result) {
  if (connectChoiceResolve) finishConnectChoice(null);
  $('connect-title').textContent = `连接 ${result.name}`;
  $('connect-message').textContent =
    `当前已连接：${(result.active_names || []).join('、')}。请选择本次连接方式。`;
  $('connect-warning').textContent = result.route_warning ||
    '多个 VPN 可能同时修改默认路由或使用重叠网段。';
  $('connectmodal').classList.remove('hidden');
  setTimeout(() => $('btn-connect-switch').focus(), 0);
  return new Promise(resolve => { connectChoiceResolve = resolve; });
}

function finishConnectChoice(mode) {
  $('connectmodal').classList.add('hidden');
  const resolve = connectChoiceResolve;
  connectChoiceResolve = null;
  if (resolve) resolve(mode);
}

async function finishConnectionAction() {
  connectionBusy = false;
  try {
    const [state, status] = await Promise.all([
      api().get_state(), api().vpn_status()
    ]);
    updateOverview(state, status);
  } catch (e) {
    console.error('连接状态刷新失败', e);
    updateConnectionControls(VPN_STATUS);
  }
  void refreshIpInfo(true);
  setTimeout(refreshUiStateOnce, 0);
}

async function saveVpnModal() {
  const name = $('m-name').value.trim();
  const server = $('m-server').value.trim();
  const l2tpPsk = $('m-l2tp-key').value;
  const user = $('m-user').value.trim();
  const password = $('m-pass').value;
  const ipv4DefaultGateway = $('m-ipv4-gateway').checked;
  const ipv6DefaultGateway = $('m-ipv6-gateway').checked;
  if (!name) {
    setFeedback($('m-result'), '请填写 VPN 名称', 'bad');
    $('m-name').focus();
    return;
  }
  if (!server || /\s/.test(server)) {
    setFeedback($('m-result'), server ? '服务器地址不能包含空格' : '请填写服务器地址', 'bad');
    $('m-server').focus();
    return;
  }
  if (!editing && ((user && !password) || (!user && password))) {
    setFeedback($('m-result'), '用户名和密码需要同时填写，或同时留空', 'bad');
    (user ? $('m-pass') : $('m-user')).focus();
    return;
  }
  await runBusy($('btn-modal-ok'), '保存中…', async () => {
    setFeedback($('m-result'), '正在写入 Windows VPN 配置…', 'loading');
    try {
      const created = !editing && !modalCreatedName;
      const raw = (editing || modalCreatedName)
        ? await api().update_vpn(editing || modalCreatedName, server, ddValue,
          l2tpPsk, ipv4DefaultGateway, ipv6DefaultGateway)
        : await api().add_vpn(name, server, ddValue, l2tpPsk,
          ipv4DefaultGateway, ipv6DefaultGateway);
      const result = normalizeResult(raw, editing ? 'VPN 配置已更新' : 'VPN 配置已创建');
      if (!result.ok) {
        setFeedback($('m-result'), result.msg, 'bad');
        return;
      }
      if (created) modalCreatedName = name;
      $('m-name').disabled = true;
      await syncVpnUi({ forceRefresh: true, refreshConfig: true });
      if (!editing && user && password) {
        setFeedback($('m-result'), 'VPN 已创建，正在将账号密码写入 Windows…', 'loading');
        const credentialResult = normalizeResult(
          await api().save_cred(name, user, password), '凭据同步成功');
        await syncVpnUi({ forceRefresh: true, refreshConfig: true });
        if (!credentialResult.ok) {
          setFeedback($('m-result'), `VPN 已创建，但凭据同步未完成：${credentialResult.msg}`, 'bad');
          toast({ ...credentialResult, tone: 'warning' });
          return;
        }
        result.msg = `${name} 已创建，账号密码已同步到 Windows`;
      }
      setFeedback($('m-result'), result.msg, 'ok');
      toast(result);
      setTimeout(() => $('modal').classList.add('hidden'), 450);
    } catch (e) {
      setFeedback($('m-result'), `保存失败：${friendlyError(e)}`, 'bad');
    }
  });
}

async function saveCredential() {
  if (!credName) return;
  const user = $('c-user').value.trim();
  if (!user) {
    setFeedback($('cred-result'), '请填写 VPN 用户名', 'bad');
    $('c-user').focus();
    return;
  }
  await runBusy($('btn-cred-ok'), '保存中…', async () => {
    setFeedback($('cred-result'), '正在将账号密码同步到 Windows…', 'loading');
    try {
      const result = normalizeResult(
        await api().save_cred(credName, user, $('c-pass').value),
        '账号密码已保存');
      setFeedback($('cred-result'), result.ok
        ? '账号密码已保存到软件并同步到 Windows'
        : result.tool_saved
          ? result.msg
          : `保存失败：${result.msg}`, result.ok ? 'ok' : 'bad');
      if (result.ok) {
        await syncVpnUi({ forceRefresh: true, refreshConfig: true });
        const retry = credentialConnectRetry;
        credentialConnectRetry = null;
        $('credmodal').classList.add('hidden');
        if (retry) {
          toast({ ok: true, tone: 'info',
            msg: `${credName} 的凭据已保存，正在继续连接` });
          await connectVpn(retry.name, null, retry);
        } else {
          toast({ ok: true, msg: `${credName} 的凭据已保存` });
        }
      }
    } catch (e) {
      setFeedback($('cred-result'), `保存失败：${friendlyError(e)}`, 'bad');
    }
  });
}

async function saveCxSettings() {
  const phone = $('cx-phone').value.trim();
  const hours = Number($('cx-hours').value);
  if (!/^1\d{10}$/.test(phone)) {
    setFeedback($('cx-result'), '请输入 11 位中国大陆手机号', 'bad');
    $('cx-phone').focus();
    return;
  }
  if (!Number.isFinite(hours) || hours < 1 || hours > 8) {
    setFeedback($('cx-result'), '续期间隔需在 1–8 小时之间', 'bad');
    $('cx-hours').focus();
    return;
  }
  const previous = { phone: CFG.phone, renew_hours: CFG.renew_hours };
  await runBusy($('btn-save-cx'), '保存中…', async () => {
    CFG.phone = phone;
    CFG.renew_hours = hours;
    setFeedback($('cx-result'), '正在保存…', 'loading');
    try {
      const saved = await api().save_config(CFG);
      if (saved === false) throw new Error('后端未保存配置');
      setFeedback($('cx-result'), '超星账号配置已保存', 'ok');
      toast({ ok: true, msg: '超星账号配置已保存' });
    } catch (e) {
      Object.assign(CFG, previous);
      setFeedback($('cx-result'), `保存失败：${friendlyError(e)}`, 'bad');
    }
  });
}

function readVlmForm() {
  const vlm = {
    base: $('vlm-base').value.trim(),
    key: $('vlm-key').value.trim(),
    model: $('vlm-model').value.trim()
  };
  const count = Object.values(vlm).filter(Boolean).length;
  if (count > 0 && count < 3) return { ok: false, msg: '请完整填写 Base URL、API Key 和模型名称' };
  if (vlm.base && !/^https?:\/\//i.test(vlm.base)) {
    return { ok: false, msg: 'Base URL 需以 http:// 或 https:// 开头' };
  }
  return { ok: true, vlm };
}

async function persistVlm(vlm) {
  const previous = CFG.vlm;
  CFG.vlm = vlm;
  try {
    const saved = await api().save_config(CFG);
    if (saved === false) throw new Error('后端未保存配置');
  } catch (e) {
    CFG.vlm = previous;
    throw e;
  }
}

async function saveVlmSettings() {
  const form = readVlmForm();
  if (!form.ok) {
    setFeedback($('vlm-result'), form.msg, 'bad');
    return;
  }
  await runBusy($('btn-save-vlm'), '保存中…', async () => {
    setFeedback($('vlm-result'), '正在保存…', 'loading');
    try {
      await persistVlm(form.vlm);
      const msg = form.vlm.base ? 'AI 模型配置已保存' : 'AI 模型配置已清空';
      setFeedback($('vlm-result'), msg, 'ok');
      toast({ ok: true, msg });
    } catch (e) {
      setFeedback($('vlm-result'), `保存失败：${friendlyError(e)}`, 'bad');
    }
  });
}

async function testVlmSettings() {
  const form = readVlmForm();
  if (!form.ok || !form.vlm.base) {
    setFeedback($('vlm-result'), form.msg || '请先完整填写模型配置', 'bad');
    return;
  }
  await runBusy($('btn-test-vlm'), '测试中…', async () => {
    setFeedback($('vlm-result'), '正在保存当前配置并测试连通性…', 'loading');
    try {
      await persistVlm(form.vlm);
      const result = normalizeResult(await api().test_vlm(), '模型接口连通正常');
      setFeedback($('vlm-result'), result.msg, result.ok ? 'ok' : 'bad');
      toast(result);
    } catch (e) {
      setFeedback($('vlm-result'), `测试失败：${friendlyError(e)}`, 'bad');
    }
  });
}

async function openCap(cap) {
  capId = cap.id;
  capPoints = [];
  $('cap-marks').innerHTML = '';
  $('cap-img').src = cap.img;
  if (cap.tip) {
    $('cap-tip').src = cap.tip;
    $('cap-tip').style.display = 'inline-block';
  } else {
    $('cap-tip').style.display = 'none';
  }
  await showBlockingBrowserModal($('capmodal'));
}

function fillForms() {
  $('cx-phone').value = CFG.phone || '';
  $('cx-hours').value = CFG.renew_hours || 7;
  $('vlm-base').value = (CFG.vlm && CFG.vlm.base) || '';
  $('vlm-key').value = (CFG.vlm && CFG.vlm.key) || '';
  $('vlm-model').value = (CFG.vlm && CFG.vlm.model) || '';
  const sms = CFG.sms || {};
  const email = sms.email || {};
  $('sms-method').value = sms.method === 'email' ? 'email' : 'phone_link';
  $('sms-email-provider').value = email.provider || 'custom';
  $('sms-email-host').value = email.host || '';
  $('sms-email-port').value = email.port || 993;
  $('sms-email-user').value = email.username || '';
  $('sms-email-password').value = email.password || '';
  $('sms-email-mailbox').value = email.mailbox || 'INBOX';
  $('sms-email-subject').value = email.subject || '超星验证码';
  $('sms-email-sender').value = email.sender || '';
  syncSmartSelect('sms-method');
  syncSmartSelect('sms-email-provider');
  applyEmailProviderPreset(false);
  updateSmsMethodUi();
  $('ov-auto').checked = CFG.auto_renew === true;
  $('ov-autoconn').checked = CFG.auto_connect === true;
  $('ov-tray').checked = CFG.close_to_tray === true;
  $('ov-hours').textContent = '待同步';
  $('ov-renew-hours').textContent = '待同步';
}

function configuredVpnPreviewProfiles(config = CFG) {
  const credentialNames = config && config.creds &&
    typeof config.creds === 'object' && !Array.isArray(config.creds)
    ? Object.keys(config.creds) : [];
  const names = [config?.vpn_name, ...credentialNames];
  const seen = new Set();
  return names.reduce((profiles, value) => {
    const name = String(value || '').trim();
    const key = name.toLocaleLowerCase();
    if (!name || seen.has(key)) return profiles;
    seen.add(key);
    profiles.push({
      name,
      server: '',
      type: '',
      status: 'Checking',
      pending_system_check: true,
    });
    return profiles;
  }, []);
}

function showConfiguredVpnPreview() {
  const profiles = configuredVpnPreviewProfiles();
  const displayCapacity = Math.max(5, profiles.length);
  $('ov-name').textContent = CFG.vpn_name || '尚未设置默认 VPN';
  $('ov-conn').textContent = `核对中 / ${displayCapacity} 个`;
  $('ov-active-count').textContent = profiles.length
    ? `${profiles.length} 个已保存配置 · 正在核对`
    : '正在核对系统 VPN';
  if (CFG.vpn_name) {
    $('ov-default-status').textContent = '正在核对';
    $('ov-default-type').textContent = '读取中';
    $('ov-default-server').textContent = '读取中';
  }
  renderVpnConnections(profiles, [], {});
}

function setVpnLoadingState() {
  const tbody = $('vpn-tbody');
  $('vpn-count').textContent = '正在读取';
  $('vpn-connected-count').textContent = '检查系统连接状态';
  tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state"><strong>正在读取 VPN 配置…</strong></div></td></tr>';
}

function ensureVpnsLoaded({ background = false } = {}) {
  if (vpnListReady) return Promise.resolve(VPN_LIST);
  if (!background) setVpnLoadingState();
  return startVpnLoad({
    showLoading: false,
    notifyError: !background,
  });
}

function reloadVpns({ announce = false, forceRefresh = false } = {}) {
  return startVpnLoad({
    announce,
    forceRefresh,
    showLoading: !vpnListReady,
    notifyError: true,
  });
}

function startVpnLoad(options) {
  if (vpnLoadPromise) {
    if (!options.forceRefresh || vpnLoadIncludesForceRefresh) return vpnLoadPromise;
    const current = vpnLoadPromise;
    vpnLoadIncludesForceRefresh = true;
    let queued;
    queued = current.then(() => loadVpns(options)).finally(() => {
      if (vpnLoadPromise === queued) {
        vpnLoadPromise = null;
        vpnLoadIncludesForceRefresh = false;
      }
    });
    vpnLoadPromise = queued;
    return queued;
  }
  vpnLoadIncludesForceRefresh = !!options.forceRefresh;
  let pending;
  pending = loadVpns(options).finally(() => {
    if (vpnLoadPromise === pending) {
      vpnLoadPromise = null;
      vpnLoadIncludesForceRefresh = false;
    }
  });
  vpnLoadPromise = pending;
  return pending;
}

async function loadVpns({ announce = false, forceRefresh = false,
  showLoading = true, notifyError = true } = {}) {
  const tbody = $('vpn-tbody');
  const hadUsableList = vpnListReady;
  if (showLoading) setVpnLoadingState();
  try {
    const list = await api().list_vpns(forceRefresh);
    setVpnProfiles(list, { render: true });
    if (announce) toast({ ok: true, msg: `已刷新，共 ${list.length} 条 VPN` });
    return list;
  } catch (e) {
    if (hadUsableList) {
      if (notifyError) toast({
        ok: false,
        msg: `VPN 列表刷新失败，已保留当前内容：${friendlyError(e)}`,
      });
      return VPN_LIST;
    }
    vpnListReady = false;
    $('vpn-count').textContent = '读取失败';
    $('vpn-connected-count').textContent = friendlyError(e);
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><strong>读取失败</strong><span>${esc(friendlyError(e))}</span></div></td></tr>`;
    if (notifyError) toast({ ok: false, msg: `VPN 列表读取失败：${friendlyError(e)}` });
    return [];
  }
}

function setVpnProfiles(profiles, { render = false } = {}) {
  if (!Array.isArray(profiles)) return false;
  VPN_LIST = profiles;
  vpnListReady = true;
  if (render) renderVpnList(VPN_LIST);
  return true;
}

function vpnRowKey(vpn) {
  return JSON.stringify([
    vpn.name, vpn.server, vpn.type, vpn.status,
    vpn.ip_address || '', vpn.prefix_length || 0,
    vpn.ipv6_address || '', vpn.ipv6_prefix_length || 0,
    vpn.ipv4_default_gateway !== false, vpn.ipv6_default_gateway !== false,
    vpn.name === CFG.vpn_name,
    (CFG.credential_status || {})[vpn.name] || '',
  ]);
}

function renderVpnList(list) {
  const tbody = $('vpn-tbody');
  const connectedCount = list.filter(vpn => vpn.status === 'Connected').length;
  $('vpn-count').textContent = `${list.length} 个 VPN 连接`;
  $('vpn-connected-count').textContent = connectedCount
    ? `${connectedCount} 个已连接 · ${CFG.vpn_name ? `默认：${CFG.vpn_name}` : '尚未设置默认 VPN'}`
    : `当前均未连接 · ${CFG.vpn_name ? `默认：${CFG.vpn_name}` : '尚未设置默认 VPN'}`;
  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state"><strong>暂无 VPN 配置</strong><span>点击“新增 VPN”创建第一条连接</span></div></td></tr>';
    return;
  }
  const existing = new Map(Array.from(
    tbody.querySelectorAll('tr[data-vpn-name]'),
    row => [row.dataset.vpnName, row]
  ));
  for (const vpn of list) {
    let row = existing.get(vpn.name);
    const key = vpnRowKey(vpn);
    if (!row || row.dataset.vpnKey !== key) {
      const updated = renderVpnRow(vpn);
      if (row) row.replaceWith(updated);
      row = updated;
    }
    tbody.appendChild(row);
    existing.delete(vpn.name);
  }
  for (const row of existing.values()) row.remove();
  for (const row of tbody.querySelectorAll('tr:not([data-vpn-name])')) row.remove();
}

function renderVpnRow(vpn) {
  const row = document.createElement('tr');
  row.dataset.vpnName = vpn.name;
  row.dataset.vpnKey = vpnRowKey(vpn);
  const isDefault = vpn.name === CFG.vpn_name;
  const connected = vpn.status === 'Connected';
  const validationStatus = (CFG.credential_status || {})[vpn.name] || '';
  const validationTag = validationStatus === 'windows_sync_failed'
    ? ' <span class="credential-tag failed">Windows 同步失败</span>' : '';
  row.classList.toggle('is-target', isDefault);
  const address = vpn.ip_address
    ? `<br><span class="cell-detail">${esc(vpn.ip_address)}${vpn.prefix_length ? `/${esc(vpn.prefix_length)}` : ''}</span>`
    : '';
  const gateway = `<br><span class="cell-detail">${esc(gatewaySummary(vpn))}</span>`;
  row.innerHTML =
    `<td>${esc(vpn.name)}${isDefault ? ' <span class="target">默认</span>' : ''}${validationTag}</td>` +
    `<td class="cell-secondary">${esc(vpn.server)}${address}${gateway}</td><td>${esc(vpn.type)}</td>` +
    `<td>${connected ? '<span class="pill on">已连接</span>' : '<span class="pill off">未连接</span>'}</td>` +
    '<td class="right"><div class="vpn-actions"></div></td>';
  const actions = row.children[4].firstElementChild;
  const connectionButton = connected
    ? btn('断开', 'mini ghost', event => disconnectVpn(vpn.name, event.currentTarget))
    : btn('连接', 'mini primary', event => connectVpn(vpn.name, event.currentTarget));
  connectionButton.dataset.vpnAction = 'true';
  actions.appendChild(connectionButton);
  const defaultButton = isDefault
    ? btn('已默认', 'mini default-state', null)
    : btn('设为默认', 'mini', () => setDefaultVpn(vpn.name));
  if (isDefault) {
    defaultButton.disabled = true;
    defaultButton.title = '当前默认 VPN';
  }
  actions.appendChild(defaultButton);
  actions.appendChild(btn('凭据', 'mini ghost', () => openCredential(vpn.name)));
  actions.appendChild(btn('编辑', 'mini ghost', () => openModal(vpn)));
  actions.appendChild(btn('删除', 'mini danger', () => deleteVpn(vpn.name)));
  return row;
}

function gatewaySummary(vpn) {
  const families = [];
  if (vpn.ipv4_default_gateway !== false) families.push('IPv4');
  if (vpn.ipv6_default_gateway !== false) families.push('IPv6');
  return families.length
    ? `默认网关：${families.join(' + ')}`
    : '默认网关：均关闭（分流）';
}

async function setDefaultVpn(name) {
  const previous = CFG.vpn_name;
  CFG.vpn_name = name;
  try {
    const saved = await api().save_config(CFG);
    if (saved === false) throw new Error('后端未保存配置');
    toast({ ok: true, msg: `已将 ${name} 设为默认 VPN` });
    await syncVpnUi({ forceRefresh: true });
  } catch (e) {
    CFG.vpn_name = previous;
    toast({ ok: false, msg: `设置失败：${friendlyError(e)}` });
  }
}

async function openCredential(name, options = {}) {
  credName = name;
  credentialConnectRetry = options.retryConnection
    ? { name, mode: options.mode || '',
      afterAuthorization: !!options.afterAuthorization }
    : null;
  $('cred-title').textContent = `凭据 · ${name}`;
  setFeedback($('cred-result'), '正在读取凭据…', 'loading');
  $('credmodal').classList.remove('hidden');
  try {
    const cred = await api().get_cred(name);
    $('c-user').value = cred.user || '';
    $('c-pass').value = cred.pass || '';
    concealSecret($('c-pass'), $('c-eye'));
    const windowsSaved = cred.has_saved && !cred.stored;
    $('c-pass').placeholder = cred.stored
      ? '已保存，留空表示保持不变'
      : (windowsSaved ? 'Windows 已保存但无法读取明文，请补录一次' : '输入 VPN 密码');
    $('cred-status').textContent = cred.validation_status === 'windows_sync_failed'
      ? '账号密码已保存到软件，但 Windows 同步失败；请重新提交'
      : cred.stored
        ? (options.retryConnection
          ? '账号密码已保存；本次保存成功后将继续刚才的连接'
          : '账号密码已保存到软件并同步到 Windows；主动保存不会自动连接或启动网页授权')
        : (windowsSaved
          ? 'Windows 已保存旧凭据；请在此补录一次，之后软件可直接连接'
          : 'Windows 和本工具均未检测到可用凭据');
    $('cred-status').className = `credential-status ${(cred.stored || windowsSaved) ? 'stored' : ''}`;
    setFeedback($('cred-result'), '', '');
    $('c-user').focus();
  } catch (e) {
    setFeedback($('cred-result'), `读取失败：${friendlyError(e)}`, 'bad');
  }
}

async function deleteVpn(name) {
  const confirmed = await confirmAction({
    title: '删除 VPN 配置？',
    message: `将从 Windows 中删除“${name}”，并清理本工具保存的默认项与凭据引用。此操作无法撤销。`,
    confirmText: '删除'
  });
  if (!confirmed) return;
  try {
    const result = normalizeResult(await api().remove_vpn(name), 'VPN 配置已删除');
    toast(result);
    if (result.ok) {
      await syncVpnUi({ forceRefresh: true, refreshConfig: true });
    }
  } catch (e) {
    toast({ ok: false, msg: `删除失败：${friendlyError(e)}` });
  }
}

function btn(text, cls, onclick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `btn ${cls}`;
  button.textContent = text;
  button.onclick = onclick;
  return button;
}

function openModal(vpn) {
  editing = vpn ? vpn.name : null;
  modalCreatedName = null;
  $('modal-title').textContent = vpn ? '编辑 VPN' : '新增 VPN';
  $('m-name').value = vpn ? vpn.name : '';
  $('m-name').disabled = !!vpn;
  $('m-server').value = vpn ? vpn.server : '';
  ddValue = vpn ? vpn.type : 'Automatic';
  $('m-type-btn').textContent = ddValue;
  $('m-l2tp-key').value = '';
  $('m-l2tp-key').placeholder = vpn
    ? '留空保持现有密钥或证书设置'
    : '使用证书认证时留空';
  concealSecret($('m-l2tp-key'), $('m-l2tp-eye'));
  $('m-user').value = '';
  $('m-pass').value = '';
  concealSecret($('m-pass'), $('m-pass-eye'));
  $('m-ipv4-gateway').checked = vpn
    ? vpn.ipv4_default_gateway !== false : true;
  $('m-ipv6-gateway').checked = vpn
    ? vpn.ipv6_default_gateway !== false : true;
  updateVpnModalFields();
  $('m-type-list').classList.add('hidden');
  setFeedback($('m-result'), '', '');
  $('modal').classList.remove('hidden');
  (vpn ? $('m-server') : $('m-name')).focus();
}

function updateVpnModalFields() {
  $('m-l2tp-fields').classList.toggle('hidden', ddValue !== 'L2tp');
  $('m-credential-fields').classList.toggle('hidden', !!editing);
}

async function syncVpnUi({ forceRefresh = false, refreshConfig = false } = {}) {
  if (refreshConfig) {
    CFG = await api().get_config();
    fillForms();
  }
  await reloadVpns({ forceRefresh });
  const [state, status] = await Promise.all([api().get_state(), api().vpn_status()]);
  updateOverview(state, status);
}

function applyUiStateSnapshot(snapshot) {
  if (!snapshot) return;
  updateOverview(snapshot.state || {}, snapshot.vpn_status || {});
  if (snapshot.ip_info) renderIpInfo(snapshot.ip_info);
  blockingModalSync = blockingModalSync
    .then(() => syncBlockingModals(snapshot.captcha, snapshot.sms))
    .catch(error => console.error('阻塞弹窗状态同步失败', error));
  const logsVersion = Number(snapshot.logs_version ?? -1);
  if (curPage === 'logs' && window.RoutingActivityWorkspace?.runtimeLogsVisible?.() && logsVersion !== lastLogsVersion) {
    lastLogsVersion = logsVersion;
    void updateLogs();
  }
  if (curPage === 'browser') void refreshBrState(snapshot.browser || null);
  window.dispatchEvent(new CustomEvent('cxvpn:uistate', { detail: snapshot }));
}

async function startUiStateSubscription() {
  const initial = await api().get_ui_state_snapshot();
  if (initial?.changed && initial.snapshot) {
    uiStateVersion = Number(initial.version || 0);
    applyUiStateSnapshot(initial.snapshot);
  }
  void uiStateSubscriptionLoop();
}

async function uiStateSubscriptionLoop() {
  while (!uiStateSubscriptionStopped) {
    let retry = false;
    try {
      const result = await api().wait_ui_state(uiStateVersion, 25);
      if (result?.resync) uiStateVersion = 0;
      if (result?.changed && result.snapshot) {
        uiStateVersion = Number(result.version || uiStateVersion);
        applyUiStateSnapshot(result.snapshot);
      }
      uiStateRetryMs = 1000;
      const sideState = $('side-state');
      if (sideState) sideState.removeAttribute('data-stream-error');
    } catch (error) {
      retry = true;
      console.error('状态订阅中断', error);
      const sideState = $('side-state');
      if (sideState) {
        sideState.dataset.streamError = 'true';
        sideState.title = `状态通道正在恢复：${friendlyError(error)}`;
      }
    }
    if (retry && !uiStateSubscriptionStopped) {
      await new Promise(resolve => setTimeout(resolve, uiStateRetryMs));
      uiStateRetryMs = Math.min(15000, uiStateRetryMs * 2);
    }
  }
}

window.addEventListener('beforeunload', () => {
  uiStateSubscriptionStopped = true;
});

async function refreshUiStateOnce() {
  if (!CFG) return;
  try {
    const [state, vpnStatus, cap, sms] = await Promise.all([
      api().get_state(), api().vpn_status(), api().get_manual_captcha(), api().get_sms_ui()
    ]);
    updateOverview(state, vpnStatus);
    await syncBlockingModals(cap, sms);
    if (curPage === 'logs' && window.RoutingActivityWorkspace?.runtimeLogsVisible?.()) await updateLogs();
    if (curPage === 'browser') await refreshBrState();
  } catch (e) {
    console.error('状态快照读取失败', e);
    $('side-state').title = `状态更新失败：${friendlyError(e)}`;
  }
}

function updateOverview(state, vpnStatus) {
  VPN_STATUS = vpnStatus || { connections: [], connected_names: [], route_conflicts: [] };
  const connections = Array.isArray(VPN_STATUS.connections) ? VPN_STATUS.connections : [];
  const connectedCount = connections.length;
  const routeSignature = JSON.stringify(connections.map(item => [
    item.name, item.ip_address, item.default_route_ipv4, item.default_route_ipv6
  ]));
  if (lastIpRouteSignature !== null && routeSignature !== lastIpRouteSignature) {
    setTimeout(() => void refreshIpInfo(true), 350);
  }
  lastIpRouteSignature = routeSignature;
  const snapshotProfiles = Array.isArray(VPN_STATUS.profiles)
    ? VPN_STATUS.profiles : [];
  const snapshotReady = Number(VPN_STATUS.checked_at) > 0 || snapshotProfiles.length > 0;
  if (snapshotReady) {
    setVpnProfiles(snapshotProfiles, { render: curPage === 'vpns' });
  }
  const profiles = snapshotReady
    ? VPN_LIST
    : vpnListReady ? VPN_LIST : configuredVpnPreviewProfiles();
  const previewPending = profiles.some(profile => profile.pending_system_check === true);
  const displayCapacity = Math.max(5, profiles.length);
  const defaultProfile = profiles.find(row => row.name === CFG.vpn_name);
  const defaultConnection = connections.find(row => row.name === CFG.vpn_name);
  const defaultPending = defaultProfile?.pending_system_check === true;
  const connection = $('ov-conn');
  connection.textContent = previewPending
    ? `核对中 / ${displayCapacity} 个`
    : `${connectedCount} / ${displayCapacity} 个`;
  connection.className = connectedCount ? 'on' : 'off';
  $('ov-active-count').textContent = previewPending
    ? `${profiles.length} 个已保存配置 · 正在核对`
    : `${connectedCount} / ${displayCapacity} 个已连接`;
  $('ov-name').textContent = CFG.vpn_name || '尚未设置默认 VPN';
  const defaultStatus = $('ov-default-status');
  defaultStatus.textContent = defaultConnection
    ? '已连接' : defaultPending ? '正在核对' : '未连接';
  defaultStatus.className = `connection-state ${defaultConnection ? 'on' : 'off'}`;
  $('ov-last-connection').textContent = defaultConnection
    ? `已连接 ${formatDuration(defaultConnection.connected_seconds)}` : '—';
  $('ov-default-type').textContent = defaultPending
    ? '读取中' : defaultProfile?.type || '—';
  $('ov-default-server').textContent = defaultPending
    ? '读取中' : defaultProfile?.server || '—';
  const issue = state.connection_error;
  const connectionAction = state.connection_action || {};
  const autoConnecting = connectionAction.active &&
    ['queued', 'connecting', 'verifying'].includes(connectionAction.status);
  const relevantIssue = issue && (!issue.name || issue.name === CFG.vpn_name);
  const connectionIssue = $('ov-conn-detail');
  connectionIssue.textContent = autoConnecting
    ? (connectionAction.message || '正在执行自动连接任务')
    : relevantIssue
      ? issue.message : (!CFG.vpn_name ? '先配置默认 VPN，再发起连接' : '');
  connectionIssue.classList.toggle('active', autoConnecting);
  connectionIssue.classList.toggle('hidden', !connectionIssue.textContent);
  renderVpnConnections(profiles, connections, connectionAction);
  const conflicts = Array.isArray(VPN_STATUS.route_conflicts)
    ? VPN_STATUS.route_conflicts : [];
  const warning = $('ov-route-warning');
  warning.textContent = conflicts.map(row => row.message).filter(Boolean).join('；');
  warning.classList.toggle('hidden', !warning.textContent);
  const authorization = state.authorization || {};
  const expiryText = authorization.status === 'expired'
    ? '已到期' : (authorization.expires_at_text || '待同步');
  $('ov-hours').textContent = expiryText;
  $('ov-renew-hours').textContent = expiryText;
  const renewalHint = authorization.next_renew_at_text &&
    authorization.next_renew_at_text !== '待同步'
    ? `下次续期：${authorization.next_renew_at_text}`
    : '完成一次软件授权后同步真实到期时间';
  $('ov-hours').title = renewalHint;
  $('ov-renew-hours').title = renewalHint;
  $('ov-renew').textContent = state.last_renew || '暂无记录';
  const sideState = $('side-state');
  sideState.textContent = state.running ? '后台服务运行中' : '后台服务未启动';
  sideState.className = `pill ${state.running ? 'on' : 'off'}`;
  $('ov-service').textContent = issue && issue.suggest_repair
    ? '建议修复 VPN 服务'
    : state.running ? '运行正常' : '未启动';
  $('ov-network-state').textContent = autoConnecting
    ? (connectionAction.source === 'authorization'
      ? '授权后重连中'
      : connectionAction.source === 'manual'
        ? '连接确认中' : '自动连接中')
    : issue && issue.suggest_repair
      ? '需要检查'
      : state.running ? '运行正常' : '服务未启动';
  sideState.title = '后台服务常驻：监视连接状态、执行手动/自动续期、托管内嵌浏览器。' +
    '“自动连接”“自动续期”都关闭时，它只监视不动作，不会主动拨号。';
  updateConnectionControls(VPN_STATUS, connectionAction);
  updateRenewalState(state);
  updateRepairState(state);
}

function renderVpnConnections(profiles, connections, connectionAction = {}) {
  const root = $('ov-active-connections');
  root.replaceChildren();
  const configured = Array.isArray(profiles) ? profiles : [];
  const connectedByName = new Map(connections.map(item => [item.name, item]));
  const rows = configured.slice().sort((left, right) => {
    if (left.name === CFG.vpn_name) return -1;
    if (right.name === CFG.vpn_name) return 1;
    return Number(connectedByName.has(right.name)) - Number(connectedByName.has(left.name));
  });
  connections.forEach(item => {
    if (!rows.some(row => row.name === item.name)) rows.push(item);
  });
  rows.forEach(profile => {
    const item = connectedByName.get(profile.name);
    const connected = !!item;
    const pendingSystemCheck = profile.pending_system_check === true;
    const autoBusy = !!connectionAction.active &&
      connectionAction.name === profile.name;
    const address = connected && item.ip_address
      ? `${item.ip_address}${item.prefix_length ? `/${item.prefix_length}` : ''}`
      : '';
    const meta = autoBusy
      ? (connectionAction.message || '正在自动连接')
      : pendingSystemCheck
        ? '已从 config.json 读取，正在核对 Windows VPN 状态'
      : connected
        ? `${address || '地址由 Windows 管理'} · 已连接 ${formatDuration(item.connected_seconds)}`
        : '未连接';
    const validationStatus = (CFG.credential_status || {})[profile.name] || '';
    root.appendChild(createConnectionRow({
      profile,
      name: profile.name || '未命名 VPN',
      connected,
      defaultRouteLabel: activeDefaultRouteLabel(item),
      validationStatus,
      meta,
      actionText: autoBusy
        ? '正在自动连接' : pendingSystemCheck ? '核对中' : (connected ? '断开' : '连接'),
      busy: autoBusy || pendingSystemCheck,
      action: pendingSystemCheck ? null : event => connected
        ? disconnectVpn(profile.name, event.currentTarget)
        : connectVpn(profile.name, event.currentTarget),
    }));
  });
  const displayCapacity = Math.max(5, rows.length);
  for (let index = rows.length; index < displayCapacity; index += 1) {
    root.appendChild(createEmptyConnectionSlot());
  }
}

function activeDefaultRouteLabel(item) {
  if (!item) return '';
  const families = [];
  if (item.default_route_ipv4) families.push('IPv4');
  if (item.default_route_ipv6) families.push('IPv6');
  return families.length ? `${families.join('/')} 默认路由` : '';
}

function createConnectionRow({ profile, name, connected, defaultRouteLabel = '', validationStatus = '', meta, actionText, action, busy = false }) {
  const row = document.createElement('div');
  const isDefault = name === CFG.vpn_name;
  row.className = `active-connection${connected ? '' : ' disconnected'}${isDefault ? ' is-default' : ''}`;
  const stateDot = document.createElement('span');
  stateDot.className = `connection-status-dot${connected ? ' on' : ''}`;
  stateDot.setAttribute('aria-hidden', 'true');
  const nameBox = document.createElement('div');
  nameBox.className = 'active-connection-name';
  const title = document.createElement('strong');
  title.textContent = name;
  nameBox.appendChild(title);
  if (isDefault) {
    const tag = document.createElement('span');
    tag.className = 'default-tag';
    tag.textContent = '默认';
    nameBox.appendChild(tag);
  }
  if (defaultRouteLabel) {
    const tag = document.createElement('span');
    tag.className = 'route-tag';
    tag.textContent = defaultRouteLabel;
    nameBox.appendChild(tag);
  }
  if (validationStatus === 'windows_sync_failed') {
    const tag = document.createElement('span');
    tag.className = 'credential-tag failed';
    tag.textContent = 'Windows 同步失败';
    nameBox.appendChild(tag);
  }
  const metaBox = document.createElement('div');
  metaBox.className = 'active-connection-meta';
  metaBox.textContent = meta;
  const actions = document.createElement('div');
  actions.className = 'connection-row-actions';
  const actionButton = document.createElement('button');
  actionButton.type = 'button';
  actionButton.className = `connection-icon-action${connected ? ' connected' : ' primary'}`;
  actionButton.innerHTML = connected
    ? '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 3v8"/>' +
      '<path d="M17.7 6.3a8 8 0 1 1-11.4 0"/></svg>'
    : '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m9 7 8 5-8 5Z"/></svg>';
  actionButton.title = `${actionText} ${name}`;
  actionButton.setAttribute('aria-label', `${actionText} ${name}`);
  actionButton.onclick = action;
  actionButton.dataset.vpnAction = 'true';
  if (busy) setConnectionButtonBusy(actionButton, actionText);
  const moreButton = document.createElement('button');
  moreButton.type = 'button';
  moreButton.className = 'connection-icon-action more';
  moreButton.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24">' +
    '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/>' +
    '<circle cx="19" cy="12" r="1"/></svg>';
  moreButton.title = `编辑 ${name}`;
  moreButton.setAttribute('aria-label', `编辑 ${name}`);
  moreButton.onclick = () => openModal(profile);
  actions.append(actionButton, moreButton);
  row.append(stateDot, nameBox, metaBox, actions);
  return row;
}

function createEmptyConnectionSlot() {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'connection-slot-empty';
  button.innerHTML = '<span class="slot-plus" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 7v10M7 12h10"/></svg></span>' +
    '<span>添加 VPN 配置</span><span class="slot-dash">—</span>' +
    '<svg class="slot-arrow" aria-hidden="true" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>';
  button.onclick = () => openModal(null);
  return button;
}

function formatDuration(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  if (total < 60) return '不足 1 分钟';
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return hours ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`;
}

function updateConnectionControls(status, connectionAction = {}) {
  if (connectionBusy) return;
  const hasTarget = !!CFG.vpn_name;
  const names = Array.isArray(status.connected_names)
    ? status.connected_names : [];
  const defaultConnected = hasTarget && names.includes(CFG.vpn_name);
  const connectedCount = Number(status.connected_count) || names.length;
  const connect = $('btn-connect');
  const autoBusy = !!connectionAction.active &&
    connectionAction.name === CFG.vpn_name;
  if (autoBusy) {
    connect.classList.remove('hidden');
    setConnectionButtonBusy(connect,
      connectionAction.source === 'authorization' ? '授权后重连中…' : '自动连接中…');
  } else {
    connect.textContent = hasTarget ? '连接默认 VPN' : '配置 VPN';
    connect.disabled = defaultConnected;
    connect.classList.toggle('hidden', defaultConnected);
  }
  const disconnect = $('btn-disconnect');
  disconnect.textContent = '断开默认 VPN';
  disconnect.disabled = autoBusy || !defaultConnected;
  disconnect.classList.toggle('hidden', autoBusy || !defaultConnected);
  const disconnectAllButton = $('btn-disconnect-all');
  disconnectAllButton.classList.toggle('hidden', connectedCount < 2);
  disconnectAllButton.disabled = connectedCount < 2;
}

async function syncBlockingModals(cap, sms) {
  if (cap && cap.id !== capId) await openCap(cap);
  if (!cap && !$('capmodal').classList.contains('hidden')) {
    capPoints = [];
    await hideBlockingBrowserModal($('capmodal'));
  }
  if (sms && sms.id !== smsUiId) {
    smsUiId = sms.id;
    syncSmsModalCopy();
    $('sms-code').value = '';
    await showBlockingBrowserModal($('smsmodal'));
    $('sms-code').focus();
  } else if (!sms && !$('smsmodal').classList.contains('hidden')) {
    await hideBlockingBrowserModal($('smsmodal'));
  }
}

async function updateLogs() {
  const logbox = $('logbox');
  const stick = logbox.scrollHeight - logbox.scrollTop - logbox.clientHeight < 24;
  const logs = await api().get_logs();
  logbox.textContent = logs.join('\n');
  $('log-count').textContent = `${logs.length} 条记录`;
  $('btn-copy-logs').disabled = logs.length === 0;
  $('log-empty').classList.toggle('hidden', logs.length > 0);
  logbox.classList.toggle('hidden', logs.length === 0);
  if (logs.length && stick) logbox.scrollTop = logbox.scrollHeight;
}

async function copyLogs() {
  const text = $('logbox').textContent;
  if (!text) {
    toast({ ok: false, msg: '当前没有可复制的日志' });
    return;
  }
  try {
    await writeClipboardText(text);
    toast({ ok: true, msg: '运行日志已复制' });
  } catch (e) {
    toast({ ok: false, msg: `复制失败：${friendlyError(e)}` });
  }
}

async function writeClipboardText(text) {
  let copied = false;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (e) {
      // file:// WebView2 可能拒绝 Clipboard API，继续使用兼容方案。
    }
  }
  if (!copied) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    copied = document.execCommand('copy');
    textarea.remove();
  }
  if (!copied) throw new Error('系统未允许写入剪贴板');
}

async function copyIpAddress(button) {
  const target = $(button.dataset.ipTarget);
  const address = target?.textContent?.trim();
  if (!address || address === '—') return;
  try {
    await writeClipboardText(address);
    button.classList.add('copied');
    setTimeout(() => button.classList.remove('copied'), 900);
    toast({ ok: true, msg: `${button.dataset.ipLabel || 'IP'} 已复制` });
  } catch (e) {
    toast({ ok: false, msg: `复制失败：${friendlyError(e)}` });
  }
}

function renderIpEntry(prefix, entry) {
  const available = !!entry?.ok;
  const stale = available && !!entry?.stale;
  const address = available ? entry.ip : '—';
  const baseLocation = available ? (entry.location || '位置未知') : '查询失败';
  const location = stale ? `旧数据 · ${baseLocation}` : baseLocation;
  const provider = available
    ? (entry.provider || '运营商未知') : (entry?.error || '请稍后重试');
  $(`ip-${prefix}-address`).textContent = address;
  $(`ip-${prefix}-address`).title = available ? address : '';
  $(`ip-${prefix}-location`).textContent = location;
  $(`ip-${prefix}-location`).title = location;
  $(`ip-${prefix}-provider`).textContent = provider;
  $(`ip-${prefix}-provider`).title = stale && entry?.error
    ? `${provider}；刷新失败：${entry.error}` : provider;
  const copyButton = document.querySelector(`[data-ip-target="ip-${prefix}-address"]`);
  if (copyButton) copyButton.disabled = !available;
}

function renderIpInfo(info = {}) {
  const local = info.local || {};
  $('ip-local-address').textContent = local.ok ? local.ip : '—';
  $('ip-local-detail').textContent = local.ok
    ? (local.detail || '本机网卡') : (local.error || '读取失败');
  const localCopy = document.querySelector('[data-ip-target="ip-local-address"]');
  if (localCopy) localCopy.disabled = !local.ok;
  renderIpEntry('domestic', info.domestic);
  renderIpEntry('overseas', info.overseas);

  const available = ['local', 'domestic', 'overseas']
    .filter(name => info[name]?.ok).length;
  const staleCount = ['local', 'domestic', 'overseas']
    .filter(name => info[name]?.ok && info[name]?.stale).length;
  const status = $('ip-status');
  const freshness = status.closest('.ip-freshness');
  const refresh = $('btn-refresh-ip');
  freshness.className = 'ip-freshness';
  if (info.loading) {
    status.textContent = '更新中';
    freshness.classList.add('loading');
  } else if (staleCount > 0) {
    status.textContent = '部分为旧数据';
    freshness.classList.add('partial');
  } else if (available === 3) {
    status.textContent = info.updated_at_text
      ? `${info.updated_at_text} 已更新` : '已更新';
  } else if (available > 0) {
    status.textContent = '部分可用';
    freshness.classList.add('partial');
  } else {
    status.textContent = '暂不可用';
    freshness.classList.add('failed');
  }
  refresh.disabled = !!info.loading;
  refresh.classList.toggle('is-loading', !!info.loading);
}

async function refreshIpInfo(forceRefresh = false) {
  try {
    const info = await api().get_ip_info(!!forceRefresh);
    renderIpInfo(info);
  } catch (e) {
    console.error('网络出口信息刷新失败', e);
    renderIpInfo({});
  }
}

async function refreshBrState(browserSnapshot = null) {
  try {
    const browser = browserSnapshot || await api().get_browser();
    const state = $('br-state');
    state.textContent = browser.up ? '运行中' : '未启动';
    state.className = `browser-state ${browser.up ? 'on' : 'off'}`;
    const dot = document.createElement('span');
    state.prepend(dot);
    $('br-url').textContent = browser.url || 'https://remote.chaoxing.com/vpn';
    $('btn-br-open').classList.toggle('hidden', browser.up);
    $('btn-br-reload').classList.toggle('hidden', !browser.up);
    $('browser-empty').classList.toggle('hidden', browser.up);
  } catch (e) {
    console.error('浏览器状态读取失败', e);
  }
}

function updateRenewalState(state) {
  const progress = state.browser_progress || {};
  const previousStatus = lastRenewalStatus;
  lastRenewalStatus = progress.status || 'idle';
  renewalActive = !!(state.renewing || state.renew_queued);
  renewalCancelPending = !!state.renew_cancel_pending;
  const badge = $('ov-renew-state');
  let text = '等待处理';
  let tone = 'neutral';
  if (renewalCancelPending) {
    text = '正在中断';
    tone = 'cancelled';
  } else if (renewalActive) {
    text = '授权处理中';
    tone = 'running';
  } else if (progress.status === 'completed') {
    text = '最近处理完成';
    tone = 'on';
  } else if (progress.status === 'cancelled') {
    text = '授权处理已中断';
    tone = 'cancelled';
  } else if (progress.status === 'failed') {
    text = '授权处理未完成';
    tone = 'failed';
  }
  badge.textContent = text;
  badge.className = `status-badge ${tone}`;
  $('renew-card').classList.toggle('is-running', renewalActive);
  $('ov-renew-title').textContent = progress.title || '等待自动化任务';
  $('ov-renew-detail').textContent = progress.detail || '根据当前状态自动执行首次授权或续期';
  $('ov-renew-step').textContent = `${progress.step || 0} / ${progress.total || 5}`;
  const currentStep = Math.max(0, Number(progress.step) || 0);
  const completed = progress.status === 'completed';
  $('ov-renew-progress').value = Math.max(0, Math.min(4, currentStep - 1));
  $('ov-renew-stages').querySelectorAll('li').forEach((item, index) => {
    const stageNumber = index + 1;
    const done = completed || stageNumber < currentStep;
    const current = renewalActive && stageNumber === currentStep;
    item.classList.toggle('done', done);
    item.classList.toggle('current', current);
    const stageStatus = item.querySelector('small');
    if (stageStatus) stageStatus.textContent = done ? '通过' : current ? '处理中' : '待执行';
  });
  renderRenewalActionButtons();
  if (previousStatus === 'running' && lastRenewalStatus === 'completed') {
    toast({ ok: true, msg: 'VPN 授权处理已完成，软件内到期状态已同步' });
  }
}

function updateRepairState(state) {
  const active = !!state.repairing;
  const result = state.repair_result;
  const button = $('btn-repair');
  button.disabled = active;
  if (active) {
    $('repair-title').textContent = '正在修复 VPN 服务';
    $('repair-detail').textContent = '请响应 UAC，完成前请勿重复操作';
  } else if (result) {
    $('repair-title').textContent = result.ok ? 'VPN 服务修复完成' : 'VPN 服务修复未完成';
    $('repair-detail').textContent = `${result.finished_at || ''} ${result.msg || ''}`.trim();
  } else {
    $('repair-title').textContent = '修复 VPN 服务';
    $('repair-detail').textContent = '连接长时间卡住时使用';
  }
  if (repairStateReady && repairWasActive && !active && result) {
    toast({ ok: !!result.ok, msg: result.msg || (result.ok ? 'VPN 服务修复完成' : 'VPN 服务修复未完成') });
  }
  repairWasActive = active;
  repairStateReady = true;
}

async function syncBrowserVisibility() {
  const browserPage = curPage === 'browser';
  $('main-content').classList.toggle('browser-mode', browserPage);
  const bridge = window.pywebview?.api;
  if (!bridge?.browser_set_visible) return;
  await bridge.browser_set_visible(browserPage && !hasBlockingBrowserModal());
}

function hasBlockingBrowserModal() {
  return ['capmodal', 'smsmodal'].some(id => {
    const modal = $(id);
    return modal && !modal.classList.contains('hidden');
  });
}

async function showBlockingBrowserModal(modal) {
  if (!modal) return;
  try {
    await api().browser_set_visible(false);
  } finally {
    modal.classList.remove('hidden');
  }
}

async function hideBlockingBrowserModal(modal) {
  if (!modal) return;
  modal.classList.add('hidden');
  await syncBrowserVisibility();
}

function confirmAction({ title, message, confirmText = '确认', tone = 'danger', kicker = '' }) {
  if (confirmResolve) finishConfirm(false);
  confirmReturnFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement : null;
  $('confirm-title').textContent = title;
  $('confirm-message').textContent = message;
  const modal = $('confirmmodal');
  const notice = tone === 'notice';
  $('confirm-kicker').textContent = kicker || (notice ? '网络接管确认' : '危险操作');
  $('confirm-kicker').classList.toggle('danger-kicker', !notice);
  $('confirm-icon').textContent = notice ? 'i' : '!';
  $('confirm-icon').classList.toggle('notice', notice);
  const confirmButton = $('btn-confirm-ok');
  confirmButton.textContent = confirmText;
  confirmButton.className = notice ? 'btn primary' : 'btn danger danger-solid';
  modal.setAttribute('role', notice ? 'dialog' : 'alertdialog');
  modal.classList.remove('hidden');
  setTimeout(() => $('btn-confirm-cancel').focus(), 0);
  return new Promise(resolve => { confirmResolve = resolve; });
}

function finishConfirm(result) {
  $('confirmmodal').classList.add('hidden');
  const resolve = confirmResolve;
  confirmResolve = null;
  const returnFocus = confirmReturnFocus;
  confirmReturnFocus = null;
  if (returnFocus?.isConnected) setTimeout(() => returnFocus.focus(), 0);
  if (resolve) resolve(result);
}

async function runBusy(button, busyText, task) {
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = busyText;
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function toggleSecret(input, button) {
  const revealed = input.type === 'password';
  input.type = revealed ? 'text' : 'password';
  setSecretToggleState(button, revealed);
}

function concealSecret(input, button) {
  input.type = 'password';
  setSecretToggleState(button, false);
}

function setSecretToggleState(button, revealed) {
  const openEye = '<svg aria-hidden="true" viewBox="0 0 24 24">' +
    '<path d="M2.8 12s3.2-5 9.2-5 9.2 5 9.2 5-3.2 5-9.2 5-9.2-5-9.2-5Z"/>' +
    '<circle cx="12" cy="12" r="2.5"/></svg>';
  const closedEye = '<svg aria-hidden="true" viewBox="0 0 24 24">' +
    '<path d="M3.5 15.5s3-4 8.5-4 8.5 4 8.5 4"/>' +
    '<path d="m5.5 17.5-1.3 1.6M9.8 16.4l-.3 2.2M14.2 16.4l.3 2.2M18.5 17.5l1.3 1.6"/></svg>';
  const subject = button.dataset.secretLabel || '密码';
  const separator = /^[A-Za-z]/.test(subject) ? ' ' : '';
  const label = `${revealed ? '隐藏' : '显示'}${separator}${subject}`;
  button.innerHTML = revealed ? closedEye : openEye;
  button.classList.toggle('is-revealed', revealed);
  button.setAttribute('aria-label', label);
  button.setAttribute('aria-pressed', String(revealed));
  button.title = label;
}

function normalizeResult(result, successMessage) {
  if (result === false) return { ok: false, msg: '操作未完成' };
  if (result && typeof result === 'object') {
    return { ...result, ok: result.ok !== false, msg: result.msg || successMessage };
  }
  return { ok: true, msg: successMessage };
}

function friendlyError(error) {
  const value = error && error.message ? error.message : error;
  return String(value || '未知错误').split('\n')[0];
}

function setFeedback(element, message, tone = '') {
  element.textContent = message || '';
  element.className = `form-feedback${tone ? ` ${tone}` : ''}`;
}

function toast(result) {
  const target = $('toast');
  if (!target) return;
  const normalized = normalizeResult(result, '操作成功');
  const requestedTone = normalized.tone || normalized.level;
  const tone = ['success', 'error', 'warning', 'info'].includes(requestedTone)
    ? requestedTone : (normalized.ok ? 'success' : 'error');
  const meta = {
    success: { title: '操作成功', mark: '✓', duration: 3200 },
    error: { title: '操作失败', mark: '×', duration: 5200 },
    warning: { title: '请注意', mark: '!', duration: 4600 },
    info: { title: '提示', mark: 'i', duration: 4000 },
  }[tone];
  const message = String(normalized.msg || meta.title).split('\n')[0];

  target.replaceChildren();
  const icon = document.createElement('span');
  icon.className = 'toast-icon';
  icon.setAttribute('aria-hidden', 'true');
  if (tone === 'success') {
    icon.classList.add('toast-icon-success');
    icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="m7.5 12.5 3 3 6.5-7"/></svg>';
  } else {
    icon.textContent = meta.mark;
  }
  const body = document.createElement('span');
  body.className = 'toast-body';
  const title = document.createElement('strong');
  title.className = 'toast-title';
  title.textContent = meta.title;
  const detail = document.createElement('span');
  detail.className = 'toast-message';
  detail.textContent = message;
  const timer = document.createElement('span');
  timer.className = 'toast-timer';
  body.append(title, detail);
  target.append(icon, body, timer);
  target.className = `show toast-${tone}`;
  target.setAttribute('role', tone === 'error' || tone === 'warning' ? 'alert' : 'status');
  target.style.setProperty('--toast-duration', `${meta.duration}ms`);
  clearTimeout(target._h);
  target._h = setTimeout(() => { target.className = ''; }, meta.duration);
}

function esc(value) {
  const div = document.createElement('div');
  div.textContent = value == null ? '' : String(value);
  return div.innerHTML;
}

function injectModalClose(root) {
  root = root || document;
  const cards = (root.nodeType === 1 && root.classList.contains('modal-card'))
    ? [root] : root.querySelectorAll('.modal-card');
  cards.forEach(card => {
    if (card.querySelector('.modal-x')) return;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'modal-x';
    close.title = '关闭';
    close.setAttribute('aria-label', '关闭');
    close.textContent = '×';
    card.prepend(close);
  });
}

async function closeModal(modal) {
  if (!modal) return;
  if (modal.id === 'confirmmodal') {
    finishConfirm(false);
    return;
  }
  if (modal.id === 'connectmodal') {
    finishConnectChoice(null);
    return;
  }
  if (modal.id === 'credmodal') credentialConnectRetry = null;
  if (modal.id === 'smsmodal') await api().dismiss_sms_ui();
  if (modal.id === 'capmodal') {
    if (api().dismiss_manual_captcha) await api().dismiss_manual_captcha();
    capPoints = [];
    $('cap-marks').innerHTML = '';
  }
  if (modal.id === 'smsmodal' || modal.id === 'capmodal') {
    await hideBlockingBrowserModal(modal);
  } else {
    modal.classList.add('hidden');
  }
}

document.addEventListener('click', async (e) => {
  const close = e.target.closest && e.target.closest('.modal-x');
  if (close) await closeModal(close.closest('.modal'));
});

new MutationObserver(mutations => {
  mutations.forEach(mut => mut.addedNodes.forEach(node => {
    if (node.nodeType === 1) injectModalClose(node);
  }));
}).observe(document.body, { childList: true, subtree: true });
injectModalClose();
