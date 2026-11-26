// 点击扩展图标时探测与主程序的连接，便于发现宿主未登记或主程序未启动。
const REASONS = {
  app_not_running: '未连接到 CX Toolkit：请先启动 CX Toolkit。',
  app_unreachable: '无法访问 CX Toolkit 的自动登录通道，请重启 CX Toolkit。',
  host_error: 'CX Toolkit 宿主处理消息失败，请查看运行日志。',
};

document.getElementById('status').textContent = '正在连接 CX Toolkit…';
chrome.runtime.sendMessage({ type: 'hello' }, (response) => {
  const status = document.getElementById('status');
  if (chrome.runtime.lastError) {
    status.textContent = '扩展后台未响应：' + chrome.runtime.lastError.message;
    return;
  }
  if (response && response.ok) {
    status.textContent = '已连接 CX Toolkit。在 CX Toolkit 的“网页快开”中打开自动登录条目即可使用。';
    return;
  }
  const error = response && typeof response.error === 'string' ? response.error : '';
  status.textContent = REASONS[error] || ('连接 CX Toolkit 失败：' + (error || '未知原因')
    + '\n请确认 CX Toolkit 为打包安装版本且正在运行，然后在 edge://extensions 重新加载本扩展。');
});
