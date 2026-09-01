# -*- coding: utf-8 -*-
"""core/browser_win.py - 原生内嵌浏览器 (主窗口内嵌 WebView2 面板)

真·内嵌: 主窗口 (pywebview/WinForms) 内追加第二个 WebView2 控件，覆盖在
浏览器页预留的工作区画布内。主 UI 始终铺满窗口，切换其它页面时原生控件
隐藏；浏览器使用独立持久化目录，原生渲染 60fps，用户可直接操作。

- 自动化经 JS 桥接驱动: ExecuteScriptAsync **不等待 JS Promise**
  (async 脚本直接返回 '{}'), 桥接以"两阶段等待"补齐 —— async 表达式
  结果暂存页内全局变量, 同步脚本轮询读回 (见 eval_js/_exec_raw);
  坐标点击带红圈编号标记, 自动化操作在页面上可见;
- 抓包: WebResourceRequested/ResponseReceived (metadata) + 页内 fetch/XHR
  钩子 (请求体/响应体, AddScriptToExecuteOnDocumentCreated 注入, 每次导航
  自动生效), worker 定时排空落盘 netlog.jsonl;
- 新窗口请求 (target=_blank/window.open, 如授权成功页) 在同窗口内导航。

PageShim 提供 web_flow/captcha_handler 所需的 Playwright 兼容子集:
evaluate / eval_on_selector / query_selector(_all) / click / goto /
reload / url / bring_to_front / wait_for_selector。
"""
import json
import os
import secrets
import threading
import time

# 页内基础设施 (选择器引擎 + 点击标记 + fetch/XHR 抓包钩子), 幂等
INSTALL_JS = r"""() => {
  if (window.__cx) return 'ok';
  const cx = window.__cx = { els: [], netlog: [] };
  // ---------- 点击标记: 固定定位红圈+编号, 自动淡出 ----------
  cx.mark = (x, y, label) => {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;left:' + (x - 14) + 'px;top:' + (y - 14) +
      'px;width:28px;height:28px;margin:0;border:3px solid #f43f5e;' +
      'border-radius:50%;box-shadow:0 0 0 2px rgba(255,255,255,.85),0 2px 10px rgba(0,0,0,.45);' +
      'z-index:2147483647;pointer-events:none;display:flex;align-items:center;' +
      'justify-content:center;font:bold 14px/1 sans-serif;color:#fff;' +
      'text-shadow:0 1px 2px #000;transition:opacity .5s;';
    d.textContent = label == null ? '' : String(label);
    document.documentElement.appendChild(d);
    setTimeout(() => { d.style.opacity = '0'; }, 1800);
    setTimeout(() => d.remove(), 2500);
  };
  // ---------- 选择器引擎 (Playwright 常用子集) ----------
  const vis = el => {
    if (!el || !el.getClientRects) return false;
    if (!el.getClientRects().length) return false;
    const b = el.getBoundingClientRect();
    // 站点验证码 SDK 会预插 0x0 的隐藏容器 (#eject), 空矩形必须算隐藏
    if (b.width < 1 || b.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const textOf = el => (el.textContent || '').replace(/\s+/g, ' ').trim();
  const pick = list => {
    let best = null;
    // 同长度取文档序靠后 (最内层): 外层容器 textContent 与按钮同文时
    // 点容器不会冒泡到按钮 handler, 必须命中最内层元素
    for (const el of list) { if (!vis(el)) continue;
      if (!best || textOf(el).length <= textOf(best).length) best = el; }
    return best;
  };
  function match(sel, root) {
    root = root || document;
    let m;
    if (sel.startsWith('text=')) {
      const t = sel.slice(5).trim();
      const b = pick([...root.querySelectorAll('*')].filter(
        el => textOf(el).includes(t)));
      return b ? [b] : [];
    }
    if ((m = sel.match(/^([a-zA-Z][\w-]*)?:has-text\("([^"]*)"\)$/))) {
      const t = m[2];
      const b = pick([...root.querySelectorAll(m[1] || '*')].filter(
        el => textOf(el).includes(t)));
      return b ? [b] : [];
    }
    if (sel.includes(':near(')) return [];   // 上层有兜底分支
    try { return [...root.querySelectorAll(sel)].filter(vis); }
    catch (e) { return []; }
  }
  cx.q = (sel, all, rootId) => {
    const root = rootId != null ? cx.els[rootId] : document;
    const list = match(sel, root);
    if (!list.length) return all ? [] : null;
    const ids = list.map(el => { cx.els.push(el); return cx.els.length - 1; });
    if (cx.els.length > 500) cx.els.splice(0, cx.els.length - 500);
    return all ? ids : ids[0];
  };
  // ---------- 元素操作 ----------
  cx.act = (id, act, val) => {
    const el = cx.els[id]; if (!el) return null;
    if (act === 'click') {
      el.scrollIntoView({ block: 'center' });
      const r = el.getBoundingClientRect();
      cx.mark(r.left + r.width / 2, r.top + r.height / 2, '');
      el.click(); return true;
    }
    if (act === 'fill') {
      const proto = el.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    if (act === 'text') return (el.textContent || '').trim();
    if (act === 'visible') return !!el.getClientRects().length;
    if (act === 'enabled') return !el.disabled;
    return null;
  };
  // ---------- 坐标点击 (模拟真实鼠标事件序列 + 标记) ----------
  cx.clickAt = (sel, x, y, label) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false };
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    const cxr = r.left + x, cyr = r.top + y;
    cx.mark(cxr, cyr, label);
    for (const t of ['mousedown', 'mouseup', 'click']) {
      el.dispatchEvent(new MouseEvent(t, {
        bubbles: true, cancelable: true, view: window,
        clientX: cxr, clientY: cyr, button: 0 }));
    }
    return { ok: true };
  };
  // ---------- fetch/XHR 抓包钩子 (请求体+响应体) ----------
  const push = e => {
    e.ts = Date.now();
    cx.netlog.push(e);
    if (cx.netlog.length > 300) cx.netlog.splice(0, cx.netlog.length - 300);
  };
  const str = b => {
    if (b == null) return null;
    if (typeof b === 'string') return b;
    if (b instanceof URLSearchParams) return b.toString();
    if (typeof FormData !== 'undefined' && b instanceof FormData) {
      const o = {};
      for (const [k, v] of b.entries()) o[k] = String(v);
      return JSON.stringify(o);
    }
    return null;
  };
  const absoluteUrl = u => {
    try { return new URL(String(u || ''), location.href).href; }
    catch (e) { return String(u || ''); }
  };
  const of = window.fetch;
  window.fetch = async function (input, init) {
    const url = absoluteUrl(typeof input === 'string' ? input : input.url);
    const method = (init && init.method) ||
      (typeof input !== 'string' && input.method) || 'GET';
    const rb = str(init && init.body);
    try {
      const res = await of.apply(this, arguments);
      let rt = null;
      try {
        const ct = res.headers.get('content-type') || '';
        if (/json|text|xml|urlencoded/.test(ct))
          rt = await res.clone().text();
      } catch (e) {}
      push({ src: 'xhr', method, url, status: res.status, req: rb, res: rt });
      return res;
    } catch (err) {
      push({ src: 'xhr', method, url, status: 0, err: String(err), req: rb });
      throw err;
    }
  };
  const XO = window.XMLHttpRequest;
  window.XMLHttpRequest = function () {
    const x = new XO();
    const open = x.open.bind(x);
    let m = 'GET', u = '';
    x.open = function (mm, uu) {
      m = mm; u = absoluteUrl(uu); return open.apply(x, arguments);
    };
    const send = x.send.bind(x);
    x.send = function (body) {
      const rb = str(body);
      x.addEventListener('loadend', () => {
        let rt = null;
        try {
          const ct = x.getResponseHeader('content-type') || '';
          if (/json|text|xml|urlencoded/.test(ct))
            rt = String(x.responseText || '');
        } catch (e) {}
        push({ src: 'xhr', method: m, url: u, status: x.status, req: rb, res: rt });
      });
      return send.apply(x, arguments);
    };
    return x;
  };
  window.XMLHttpRequest.prototype = XO.prototype;
  // ---------- 浮动返回按钮: 授权成功页等新页面一键回到 /vpn 操作页 ----------
  // (站点 window.open 的新页被同窗口导航接管后, 原生面板无地址栏, 需返回入口;
  //  批量下线按站点设计本就不开新页, 成功=弹框内 toast+行变"未授权")
  cx.backbar = () => {
    const old = document.getElementById('cx-back');
    if (old) old.remove();
    if (!/chaoxing\.com/.test(location.host)) return;
    if (location.pathname === '/vpn') return;
    const b = document.createElement('button');
    b.id = 'cx-back';
    b.textContent = '← 返回 VPN 操作页';
    b.style.cssText = 'position:fixed;top:10px;left:10px;z-index:2147483647;' +
      'padding:8px 14px;border:none;border-radius:8px;background:#0f766e;' +
      'color:#fff;font:13px/1 sans-serif;cursor:pointer;' +
      'box-shadow:0 2px 10px rgba(0,0,0,.4);';
    b.onclick = () => { location.href = '/vpn'; };
    (document.body || document.documentElement).appendChild(b);
  };
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', cx.backbar);
  else cx.backbar();
  return 'ok';
}"""

