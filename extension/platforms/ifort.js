// 亚信 AIS-iFort 登录页适配：Vue 2 + Element UI。只读取页面状态与填写表单，不访问页面脚本。
globalThis.CXWebQuickPlatforms = globalThis.CXWebQuickPlatforms || {};
globalThis.CXWebQuickPlatforms.ifort = {
  id: 'ifort',
  origin: 'https://10.0.34.53',

  match(location) {
    return location.origin === this.origin;
  },

  isLoginRoute(location) {
    return /^\/ais-ifort\/login\/?$/i.test(location.pathname);
  },

  form() {
    const username = document.querySelector('#login-username');
    const password = document.querySelector('#login-password');
    const submit = document.querySelector('#login-btn');
    return username && password && submit ? { username, password, submit } : null;
  },

  token() {
    try {
      return sessionStorage.getItem('token') || '';
    } catch (error) {
      return '';
    }
  },

  visible(element) {
    return Boolean(element) && element.getClientRects().length > 0;
  },

  // 第二步登录的动态口令输入框：第二步展开后才渲染，与密码框同为 password 类型。
  extraFactor() {
    return document.querySelector('.loginForm input[type="password"]:not(#login-password)');
  },

  // 需要人工处理的页面状态；返回原因文本，无需处理时返回空。
  blocked() {
    const dialog = Array.from(document.querySelectorAll('.el-dialog__wrapper'))
      .find((element) => this.visible(element) && element.querySelector('.el-dialog'));
    if (dialog) {
      const title = dialog.querySelector('.el-dialog__title');
      return '登录停在需要人工处理的步骤：' + (title && title.textContent.trim() ? title.textContent.trim() : '请按页面提示完成');
    }
    const select = Array.from(document.querySelectorAll('.loginForm .el-select')).some((element) => this.visible(element));
    return select ? '该账号需要 USBKey 登录，暂不支持自动登录' : '';
  },

  captcha() {
    return Array.from(document.querySelectorAll('.verify-bar-area')).some((element) => this.visible(element));
  },

  error(seen) {
    const message = Array.from(document.querySelectorAll('.el-message--error .el-message__content'))
      .find((element) => !seen.has(element));
    return message ? message.textContent.trim().slice(0, 120) : '';
  },
};
