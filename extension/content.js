// CX Toolkit 网页快开：只在主程序有待领取任务时填写登录信息；每次页面加载最多提交一次登录。
(() => {
  'use strict';
  const platforms = globalThis.CXWebQuickPlatforms || {};
  const platform = Object.values(platforms).find(candidate => {
    try { return candidate.match(location); } catch (error) { return false; }
  });
  if (!platform) return;
  const RUN_KEY = `cx-web-quick-run-v1:${platform.id}`;

  const SETTLE_MS = 10000;
  const STABLE_POLLS = 6;
  const STEP_MS = 250;
  const RESULT_MS = 45000;
  const CAPTCHA_MS = 75000;

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const currentRunId = () => {
    try { return sessionStorage.getItem(RUN_KEY) || ''; } catch (error) { return ''; }
  };
  function saveRun(runId) {
    try { sessionStorage.setItem(RUN_KEY, runId); } catch (error) { /* 无痕/受限页面退化为单次流程 */ }
  }
  function clearRun() {
    try { sessionStorage.removeItem(RUN_KEY); } catch (error) { /* 无法清理不影响任务终态 */ }
  }

  function send(message) {
    return new Promise((resolve) => chrome.runtime.sendMessage(
      { platform: platform.id, ...message },
      (response) => resolve(chrome.runtime.lastError ? { ok: false, error: '扩展后台不可用' } : response),
    ));
  }

  function setValue(input, value) {
    // 直接赋值不会触发 Vue 的 v-model，须经原生 setter 写入并派发 input 事件。
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  async function alreadyLoggedIn() {
    await send({ type: 'claim', login: false });
  }

  async function finish(runId, state, detail) {
    if (runId) await send({ type: 'report', run_id: runId, state, detail });
  }

  async function attempt(claim, resume = false) {
    const credentials = claim.credentials || {};
    if (!resume) {
      const reason = platform.blocked();
      if (reason) {
        await finish(claim.run_id, 'failed', reason);
        return;
      }
    }
    const form = platform.form();
    const seen = resume ? new Set() : new Set(document.querySelectorAll('.el-message--error'));
    if (!resume) {
      if (!form) {
        await finish(claim.run_id, 'failed', '未在页面中找到登录表单，请在 Edge 中手动登录');
        return;
      }
      setValue(form.username, credentials.username || '');
      setValue(form.password, credentials.password || '');
      await pause(200);
      form.submit.click();
    }

    let deadline = Date.now() + RESULT_MS;
    let factorDone = false;
    let captchaAt = 0;
    while (Date.now() < deadline) {
      await pause(300);
      if (platform.token()) {
        await finish(claim.run_id, 'success', '');
        return;
      }
      const message = platform.error(seen);
      if (message) {
        await finish(claim.run_id, 'failed', message);
        return;
      }
      const blocked = platform.blocked();
      if (blocked) {
        await finish(claim.run_id, 'failed', blocked);
        return;
      }
      const factor = platform.extraFactor();
      if (factor && !factorDone) {
        if (!credentials.otp) {
          await finish(claim.run_id, 'failed', '账号需要动态口令，但条目未设置二次验证账号');
          return;
        }
        setValue(factor, credentials.otp);
        factorDone = true;
        await pause(200);
        const submit = platform.factorSubmit ? platform.factorSubmit() : form && form.submit;
        if (!submit) {
          await finish(claim.run_id, 'failed', '未找到动态口令提交按钮，请在 Edge 中手动提交');
          return;
        }
        submit.click();
        continue;
      }
      if (platform.captcha()) {
        // 滑块只提示人工处理，不自动识别；验证通过后页面会自行继续登录。
        captchaAt = captchaAt || Date.now();
        deadline = Math.max(deadline, captchaAt + CAPTCHA_MS);
      }
    }
    await finish(claim.run_id, 'failed', captchaAt ? '需要手动完成滑块验证，验证后未检测到登录成功'
      : '未检测到登录结果，请在 Edge 中查看');
  }

  async function resume(runId) {
    const claim = await send({ type: 'claim', login: !platform.token(), run_id: runId });
    if (!claim.ok) {
      clearRun();
      return false;
    }
    if (!claim.skipped) await attempt(claim, true);
    clearRun();
    return true;
  }

  async function run() {
    const pendingRun = currentRunId();
    if (pendingRun && await resume(pendingRun)) return;
    const deadline = Date.now() + SETTLE_MS;
    let stable = 0;
    let since = Date.now();
    let lastPath = location.pathname;
    while (Date.now() < deadline) {
      const loginRoute = platform.isLoginRoute(location);
      const factorRoute = platform.isFactorRoute?.(location) || platform.extraFactor();
      if (loginRoute || factorRoute) {
        const form = platform.form();
        stable = (form || factorRoute) && !platform.token() ? stable + 1 : 0;
        if (stable >= STABLE_POLLS) {
          const claim = await send({ type: 'claim', login: true });
          // 没有待领取任务时保持静默：用户自己打开站点不受影响。
          if (claim.ok && !claim.skipped) {
            saveRun(claim.run_id);
            // 独立 OTP 页没有账号密码表单，只续接动态口令步骤。
            await attempt(claim, Boolean(factorRoute && !form));
            clearRun();
          }
          return;
        }
      } else {
        // 只有非登录/非 MFA 路由稳定且加载完成后，才允许按已登录处理。
        if (location.pathname !== lastPath) {
          lastPath = location.pathname;
          since = Date.now();
        } else if (document.readyState === 'complete' && Date.now() - since >= 2000) {
          await alreadyLoggedIn();
          return;
        }
        stable = 0;
      }
      await pause(STEP_MS);
    }
    const claim = await send({ type: 'claim', login: true });
    if (claim.ok && !claim.skipped) await finish(claim.run_id, 'failed', '未在页面中找到登录表单，请在 Edge 中手动登录');
  }

  run().catch(async () => {
    const runId = currentRunId();
    if (!runId) return;
    try { await finish(runId, 'failed', '页面适配异常，请在 Edge 中查看'); }
    finally { clearRun(); }
  });
})();