# 浏览器工作区浮层: 真实执行进度 + 可收起的网络请求抽屉。
# 使用 Shadow DOM 隔离站点样式，页面导航时由 document-created 自动重建。
OVERLAY_JS = r"""() => {
  if (window.__cxPanel) return 'ok';
  const host = document.createElement('div');
  host.id = 'cx-manager-overlay';
  host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;' +
    'pointer-events:none;font-family:"Segoe UI Variable","Segoe UI",' +
    '"Microsoft YaHei",sans-serif;';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `
    <style>
      * { box-sizing: border-box; }
      :host { --drawer-height: 186px; --drawer-bottom: 18px;
        --drawer-gap: 14px; }
      button { font: inherit; }
      .progress-card {
        position: fixed; left: 18px; bottom: 18px; width: 258px;
        color: #e6eaf5; background: rgba(13, 20, 40, .96);
        border: 1px solid rgba(148, 163, 184, .22); border-radius: 14px;
        box-shadow: 0 18px 46px rgba(2, 6, 23, .38);
        overflow: hidden; pointer-events: auto; transition: bottom .2s ease;
        backdrop-filter: blur(16px);
      }
      .progress-card.drawer-open { bottom: calc(var(--drawer-bottom) +
        var(--drawer-height) + var(--drawer-gap)); }
      .progress-card.resizing { transition: none; }
      .progress-card.completed { border-color: rgba(52,211,153,.46);
        box-shadow: 0 18px 46px rgba(2,6,23,.38), 0 0 24px rgba(52,211,153,.12); }
      .progress-head {
        width: 100%; display: flex; align-items: center; gap: 9px;
        padding: 13px 14px 11px; color: #e6eaf5; background: transparent;
        border: 0; cursor: pointer; text-align: left;
      }
      .state-dot { width: 9px; height: 9px; flex: none; border-radius: 50%;
        background: #34d399; box-shadow: 0 0 12px rgba(52,211,153,.9); }
      .state-dot.failed { background: #f87171;
        box-shadow: 0 0 12px rgba(248,113,113,.8); }
      .state-dot.idle { background: #8b93ab; box-shadow: none; }
      .head-label { flex: 1; font-size: 13px; font-weight: 650; }
      .chevron { color: #8b93ab; font-size: 12px; transition: transform .18s; }
      .progress-card.collapsed .chevron { transform: rotate(-90deg); }
      .progress-body { padding: 0 14px 12px; }
      .progress-card.collapsed .progress-body { display: none; }
      .summary { display: flex; justify-content: space-between; align-items: center;
        color: #cbd5e1; font-size: 12px; }
      .track { height: 5px; margin: 8px 0 12px; overflow: hidden;
        border-radius: 999px; background: rgba(148,163,184,.18); }
      .fill { height: 100%; width: 0; border-radius: inherit;
        background: linear-gradient(90deg,#6366f1,#22d3ee);
        transition: width .25s ease; }
      .current-title { font-size: 13px; font-weight: 650; line-height: 1.45; }
      .current-detail { margin-top: 3px; color: #8b93ab;
        font-size: 11px; line-height: 1.5; }
      .steps { margin: 12px 0 0; padding: 11px 0 2px;
        border-top: 1px solid rgba(148,163,184,.14); }
      .step { position: relative; display: flex; gap: 9px; min-height: 30px;
        color: #8b93ab; font-size: 11px; line-height: 20px; }
      .step:not(:last-child)::after { content: ''; position: absolute;
        left: 9px; top: 20px; bottom: -1px; width: 1px;
        background: rgba(148,163,184,.25); }
      .step.done:not(:last-child)::after { background: rgba(52,211,153,.65); }
      .step-mark { position: relative; z-index: 1; width: 19px; height: 19px;
        flex: none; border-radius: 50%; display: grid; place-items: center;
        border: 1px solid rgba(148,163,184,.5); color: #94a3b8;
        background: #111a31; font-size: 10px; }
      .step.done { color: #cbd5e1; }
      .step.done .step-mark { color: #052e22; border-color: #34d399;
        background: #34d399; }
      .step.current { color: #a5b4fc; font-weight: 650; }
      .step.current .step-mark { color: white; border-color: #6366f1;
        background: #6366f1; box-shadow: 0 0 12px rgba(99,102,241,.55); }
      .step.failed { color: #fca5a5; }
      .step.failed .step-mark { color: white; border-color: #f87171;
        background: #f87171; }
      .net-toggle { width: 100%; display: flex; align-items: center;
        justify-content: space-between; padding: 11px 14px; color: #dbe4f3;
        background: rgba(99,102,241,.08); border: 0;
        border-top: 1px solid rgba(148,163,184,.14); cursor: pointer; }
      .net-toggle:hover { background: rgba(99,102,241,.16); }
      .net-count { min-width: 30px; padding: 2px 7px; border-radius: 999px;
        color: #c7d2fe; background: rgba(99,102,241,.28); font-size: 11px; }
      .drawer { position: fixed; left: 18px; right: 18px; bottom: 18px;
        height: var(--drawer-height); min-height: 186px; max-height: 70vh;
        color: #dbe4f3; background: rgba(13,20,40,.97);
        border: 1px solid rgba(148,163,184,.22); border-radius: 14px;
        box-shadow: 0 18px 48px rgba(2,6,23,.38); overflow: hidden;
        pointer-events: auto; transform: translateY(calc(100% + 24px));
        opacity: 0; transition: transform .2s ease, opacity .2s ease;
        backdrop-filter: blur(16px);
      }
      .drawer.open { transform: translateY(0); opacity: 1; }
      .drawer.resizing { transition: none; user-select: none; }
      .drawer-resize { position: absolute; z-index: 2; top: 0; left: 50%;
        width: 76px; height: 12px; padding: 0; transform: translateX(-50%);
        border: 0; outline: 0; background: transparent; cursor: ns-resize;
        touch-action: none; }
      .drawer-resize::after { content: ''; position: absolute; left: 20px;
        right: 20px; top: 4px; height: 3px; border-radius: 999px;
        background: rgba(148,163,184,.38); transition: background .15s; }
      .drawer-resize:hover::after, .drawer-resize:focus-visible::after {
        background: #a5b4fc; }
      .drawer-head { height: 42px; display: flex; align-items: center;
        padding: 0 14px; border-bottom: 1px solid rgba(148,163,184,.14); }
      .drawer-title { font-size: 12px; font-weight: 650; }
      .drawer-spacer { flex: 1; }
      .drawer-close { padding: 5px 9px; border-radius: 7px; color: #a8b1c7;
        background: transparent; border: 1px solid rgba(148,163,184,.18);
        cursor: pointer; }
      .drawer-close:hover { color: #fff; background: rgba(255,255,255,.06); }
      .net-table { height: calc(100% - 42px); overflow-y: auto; overflow-x: hidden;
        overflow-anchor: none;
        scrollbar-width: thin; scrollbar-color: rgba(148,163,184,.42) transparent;
        font-family: "Cascadia Code",Consolas,monospace; font-size: 10px; }
      .net-row { display: grid; grid-template-columns: 76px 48px 46px 160px 1fr;
        gap: 9px; align-items: center; min-height: 27px; padding: 0 14px;
        border-bottom: 1px solid rgba(148,163,184,.09); cursor: pointer; }
      .net-row:hover { background: rgba(99,102,241,.12); }
      .net-time { color: #8b93ab; }
      .net-method { color: #a5f3fc; font-weight: 700; }
      .net-method.POST { color: #fbbf24; }
      .net-status { color: #4ade80; }
      .net-domain { color: #cbd5e1; overflow: hidden; text-overflow: ellipsis;
        white-space: nowrap; }
      .net-path { color: #a8b1c7; overflow: hidden; text-overflow: ellipsis;
        white-space: nowrap; }
      .net-empty { padding: 20px 14px; color: #8b93ab; font-size: 12px; }
      .net-detail { display: none; margin: 0; padding: 9px 14px;
        max-height: 78px; overflow: auto; white-space: pre-wrap;
        overflow-anchor: none;
        scrollbar-width: thin; scrollbar-color: rgba(148,163,184,.42) transparent;
        word-break: break-all; color: #a5f3fc; background: #0a1020;
        border-bottom: 1px solid rgba(148,163,184,.12); font-size: 10px; }
      .net-detail.open { display: block; }
      .net-table::-webkit-scrollbar, .net-detail::-webkit-scrollbar {
        width: 8px; height: 8px; }
      .net-table::-webkit-scrollbar-track, .net-detail::-webkit-scrollbar-track {
        background: rgba(148,163,184,.045); border-radius: 999px; }
      .net-table::-webkit-scrollbar-thumb, .net-detail::-webkit-scrollbar-thumb {
        min-height: 28px; border: 2px solid transparent; border-radius: 999px;
        background: rgba(148,163,184,.38); background-clip: padding-box; }
      .net-table::-webkit-scrollbar-thumb:hover,
      .net-detail::-webkit-scrollbar-thumb:hover {
        background: rgba(165,180,252,.62); background-clip: padding-box; }
      .net-table::-webkit-scrollbar-button, .net-detail::-webkit-scrollbar-button {
        display: none; width: 0; height: 0; }
      .net-table::-webkit-scrollbar-corner, .net-detail::-webkit-scrollbar-corner {
        background: transparent; }
      @media (max-width: 1100px) {
        :host { --drawer-bottom: 14px; }
        .progress-card { left: 14px; bottom: 14px; width: 230px; }
        .drawer { left: 14px; right: 14px; bottom: 14px; }
      }
      @media (max-width: 760px) {
        .progress-card { width: 224px; }
        .net-row { grid-template-columns: 68px 42px 40px 1fr; }
        .net-path { display: none; }
      }
    </style>
    <section id="progress-card" class="progress-card">
      <button id="progress-toggle" class="progress-head" type="button">
        <span id="state-dot" class="state-dot idle"></span>
        <span id="head-label" class="head-label">等待自动化任务</span>
        <span class="chevron">▼</span>
      </button>
      <div class="progress-body">
        <div class="summary"><span id="step-summary">步骤 0 / 5</span>
          <span id="percent">0%</span></div>
        <div class="track"><div id="progress-fill" class="fill"></div></div>
        <div id="current-title" class="current-title">等待自动化任务</div>
        <div id="current-detail" class="current-detail">浏览器可随时手动操作</div>
        <div id="steps" class="steps"></div>
      </div>
      <button id="net-toggle" class="net-toggle" type="button">
        <span id="net-label">网络请求</span><span id="net-count" class="net-count">0</span>
      </button>
    </section>
    <section id="drawer" class="drawer">
      <div id="drawer-resize" class="drawer-resize" role="separator"
        aria-label="调整网络请求面板高度" aria-orientation="horizontal"
        aria-valuemin="186" tabindex="0"></div>
      <div class="drawer-head"><span id="drawer-title" class="drawer-title">网络请求 (0)</span>
        <span class="drawer-spacer"></span>
        <button id="drawer-close" class="drawer-close" type="button">关闭</button></div>
      <div id="net-table" class="net-table"></div>
    </section>`;
  document.documentElement.appendChild(host);

  const $ = id => root.getElementById(id);
  const stepNames = ['启动浏览器', '打开授权页', '登录与短信验证',
                     '验证授权状态', '完成授权/续期'];
  let drawerOpen = false;
  let progressManuallyToggled = false;
  let rows = [];
  let openDetailKeys = new Set();
  let detailScrollTops = new Map();
  let renderedRowsSignature = null;
  let drawerHeight = 186;
  let resizeStart = null;

  const netRowKey = r => [r.ts || '', r.src || '', r.method || '',
    r.status == null ? '' : r.status, r.url || ''].join('\u001f');
  const netRowSignature = r => [netRowKey(r), r.err || '', r.req || '',
    r.res || ''].join('\u001d');

  function maxDrawerHeight() {
    return Math.max(186, Math.min(Math.round(window.innerHeight * .7),
                                  window.innerHeight - 36));
  }

  function setDrawerHeight(height) {
    const max = maxDrawerHeight();
    drawerHeight = Math.max(186, Math.min(Math.round(height), max));
    host.style.setProperty('--drawer-height', drawerHeight + 'px');
    $('drawer-resize').setAttribute('aria-valuemax', String(max));
    $('drawer-resize').setAttribute('aria-valuenow', String(drawerHeight));
  }

  function syncProgressDensity() {
    if (progressManuallyToggled) return;
    $('progress-card').classList.toggle('collapsed', window.innerWidth <= 1100);
  }

  function renderProgress(p) {
    p = p || {};
    const step = Math.max(0, Math.min(Number(p.step) || 0, 5));
    const status = p.status || 'idle';
    const completed = status === 'completed';
    $('progress-card').classList.toggle('completed', completed);
    const pct = completed ? 100 : Math.round(step / 5 * 100);
    $('head-label').textContent = p.active ? '自动化执行中'
      : (completed ? '自动化已完成' : status === 'failed'
        ? '自动化未完成' : '等待自动化任务');
    $('state-dot').className = 'state-dot ' +
      (status === 'failed' ? 'failed' : status === 'idle' ? 'idle' : '');
    $('step-summary').textContent = '步骤 ' + step + ' / 5';
    $('percent').textContent = pct + '%';
    $('progress-fill').style.width = pct + '%';
    $('current-title').textContent = p.title || '等待自动化任务';
    $('current-detail').textContent = p.detail || '浏览器可随时手动操作';
    $('steps').innerHTML = '';
    stepNames.forEach((name, i) => {
      const n = i + 1;
      const el = document.createElement('div');
      let cls = 'step';
      if (completed || n < step) cls += ' done';
      else if (n === step && status === 'failed') cls += ' failed';
      else if (n === step && status === 'running') cls += ' current';
      el.className = cls;
      const mark = document.createElement('span');
      mark.className = 'step-mark';
      mark.textContent = String(n);
      const label = document.createElement('span');
      label.textContent = name;
      el.append(mark, label);
      $('steps').appendChild(el);
    });
  }

  function renderRows() {
    const table = $('net-table');
    const renderedRows = rows.slice(-30).reverse();
    const signature = renderedRows.map(netRowSignature).join('\u001e');
    if (signature === renderedRowsSignature) return;

    const scrollTop = table.scrollTop;
    const keepAtTop = scrollTop <= 4;
    let anchorKey = null;
    let anchorOffset = 0;
    table.querySelectorAll('.net-detail.open').forEach(detail => {
      if (detail.__rowKey) detailScrollTops.set(detail.__rowKey, detail.scrollTop);
    });
    if (!keepAtTop) {
      const visibleRow = Array.from(table.querySelectorAll('.net-row'))
        .find(row => row.offsetTop + row.offsetHeight > scrollTop);
      if (visibleRow) {
        anchorKey = visibleRow.__rowKey || null;
        anchorOffset = visibleRow.offsetTop - scrollTop;
      }
    }

    table.innerHTML = '';
    renderedRowsSignature = signature;
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'net-empty';
      empty.textContent = '尚未捕获网络请求';
      table.appendChild(empty);
      return;
    }
    const availableKeys = new Set(renderedRows.map(netRowKey));
    openDetailKeys.forEach(key => {
      if (!availableKeys.has(key)) openDetailKeys.delete(key);
    });
    detailScrollTops.forEach((_, key) => {
      if (!availableKeys.has(key)) detailScrollTops.delete(key);
    });
    renderedRows.forEach(r => {
      let url;
      try { url = new URL(String(r.url || ''), location.href); }
      catch (e) { url = { host: '', pathname: String(r.url || '') }; }
      const t = r.ts ? new Date(r.ts) : new Date();
      const hh = [t.getHours(), t.getMinutes(), t.getSeconds()]
        .map(v => String(v).padStart(2, '0')).join(':');
      const line = document.createElement('div');
      line.className = 'net-row';
      const values = [hh, r.method || '-',
        r.status != null ? String(r.status) : (r.err ? 'ERR' : ''),
        url.host || '', (url.pathname || '') + (url.search || '')];
      const classes = ['net-time', 'net-method ' + (r.method || ''),
        'net-status', 'net-domain', 'net-path'];
      values.forEach((value, i) => {
        const span = document.createElement('span');
        span.className = classes[i];
        span.textContent = value;
        line.appendChild(span);
      });
      const detail = document.createElement('pre');
      detail.className = 'net-detail';
      const rowKey = netRowKey(r);
      line.__rowKey = rowKey;
      detail.__rowKey = rowKey;
      if (openDetailKeys.has(rowKey)) detail.classList.add('open');
      detail.textContent = (r.method || '-') + ' ' +
        (r.status != null ? r.status : '') + ' ' + (r.url || '') + '\n' +
        (r.err ? 'error: ' + r.err + '\n' : '') +
        (r.req ? '\n--- 请求体 ---\n' + r.req + '\n' : '') +
        (r.res ? '\n--- 响应体 ---\n' + r.res + '\n' : '');
      line.onclick = () => {
        const open = detail.classList.toggle('open');
        if (open) openDetailKeys.add(rowKey);
        else openDetailKeys.delete(rowKey);
      };
      detail.addEventListener('scroll', () => {
        detailScrollTops.set(rowKey, detail.scrollTop);
      }, { passive: true });
      table.append(line, detail);
      if (detail.classList.contains('open') && detailScrollTops.has(rowKey)) {
        detail.scrollTop = detailScrollTops.get(rowKey);
      }
    });
    if (keepAtTop) {
      table.scrollTop = 0;
    } else if (anchorKey) {
      const anchor = Array.from(table.querySelectorAll('.net-row'))
        .find(row => row.__rowKey === anchorKey);
      table.scrollTop = anchor ? anchor.offsetTop - anchorOffset : scrollTop;
    } else {
      table.scrollTop = scrollTop;
    }
  }

  function setDrawer(open) {
    drawerOpen = !!open;
    $('drawer').classList.toggle('open', drawerOpen);
    $('progress-card').classList.toggle('drawer-open', drawerOpen);
    if (drawerOpen && window.innerWidth <= 1100)
      $('progress-card').classList.add('collapsed');
    $('net-label').textContent = drawerOpen ? '收起请求' : '网络请求';
  }

  $('progress-toggle').onclick = () => {
    progressManuallyToggled = true;
    $('progress-card').classList.toggle('collapsed');
  };
  $('net-toggle').onclick = () => setDrawer(!drawerOpen);
  $('drawer-close').onclick = () => setDrawer(false);
  const resizeHandle = $('drawer-resize');
  resizeHandle.onpointerdown = e => {
    if (e.button !== 0) return;
    resizeStart = { y: e.clientY, height: drawerHeight };
    $('drawer').classList.add('resizing');
    $('progress-card').classList.add('resizing');
    resizeHandle.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  resizeHandle.onpointermove = e => {
    if (!resizeStart) return;
    setDrawerHeight(resizeStart.height + resizeStart.y - e.clientY);
  };
  const finishResize = e => {
    if (!resizeStart) return;
    resizeStart = null;
    $('drawer').classList.remove('resizing');
    $('progress-card').classList.remove('resizing');
    if (resizeHandle.hasPointerCapture(e.pointerId))
      resizeHandle.releasePointerCapture(e.pointerId);
  };
  resizeHandle.onpointerup = finishResize;
  resizeHandle.onpointercancel = finishResize;
  resizeHandle.onkeydown = e => {
    let next = null;
    if (e.key === 'ArrowUp') next = drawerHeight + 24;
    else if (e.key === 'ArrowDown') next = drawerHeight - 24;
    else if (e.key === 'Home') next = 186;
    else if (e.key === 'End') next = maxDrawerHeight();
    if (next == null) return;
    setDrawerHeight(next);
    e.preventDefault();
  };
  window.addEventListener('resize', syncProgressDensity);
  window.addEventListener('resize', () => setDrawerHeight(drawerHeight));

  window.__cxPanel = {
    update(payload) {
      payload = payload || {};
      rows = Array.isArray(payload.rows) ? payload.rows : rows;
      const count = Number(payload.net_count == null ? rows.length
                                                    : payload.net_count);
      $('net-count').textContent = String(count);
      $('drawer-title').textContent = '网络请求 (' + count + ')';
      renderProgress(payload.progress);
      renderRows();
      return true;
    }
  };
  renderProgress({ status: 'idle', step: 0, title: '等待自动化任务',
                   detail: '浏览器可随时手动操作' });
  renderRows();
  setDrawerHeight(drawerHeight);
  syncProgressDensity();
  return 'ok';
}"""


