// JumpServer 登录与 MFA 适配：用户名/密码和 OTP 分步提交，认证流程中不得提前判成功。
globalThis.CXWebQuickPlatforms = globalThis.CXWebQuickPlatforms || {};
globalThis.CXWebQuickPlatforms.jumpserver = {
  id: 'jumpserver',
  origin: 'http://192.168.1.51',

  match(location) {
    return location.origin === this.origin;
  },

  isLoginRoute(location) {
    return /^\/core\/auth\/login\/?$/i.test(location.pathname);
  },

  isFactorRoute(location) {
    return /^\/core\/auth\/(?:login\/)?(?:mfa|otp|captcha)(?:\/|$)/i.test(location.pathname);
  },

  form() {
    const username = document.querySelector('#id_username');
    const password = document.querySelector('#password');
    const submit = document.querySelector('#login-form button[type="submit"]');
    return username && password && submit ? { username, password, submit } : null;
  },

  token() {
    if (this.isLoginRoute(location) || this.isFactorRoute(location) || this.extraFactor()) return '';
    return document.querySelector('#login-form') ? '' : 'authenticated';
  },

  visible(element) {
    return Boolean(element) && element.getClientRects().length > 0;
  },

  extraFactor() {
    const common = 'input[autocomplete="one-time-code"],input[name="otp"],input[name="mfa_code"],'
      + 'input[name="mfa"],#id_otp,#id_mfa_code,#id_mfa';
    const selector = common + (this.isFactorRoute(location)
      ? ',input[name="code"],#id_code,input[placeholder*="MFA 验证码" i],'
        + 'input[placeholder*="动态口令" i],input[placeholder*="动态验证码" i]' : '');
    return Array.from(document.querySelectorAll(selector))
      .find(element => this.visible(element)) || null;
  },

  factorSubmit() {
    return document.querySelector('#login-form button[type="submit"],#mfa-form button[type="submit"],'
      + '#mfa-form input[type="submit"],form button[type="submit"],form input[type="submit"]');
  },

  blocked() {
    return '';
  },

  captcha() {
    return Array.from(document.querySelectorAll(
      '.captcha-field input,input[name="captcha"],.captcha img',
    )).some(element => this.visible(element));
  },

  error(seen) {
    const message = Array.from(document.querySelectorAll(
      '.has-error .help-block,.error-info,.alert-danger',
    )).find(element => this.visible(element) && !seen.has(element) && element.textContent.trim());
    return message ? message.textContent.trim().slice(0, 120) : '';
  },
};
