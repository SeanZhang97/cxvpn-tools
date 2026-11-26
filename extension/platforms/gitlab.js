// GitLab CE 登录页适配：整页 POST；data-page 区分登录、OTP 与已登录页面。
globalThis.CXWebQuickPlatforms = globalThis.CXWebQuickPlatforms || {};
globalThis.CXWebQuickPlatforms.gitlab = {
  id: 'gitlab',
  origin: 'https://10.0.26.27',

  match(location) {
    return location.origin === this.origin;
  },

  isLoginRoute(location) {
    return /^\/users\/sign_in\/?$/i.test(location.pathname);
  },

  form() {
    const username = document.querySelector('#user_login');
    const password = document.querySelector('#user_password');
    const submit = document.querySelector('#new_user button[type="submit"]');
    return username && password && submit ? { username, password, submit } : null;
  },

  token() {
    const page = document.body && document.body.dataset ? document.body.dataset.page || '' : '';
    if (!page || /^(sessions|registrations|passwords|unlock):/.test(page)) return '';
    return 'authenticated';
  },

  visible(element) {
    return Boolean(element) && element.getClientRects().length > 0;
  },

  extraFactor() {
    return Array.from(document.querySelectorAll(
      '#user_otp_attempt,#user_backup_code,input[name="user[otp_attempt]"],input[name="user[backup_code]"]',
    )).find(element => this.visible(element)) || null;
  },

  factorSubmit() {
    return document.querySelector('#new_user button[type="submit"],form button[type="submit"]');
  },

  blocked() {
    return '';
  },

  captcha() {
    return Array.from(document.querySelectorAll(
      '.g-recaptcha,.h-captcha,iframe[src*="recaptcha"],iframe[src*="hcaptcha"],.js-captcha',
    )).some(element => this.visible(element));
  },

  error(seen) {
    const message = Array.from(document.querySelectorAll(
      '.flash-alert,.flash-danger,.alert-danger,.gl-alert-danger',
    )).find(element => this.visible(element) && !seen.has(element) && element.textContent.trim());
    return message ? message.textContent.trim().slice(0, 120) : '';
  },
};