class El:
    """PageShim 元素句柄: 以页内注册表 id 代理 Playwright ElementHandle 子集"""

    def __init__(self, page, eid):
        self._page = page
        self._id = eid

    def _act(self, act, val=None):
        return self._page.evaluate(
            '(a) => window.__cx ? window.__cx.act(a[0], a[1], a[2]) : null',
            [self._id, act, val])

    def click(self):
        return self._act('click')

    def fill(self, value):
        return self._act('fill', value)

    def text_content(self):
        return self._act('text') or ''

    def is_visible(self):
        return bool(self._act('visible'))

    def is_enabled(self):
        return bool(self._act('enabled'))

    def query_selector(self, sel):
        eid = self._page.evaluate(
            '(a) => window.__cx ? window.__cx.q(a[0], false, a[1]) : null',
            [sel, self._id])
        return El(self._page, eid) if eid is not None else None


class PageShim:
    """Playwright 兼容子集, 底层为内嵌 WebView2 的 ExecuteScriptAsync"""

    def __init__(self, browser):
        self.b = browser

    def evaluate(self, fn_src, arg=None):
        """fn_src 为函数源码 (箭头/async 均可), arg 为单参数 (可省略)"""
        args = '' if arg is None else json.dumps(arg)
        return self.b.eval_js(f'({fn_src})({args})')

    def eval_on_selector(self, sel, fn_src):
        return self.evaluate(
            f'(s) => {{ const el = document.querySelector(s); '
            f'return el ? ({fn_src})(el) : null; }}', sel)

    def query_selector(self, sel):
        eid = self.evaluate(
            '(s) => window.__cx ? window.__cx.q(s, false, null) : null', sel)
        return El(self, eid) if eid is not None else None

    def query_selector_all(self, sel):
        ids = self.evaluate(
            '(s) => window.__cx ? window.__cx.q(s, true, null) : []', sel) or []
        return [El(self, i) for i in ids]

    def wait_for_selector(self, sel, timeout=8, **_kw):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.query_selector(sel) is not None:
                return True
            time.sleep(0.4)
        return False

    def click(self, sel, position=None, timeout=5000, mark=None):
        if position is not None:
            return self.evaluate(
                '(a) => window.__cx ? window.__cx.clickAt(a[0], a[1], a[2], a[3]) : null',
                [sel, position['x'], position['y'], mark])
        el = self.query_selector(sel)
        if el is None:
            raise Exception(f'click 目标不存在: {sel}')
        return el.click()

    def goto(self, url, wait_until=None, timeout=20):
        self.b.goto(url, timeout=timeout)

    def reload(self):
        return self.b.reload()

    @property
    def url(self):
        return self.b.current_url() or ''

    def bring_to_front(self):
        self.b.bring_to_front()


