// CX Toolkit 网页快开：内容脚本与主程序原生消息宿主之间的中转，自身不保存任何登录信息。
const HOST_NAME = 'com.cxvpntools.web_quick';
const HEARTBEAT_ALARM = 'cx-web-quick-heartbeat';

importScripts('platforms/ifort.js', 'platforms/jumpserver.js', 'platforms/gitlab.js');

const platforms = globalThis.CXWebQuickPlatforms || {};

function platformFor(url, id) {
  try {
    const platform = platforms[id];
    return platform && platform.match(new URL(url)) ? platform : null;
  } catch (error) {
    return null;
  }
}

function native(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(response && typeof response === 'object' ? response
        : { ok: false, error: '宿主返回内容无效' });
    });
  });
}

function heartbeat() {
  return native({ type: 'hello' }).catch(() => ({ ok: false, error: '宿主连接失败' }));
}

chrome.runtime.onInstalled.addListener(() => { void heartbeat(); });
chrome.runtime.onStartup.addListener(() => { void heartbeat(); });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) void heartbeat();
});
chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 1 });
void heartbeat();

function text(value, limit) {
  return typeof value === 'string' ? value.slice(0, limit) : '';
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (!message || typeof message.type !== 'string' || sender.id !== chrome.runtime.id) {
      return { ok: false, error: '请求无效' };
    }
    const page = sender.tab && sender.tab.url ? sender.tab.url : '';
    const platform = platformFor(page, message.platform);
    if (message.type === 'hello') {
      // 只有扩展弹出页可以探测连接；页面不能借此反复唤起宿主进程。
      return page ? { ok: false, error: '请求无效' } : native({ type: 'hello' });
    }
    if (!platform) {
      return { ok: false, error: '请求无效' };
    }
    if (message.type === 'claim') {
      return native({
        type: 'claim',
        platform: platform.id,
        href: page,
        login: message.login === true,
        run_id: text(message.run_id, 64),
      });
    }
    if (message.type === 'report') {
      return native({
        type: 'report',
        platform: platform.id,
        run_id: text(message.run_id, 64),
        state: message.state === 'success' ? 'success' : 'failed',
        detail: text(message.detail, 200),
      });
    }
    return { ok: false, error: '请求无效' };
  })().catch(() => ({ ok: false, error: '扩展后台异常' })).then(sendResponse);
  return true;
});
