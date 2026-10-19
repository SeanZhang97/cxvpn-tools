// 离线布局回归：仅加载本地模板/样式和合成数据，不运行应用脚本或访问 pywebview。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawnSync } = require('node:child_process');

const root = path.join(__dirname, '..');
const browser = process.env.CXVPN_TEST_BROWSER || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
].find(file => fs.existsSync(file));
assert.ok(browser, '需要本地 Chromium，或设置 CXVPN_TEST_BROWSER');
const output = path.join(root, 'build_tmp', 'ui-responsive');
fs.mkdirSync(output, { recursive: true });
const original = fs.readFileSync(path.join(root, 'ui/index.html'), 'utf8');
const template = original.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
  .replace(/<link rel="stylesheet" href="([^"]+)">/g, (_, file) =>
    `<style>${fs.readFileSync(path.join(root, 'ui', file), 'utf8')}</style>`)
  .replace('<head>', `<head><base href="${pathToFileURL(path.join(root, 'ui/')).href}/">`);

function inspectLayout(scenario) {
  const q = selector => document.querySelector(selector);
  const bounds = selector => {
    const r = q(selector).getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
  };
  const failures = [];
  const check = (condition, message) => { if (!condition) failures.push(message); };
  q('#ov-name').textContent = scenario.long ? '办公网络中文 e\u0301 🇨🇳 超长名称'.repeat(4) : '办公网络 IKEv2';
  q('#btn-connect').classList.add('hidden');
  q('#ov-default-status').textContent = '已连接';
  q('#ov-default-status').className = 'connection-state on';
  q('#ip-local-address').textContent = '192.168.100.119';
  q('#ip-domestic-address').textContent = '203.100.144.175';
  q('#ip-overseas-address').textContent = '198.100.244.106';
  // 行结构使用与生产列表相同的 CSS 类，所有地址/名称均为离线合成数据。
  q('#ov-active-connections').innerHTML = Array.from({ length: scenario.rows }, (_, index) => `
    <div class="active-connection${index ? ' disconnected' : ' is-default'}">
      <button class="vpn-drag">⠿</button><span class="connection-status-dot on"></span>
      <div class="active-connection-name"><strong>${scenario.long ? '测试中文 é 🇨🇳 长名称'.repeat(3) : '办公网络 ' + (index + 1)}</strong></div>
      <div class="active-connection-meta">${index ? '未连接' : '10.0.0.2/32 · 已连接 2 小时'}</div>
      <div class="connection-row-actions"><button class="connection-icon-action">○</button><button class="connection-icon-action more">…</button></div>
    </div>`).join('') + Array.from({ length: Math.max(0, 5 - scenario.rows) }, () => `
      <button class="connection-slot-empty"><span class="slot-plus">+</span><span>添加 VPN 配置</span><span class="slot-dash">—</span><span>›</span></button>`).join('');
  // 关闭页面进入动画，避免测量 transform 过渡中的中间坐标。
  const style = document.createElement('style');
  style.textContent = '* { animation: none !important; transition: none !important; }';
  document.head.append(style);
  const main = q('main');
  const overview = bounds('#page-overview');
  const workspace = bounds('.overview-workspace');
  const ip = bounds('.ip-overview');
  const list = bounds('.active-connections');
  const automation = bounds('.automation-matrix');
  const footer = bounds('.renewal-strip');
  const disconnectSize = bounds('#btn-disconnect');
  const disconnectStyle = getComputedStyle(q('#btn-disconnect'));
  const proxyToggle = q('#btn-proxy-toggle');
  const proxyClass = proxyToggle.className;
  proxyToggle.className = 'btn proxy-toggle danger';
  const proxyStyle = getComputedStyle(proxyToggle);
  for (const property of ['color', 'borderTopColor', 'backgroundColor']) {
    check(disconnectStyle[property] === proxyStyle[property], `断开 VPN 与关闭代理的 ${property} 不一致`);
  }
  proxyToggle.className = proxyClass;
  const disconnectFont = disconnectStyle.font;
  const disconnectRadius = disconnectStyle.borderRadius;
  const disconnectIcon = getComputedStyle(q('#btn-disconnect'), '::after');
  const disconnectIconSize = [disconnectIcon.width, disconnectIcon.height];
  q('#btn-disconnect').classList.add('hidden');
  q('#btn-connect').classList.remove('hidden');
  const connectSize = bounds('#btn-connect');
  const connectStyle = getComputedStyle(q('#btn-connect'));
  const connectIcon = getComputedStyle(q('#btn-connect'), '::after');
  check(Math.abs(connectSize.width - disconnectSize.width) < 1 &&
    Math.abs(connectSize.height - disconnectSize.height) < 1, '默认 VPN 连接与断开按钮尺寸不一致');
  check(connectStyle.font === disconnectFont && connectStyle.borderRadius === disconnectRadius,
    '默认 VPN 按钮字重或圆角不一致');
  check(connectIcon.width === disconnectIconSize[0] && connectIcon.height === disconnectIconSize[1],
    '默认 VPN 按钮图标尺寸不一致');
  for (const selector of ['#btn-connect', '#btn-disconnect']) {
    const button = q(selector);
    q('#btn-connect').classList.toggle('hidden', selector !== '#btn-connect');
    q('#btn-disconnect').classList.toggle('hidden', selector !== '#btn-disconnect');
    const label = button.textContent;
    const labels = selector === '#btn-connect' ? [label, '配置 VPN', '授权后重连中…'] : [label, '断开中…'];
    for (const text of labels) {
      button.textContent = text;
      button.disabled = text.endsWith('…');
      check(button.scrollWidth <= button.clientWidth + 1, `${selector} 文字或图标溢出`);
      check(bounds(selector).right <= bounds('.connection-focus').right + 1, `${selector} 超出默认 VPN 区域`);
      check(Math.abs(bounds(selector).width - disconnectSize.width) < 1, `${selector} 忙碌状态宽度跳变`);
    }
    button.textContent = label;
    button.disabled = false;
  }
  q('#btn-connect').classList.add('hidden');
  q('#btn-disconnect').classList.remove('hidden');
  check(main.scrollWidth <= main.clientWidth + 1, '总览横向溢出');
  check(ip.right <= automation.x + 1, '出口卡片与自动化区重叠');
  check(Math.abs(ip.right - list.right) <= 1, '出口卡片和连接列表右侧未对齐');
  check(footer.y >= workspace.bottom - 1, '授权区与工作区重叠');
  if (!scenario.long && scenario.rows <= 5) {
    check(main.scrollHeight <= main.clientHeight + 1, '常规五槽总览出现纵向滚动');
  } else {
    main.scrollTop = main.scrollHeight;
    check(bounds('.renewal-strip').bottom <= bounds('main').bottom + 1, '高密度内容滚动后授权区不可达');
    main.scrollTop = 0;
  }
  if (innerWidth >= 1920) {
    check(overview.width >= Math.min(1920, main.clientWidth - 80), '宽屏仍受旧最大宽度限制');
    check(automation.width <= 501, '自动化区被过度拉宽');
  }
  for (const selector of ['#ip-domestic-address', '#ip-overseas-address']) {
    const node = q(selector);
    check(node.scrollWidth <= node.clientWidth + 1, `${selector} IPv4 被截断`);
  }
  for (const node of document.querySelectorAll('.active-connection, .connection-slot-empty')) {
    check(node.scrollWidth <= node.clientWidth + 1, '连接行内容溢出');
  }
  const overviewPage = q('#page-overview');
  const standardWidths = {};
  const overflows = [];
  // 共享宽度契约覆盖全部静态标准页面；不触发真实后端调用。
  for (const page of document.querySelectorAll('.standard-page')) {
    overviewPage.classList.remove('active');
    page.classList.add('active');
    standardWidths[page.id] = page.getBoundingClientRect().width;
    if (main.scrollWidth > main.clientWidth + 1) overflows.push({ page: page.id,
      elements: [...page.querySelectorAll('*')].filter(node => node.getBoundingClientRect().right > bounds('main').right)
        .slice(0, 8).map(node => node.id || node.className) });
    check(main.scrollWidth <= main.clientWidth + 1, `${page.id} 横向溢出`);
    // 2FA 在独立样式中保留自身卡片网格上限，不由共享宽度契约覆盖。
    const expectedWidth = page.id === 'page-two-factor' ? Math.min(1320, overview.width) : overview.width;
    check(Math.abs(standardWidths[page.id] - expectedWidth) <= 1, `${page.id} 与预期宽度不一致`);
    page.classList.remove('active');
  }
  // 复用真实节点容器及卡片 CSS，独立检查宽屏列数、长名称与按钮热区。
  const nodesPage = q('#page-nodes');
  const grid = q('#routing-node-grid');
  const metadata = q('#routing-node-metadata');
  const card = document.createElement('section');
  card.className = 'card';
  q('#routing-nodes-mount').append(card);
  card.append(metadata, grid);
  metadata.classList.remove('hidden');
  metadata.innerHTML = Array.from({ length: 3 }, () =>
    '<div class="routing-node-meta-item"><strong>订阅信息</strong><small>不参与节点计数</small></div>').join('');
  grid.innerHTML = Array.from({ length: scenario.rows ? 36 : 0 }, () => `
    <article class="routing-node"><div><span class="routing-node-identity"><strong>中文 e\u0301 \u{1F1E8}\u{1F1F3} ${'长名称'.repeat(20)}</strong></span><span class="delay">120 ms</span></div>
      <div class="routing-node-meta"><small>Vless</small><small>上次检查：10-15 13:40</small></div>
      <div class="routing-node-action"><span>可设为订阅节点</span><div class="routing-node-action-buttons"><button class="btn mini ghost">测速</button><button class="btn mini ghost">使用此节点</button></div></div></article>`).join('');
  nodesPage.classList.add('active');
  const columns = getComputedStyle(grid).gridTemplateColumns.split(' ').length;
  if (innerWidth >= 2550) check(columns >= 5, '宽屏节点列表未增加到至少五列');
  if (innerWidth === 1080) check(columns === 2, '最小窗口节点应保持两列');
  if (innerWidth >= 1440) check(new Set([...metadata.children].map(node => node.offsetTop)).size === 1,
    '宽屏订阅元信息未并排');
  for (const node of grid.children) {
    check(node.scrollWidth <= node.clientWidth + 1, '节点卡片内容横向溢出');
    for (const button of node.querySelectorAll('button')) {
      check(button.getBoundingClientRect().height >= 32, '节点按钮热区不足');
    }
  }
  main.scrollTop = main.scrollHeight;
  if (grid.lastElementChild) check(grid.lastElementChild.getBoundingClientRect().bottom <= bounds('main').bottom + 1,
    '节点列表末项滚动不可达');
  check(main.scrollWidth <= main.clientWidth + 1, '节点页横向溢出');
  nodesPage.classList.remove('active');
  overviewPage.classList.add('active');
  main.scrollTop = 0;
  const report = { viewport: [innerWidth, innerHeight], overview, footer, nodeColumns: columns, failures, standardWidths, overflows };
  const result = parent.document.createElement('script');
  result.id = 'layout-result';
  result.type = 'application/json';
  result.textContent = JSON.stringify(report);
  parent.document.body.append(result);
}