class NativeBrowser:
    """主窗口内嵌的 WebView2 浏览器面板"""

    def __init__(self, main_window, storage_path, log=print, visible=True):
        self.mw = main_window
        self.storage_path = storage_path
        self.log = log
        self.initial_visible = bool(visible)
        self.form = None
        self.ctrl = None
        self.ui_view = None
        self._core = None
        self.page = None
        self.alive = False
        self._ready = threading.Event()
        self._loaded = threading.Event()
        self._lock = threading.Lock()
        self._net = []
        self._overlay_lock = threading.Lock()
        self._overlay_payload = None

    # ---------- UI 线程调度 ----------
    def _ui(self, fn):
        from System import Func, Object
        holder = {}

        def wrapped():
            try:
                holder['v'] = fn()
            except Exception as e:
                holder['e'] = e
            return None
        self.form.Invoke(Func[Object](wrapped))
        if 'e' in holder:
            raise holder['e']
        return holder.get('v')

    def _exec_raw(self, js, timeout):
        """单次 ExecuteScriptAsync (UI 线程), 返回 ('ok', value) /
        ('err', msg) / ('timeout', None) / ('sched', msg)"""
        from System import Action, Func, Object, String
        from System.Threading.Tasks import Task
        holder = {}
        ev = threading.Event()

        def cb(task):
            try:
                res = task.Result
                try:
                    holder['v'] = json.loads(res)
                except Exception:
                    holder['v'] = res
            except Exception as e:  # noqa: BLE001
                holder['e'] = e
            ev.set()

        def do():
            try:
                self.ctrl.ExecuteScriptAsync(js).ContinueWith(
                    Action[Task[String]](cb))
            except Exception as e:  # noqa: BLE001
                holder['e'] = e
                ev.set()
            return None

        try:
            self.form.Invoke(Func[Object](do))
        except Exception as e:  # noqa: BLE001
            return 'sched', str(e)
        if not ev.wait(timeout):
            return 'timeout', None
        if 'e' in holder:
            return 'err', str(holder['e'])
        return 'ok', holder.get('v')

    def eval_js(self, script, timeout=30):
        """两阶段执行: WebView2 的 ExecuteScriptAsync 不等待 JS Promise
        (async 脚本序列化为 '{}'), 因此把表达式包进 async IIFE, 结果写入
        页内唯一全局槽 (['ok', value]/['err', msg]), 再由同步脚本轮询
        读回同一槽。同步表达式也走同一路径 (await 立即完成), 行为等价
        旧实现, 仅每次调用多一次轮询读回往返。异常语义与旧版一致: 返回
        None 并记日志。
        """
        key = '__wv2r_' + secrets.token_hex(6)
        wrapped = (
            'window["%(k)s"] = undefined;'
            '(async () => { try { window["%(k)s"] = ["ok", await (%(s)s)]; }'
            ' catch (e) { window["%(k)s"] = '
            '["err", String((e && e.message) || e)]; } })();'
        ) % {'k': key, 's': script}
        # 页内三态返回: null=异步体未落槽; ['bad']=值不可 JSON 编码
        # (等价旧行为返回 None); ['v', json] 再解一层还原结果。
        # ExecuteScriptAsync 会对脚本返回值再 JSON 编码一次, 若直接
        # 在脚本里 stringify 会双重转义, 折中用['bad']标志避歧义。
        poll = (
            'window["%(k)s"] === undefined ? null : (function(){'
            ' try { return ["v", JSON.stringify(window["%(k)s"])]; }'
            ' catch (e) { return ["bad"]; } })()'
        ) % {'k': key}
        st, v = self._exec_raw(wrapped, min(5, timeout))
        if st != 'ok':
            self.log(f'[browser] eval 提交失败: {st} {v}')
            return None
        deadline = time.time() + timeout
        while True:
            if time.time() >= deadline:
                self.log('[browser] eval 超时 (页面导航或异步结果未返回)')
                return None
            st, v = self._exec_raw(poll, min(2, max(0.05,
                                                   deadline - time.time())))
            if st == 'timeout':
                if time.time() < deadline:
                    time.sleep(0.02)
                    continue
                self.log('[browser] eval 超时 (两阶段轮询无结果)')
                return None
            if st != 'ok':
                self.log(f'[browser] eval 轮询失败: {st} {v}')
                return None
            if v is None:  # 异步体尚未落槽
                time.sleep(0.02)
                continue
            if v == ['bad']:  # 返回值不可 JSON 编码 (旧行为: null)
                return None
            try:
                payload = json.loads(v[1])
            except Exception:
                payload = v[1]
            if not (isinstance(payload, list) and len(payload) == 2):
                return payload
            status, value = payload[0], payload[1]
            if status == 'err':
                self.log(f'[browser] evaluate 异常: {value}')
                return None
            return value

    # ---------- 生命周期 ----------
    def start(self, url):
        form = None
        for _ in range(60):
            form = getattr(self.mw, 'native', None)
            if form is not None:
                break
            time.sleep(0.2)
        if form is None:
            raise Exception('主窗口原生句柄不可用')
        self.form = form
        self.page = PageShim(self)
        self._ui(self._create_ctrl)
        if not self._ready.wait(30):
            raise Exception('内嵌浏览器初始化超时')
        self.alive = True
        self.log('[browser] 内嵌浏览器面板已创建 (主窗口内)')
        self.goto(url)
        return self.page

    def _create_ctrl(self):
        import System.Windows.Forms as WinForms
        from System.Drawing import Rectangle  # noqa: F401
        from Microsoft.Web.WebView2.WinForms import (WebView2,
                                                      CoreWebView2CreationProperties)
        dock_none = getattr(WinForms.DockStyle, 'None')
        # 同表单第二个 WebView2 控件不能与主窗口共用 UserDataFolder
        # (0x8007139F 实测), 用独立子目录 (自身持久化, 登录态保留)
        embed_dir = os.path.join(self.storage_path, 'embed')
        os.makedirs(embed_dir, exist_ok=True)
        form = self.form
        self.ui_view = form.browser.webview
        ctrl = WebView2()
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = embed_dir
        props.set_IsInPrivateModeEnabled(False)
        props.AdditionalBrowserArguments = '--disable-features=ElasticOverscroll'
        ctrl.CreationProperties = props
        ctrl.Dock = dock_none
        form.Controls.Add(ctrl)
        self.ctrl = ctrl
        ctrl.set_Visible(self.initial_visible)
        self.ui_view.Dock = dock_none
        ctrl.CoreWebView2InitializationCompleted += self._on_ready
        form.Resize += self._on_resize
        self._layout()
        ctrl.BringToFront()
        ctrl.EnsureCoreWebView2Async(None)
        return None

    def _on_ready(self, sender, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
        if not args.IsSuccess:
            self.log(f'[browser] 内嵌浏览器初始化失败: {args.InitializationException}')
            return
        core = self.ctrl.CoreWebView2
        self._core = core
        # 注册任务本身是异步的，不能假设第一次 Navigate 前已经完成；
        # NavigationCompleted 还会对当前文档显式补注入，覆盖首次导航竞态。
        core.AddScriptToExecuteOnDocumentCreatedAsync(
            f'({INSTALL_JS})();({OVERLAY_JS})();')
        core.NewWindowRequested += self._on_new_window
        core.NavigationCompleted += self._on_nav_completed
        try:
            core.AddWebResourceRequestedFilter(
                '*', CoreWebView2WebResourceContext.All)
            core.WebResourceRequested += self._on_req
            core.WebResourceResponseReceived += self._on_res
        except Exception as e:
            self.log(f'[browser] 抓包事件挂载失败: {e}')
        self._ready.set()

    def _on_new_window(self, sender, args):
        """target=_blank / window.open (如授权成功页) → 同窗口内导航"""
        try:
            args.set_Handled(True)
            self._core.Navigate(str(args.get_Uri()))
        except Exception:
            pass

    def _on_nav_completed(self, sender, args):
        # document-created 注册可能晚于第一次导航；同时页面也可能主动替换
        # documentElement。每次导航完成后在当前文档补注入，并恢复最近状态。
        try:
            with self._overlay_lock:
                payload = self._overlay_payload
            self.ctrl.ExecuteScriptAsync(
                self._bootstrap_document_script(payload))
        except Exception as e:
            self.log(f'[browser] 当前页面浮层补注入失败: {e}')
        self._loaded.set()

    def _on_resize(self, sender, args):
        try:
            self._layout()
        except Exception:
            pass

    def _layout(self):
        from System.Drawing import Rectangle
        w = self.form.ClientSize.Width
        h = self.form.ClientSize.Height
        # 与 ui/style.css 的 216px 侧栏、22px 工作区边距和 58px 工具栏对齐。
        # 主 UI 始终铺满窗口，原生 WebView2 只覆盖浏览器画布，不再横向拼接。
        left, top, right, bottom = 242, 90, 26, 24
        self.ui_view.Bounds = Rectangle(0, 0, w, h)
        self.ctrl.Bounds = Rectangle(
            left, top, max(320, w - left - right),
            max(240, h - top - bottom))

    def set_visible(self, visible):
        """仅在「浏览器」页显示原生控件，切换其它页面时隐藏。"""
        if not self.ctrl:
            return
        try:
            self._ui(lambda: self.ctrl.set_Visible(bool(visible)))
        except Exception:
            pass

    def update_overlay(self, progress, rows, net_count=None):
        """把 Python 真实流程状态和已捕获请求推送到页内隔离浮层。"""
        payload = {
            'progress': dict(progress or {}),
            'rows': list(rows or []),
            'net_count': len(rows or []) if net_count is None else net_count,
        }
        with self._overlay_lock:
            self._overlay_payload = payload
        if not self.ctrl or not self.alive:
            return
        script = ('window.__cxPanel ? window.__cxPanel.update(%s) : false'
                  % json.dumps(payload, ensure_ascii=True))
        status, result = self._exec_raw(script, 2)
        if status == 'ok' and result is True:
            return
        # 浮层缺失时立即自愈；不依赖下一次页面导航或下一阶段进度。
        status, result = self._exec_raw(
            self._bootstrap_document_script(payload), 3)
        if status != 'ok' or result is not True:
            self.log(f'[browser] 浮层恢复失败: {status} {result}')

    def _bootstrap_document_script(self, payload=None):
        """生成当前文档幂等补注入脚本，并可原子恢复最近一次面板状态。"""
        update = 'return !!window.__cxPanel;'
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=True)
            update = (
                'return window.__cxPanel ? '
                f'window.__cxPanel.update({data}) : false;')
        return (
            '(() => { try {'
            f'({INSTALL_JS})();'
            f'({OVERLAY_JS})();'
            f'{update}'
            '} catch (e) { return "error:" + '
            'String((e && e.message) || e); } })()')

    # ---------- 原生抓包事件 ----------
    def _on_req(self, sender, args):
        with self._lock:
            self._net.append({'src': 'net', 'ts': int(time.time() * 1000),
                              'method': str(args.Request.Method),
                              'url': str(args.Request.Uri), 'status': None})
            if len(self._net) > 1000:
                self._net = self._net[-1000:]

    def _on_res(self, sender, args):
        url = str(args.Request.Uri)
        status = int(args.Response.StatusCode)
        with self._lock:
            for rec in reversed(self._net):
                if rec['url'] == url and rec['status'] is None:
                    rec['status'] = status
                    break

    def drain_net(self):
        with self._lock:
            out, self._net = self._net, []
        js = self.eval_js('window.__cx ? window.__cx.netlog.splice(0) : []')
        if isinstance(js, list):
            out.extend(js)
        return out

    # ---------- 导航 ----------
    def goto(self, url, timeout=20):
        self._loaded.clear()
        try:
            self._ui(lambda: self._core.Navigate(url))
        except Exception as e:
            self.log(f'[browser] 导航失败: {e}')
            return
        if not self._loaded.wait(timeout):
            self.log(f'[browser] 导航超时 ({timeout}s): {url}')
        time.sleep(0.8)

    def reload(self, timeout=20):
        """使用 WebView2 原生刷新并等待导航完成，避免旧文档结果槽被销毁。"""
        if not self._core:
            return False
        self._loaded.clear()
        try:
            self._ui(lambda: self._core.Reload())
        except Exception as e:
            self.log(f'[browser] 刷新失败: {e}')
            return False
        if not self._loaded.wait(timeout):
            self.log(f'[browser] 刷新超时 ({timeout}s)')
            return False
        return True

    def current_url(self):
        try:
            return self._ui(lambda: str(self._core.Source))
        except Exception:
            return ''

    def bring_to_front(self):
        try:
            self._ui(lambda: (self.form.Activate(), None)[1])
        except Exception:
            pass

    def destroy(self):
        """恢复主窗口单栏布局并移除面板"""
        if not self.ctrl:
            return
        try:
            def restore():
                import System.Windows.Forms as WinForms
                try:
                    self.form.Controls.Remove(self.ctrl)
                    self.ctrl.Dispose()
                except Exception:
                    pass
                try:
                    self.ui_view.Dock = WinForms.DockStyle.Fill
                    self.form.Resize -= self._on_resize
                except Exception:
                    pass
                return None
            self._ui(restore)
        except Exception:
            pass
        self.alive = False