const scenarios = [
  { width: 1080, height: 680, rows: 2 },
  { width: 1440, height: 900, rows: 5 },
  { width: 1441, height: 1000, rows: 2 },
  { width: 1600, height: 1000, rows: 5 },
  { width: 1700, height: 892, rows: 2 },
  { width: 1920, height: 1080, rows: 2 },
  { width: 2550, height: 1338, rows: 2 },
  { width: 3440, height: 1440, rows: 0 },
  { width: 1920, height: 720, rows: 5 },
  { width: 1080, height: 680, rows: 12, long: true },
];
const failures = [];
for (const scenario of scenarios) {
  const name = `${scenario.width}x${scenario.height}-${scenario.rows}${scenario.long ? '-long' : ''}`;
  const fixture = path.join(output, `${name}.html`);
  const content = path.join(output, `${name}-content.html`);
  fs.writeFileSync(content, template.replace('</body>',
    `<script>(${inspectLayout.toString()})(${JSON.stringify(scenario)});</script></body>`), 'utf8');
  // iframe 的 CSS 视口固定，避免 Chromium 的窗口边框/截图模式改变被测 innerWidth/Height。
  fs.writeFileSync(fixture, `<!doctype html><html><body style="margin:0;overflow:hidden;background:#0b1020">` +
    `<iframe src="${pathToFileURL(content).href}" style="display:block;border:0;width:${scenario.width}px;height:${scenario.height}px"></iframe></body></html>`, 'utf8');
  const profile = fs.mkdtempSync(path.join(output, 'profile-'));
  try {
    const args = ['--headless', '--disable-gpu', '--disable-extensions', '--disable-background-networking',
      '--no-first-run', '--no-default-browser-check', '--force-device-scale-factor=1',
      '--allow-file-access-from-files', `--user-data-dir=${profile}`, `--window-size=${scenario.width},${scenario.height}`,
      '--virtual-time-budget=1000', '--dump-dom'];
    if (process.argv.includes('--screenshots')) args.push(`--screenshot=${path.join(output, `${name}.png`)}`);
    args.push(pathToFileURL(fixture).href);
    const result = spawnSync(browser, args, { encoding: 'utf8', windowsHide: true, timeout: 20000, maxBuffer: 4 * 1024 * 1024 });
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr.slice(-1000));
    const reportText = result.stdout.match(/<script id="layout-result" type="application\/json">(.*?)<\/script>/s)?.[1];
    assert.ok(reportText, `${name}: 未生成布局报告`);
    const report = JSON.parse(reportText);
    fs.writeFileSync(path.join(output, `${name}.json`), JSON.stringify(report, null, 2), 'utf8');
    assert.deepEqual(report.viewport, [scenario.width, scenario.height]);
    if (report.failures.length) failures.push({ name, ...report });
    console.log(`${name}: ${report.failures.length ? JSON.stringify(report.failures) : 'PASS'} (viewport ${report.viewport.join('x')}, content ${report.overview.width}px)`);
  } finally {
    // 只删除本测试创建的专用 Chromium profile，不接触桌面浏览器或用户数据。
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  }
}
assert.deepEqual(failures, [], JSON.stringify(failures));
