# -*- coding: utf-8 -*-
"""
web_flow.py - 网页流: 登录 -> 验证码 -> 短信码 -> 授权/续期

短信验证码走页面点击事件 (填入输入框 -> 点击登录 -> 等待页面跳转),
不再直接调 checkCode API: API 不触发页面跳转/会话建立, 导致后续步骤
失败。续期/授权仍优先走同源接口, 接口不可用时回退 DOM 点击。
"""
import json
import time
import urllib.parse

try:
    from . import captcha_handler, sms_receiver
except ImportError:  # 直接运行旧目录时兼容
    import captcha_handler
    import sms_receiver

URL = 'https://remote.chaoxing.com/vpn'


class RenewCancelled(Exception):
    """用户主动中断当前授权/续期流程。"""


def _checkpoint(cancel):
    if cancel and cancel():
        raise RenewCancelled()


def _sleep(seconds, cancel=None):
    """可中断等待，避免长轮询让“中断授权处理”迟迟不生效。"""
    deadline = time.time() + max(0, seconds)
    while True:
        _checkpoint(cancel)
        left = deadline - time.time()
        if left <= 0:
            return
        time.sleep(min(0.1, left))


def start_browser(playwright, cfg):
    """仅供 probe 脚本使用的 Playwright 持久化上下文 (主流程已改用内嵌浏览器)"""
    import os
    from . import config as _cfg
    data_dir = cfg.get('browser_data_dir', './browser_data')
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(_cfg.BASE, data_dir)
    common = dict(user_data_dir=data_dir, headless=True,
                  viewport={'width': 1280, 'height': 800}, locale='zh-CN')
    try:
        return playwright.chromium.launch_persistent_context(
            channel='msedge', **common)
    except Exception:
        return playwright.chromium.launch_persistent_context(**common)


def _wait_popup(page, timeout=8, cancel=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _checkpoint(cancel)
        if page.query_selector('#eject') is not None:
            return True
        _sleep(0.25, cancel)
    return False


def _api_post(page, path, body, log=None):
    """同源接口调用 (页内 fetch, cookie 自动携带), 返回解析后 JSON 或 None

    完整记录地址/参数/返回结果到运行日志 (参数仅业务字段, 无敏感值)。
    """
    url = 'https://remote.chaoxing.com' + path
    if log:
        log(f'[api] POST {url} 参数 {json.dumps(body, ensure_ascii=False)}')
    try:
        res = page.evaluate("""async (a) => {
            try {
                const r = await fetch(a.path, {method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Content-Type':
                              'application/x-www-form-urlencoded; charset=UTF-8',
                              'X-Requested-With': 'XMLHttpRequest'},
                    body: a.body});
                return await r.json();
            } catch (e) { return {code: 0, msg: String(e)}; }
        }""", {'path': path, 'body': urllib.parse.urlencode(body)})
    except Exception as e:
        if log:
            log(f'[api] {url} 调用异常: {e}')
        return None
    if not isinstance(res, dict):
        if log:
            log(f'[api] {url} 返回非 JSON: {res!r}')
        return None
    if log:
        log(f'[api] {url} 返回 {json.dumps(res, ensure_ascii=False)}')
    return res


def _response_expiries(response):
    """提取授权接口返回的 VPN 到期时间，忽略其它业务字段。"""
    if not isinstance(response, dict):
        return {}
    data = response.get('data')
    if not isinstance(data, dict):
        return {}
    values = data.get('expTime')
    if not isinstance(values, dict):
        return {}
    return {
        str(vpn_id): str(expires_at).strip()
        for vpn_id, expires_at in values.items()
        if str(vpn_id).strip() and str(expires_at or '').strip()
    }


def _list_ready(page):
    """VPN 列表弹框/行是否已就绪 (行复选框/行 div/批量按钮/弹框标题)"""
    return bool(page.evaluate("""() => !!(
        document.querySelector('input[name="batchVpnId"]') ||
        document.querySelector('#vpnInfoList .layui-form-item') ||
        document.querySelector('#batchLoginVpn') ||
        [...document.querySelectorAll('.layui-layer-title')]
            .some(t => (t.textContent || '').includes('选择 VPN'))
    )"""))


def _read_vpn_rows(page):
    """已登录时从页面读 VPN 行 (id + 名称 + 状态 + 是否待授权)

    站点列表非表格: vpnLogin.js 用 layui div 行渲染
    (#vpnInfoList .layui-form-item, 复选框 value=id, 状态 label id^=status),
    旧表格结构仅作兼容兜底。"""
    return page.evaluate(r"""() => {
        const out = [];
        const push = (id, name, status) => out.push({
            id, name, status,
            need_auth: /未授权|未激活/.test(status)});
        const items = document.querySelectorAll(
            '#vpnInfoList .layui-form-item');
        if (items.length) {
            items.forEach(item => {
                const inp = item.querySelector(
                    'input[name="batchVpnId"], ' +
                    'input[type="hidden"][value]');
                const v = inp ? (inp.value || '').trim() : '';
                if (!/^\d+$/.test(v)) return;
                const nameEl = item.querySelector('label:not([id])');
                const stEl = item.querySelector('label[id^="status"]');
                push(+v,
                     nameEl ? (nameEl.textContent || '').trim() : '',
                     stEl ? (stEl.textContent || '').trim() : '');
            });
            return out;
        }
        document.querySelectorAll('input[type="checkbox"][value]')
            .forEach(cb => {
                const v = (cb.value || '').trim();
                if (!/^\d+$/.test(v)) return;
                const row = cb.closest('tr');
                const txt = row
                    ? (row.textContent || '').replace(/\s+/g, ' ').trim()
                    : '';
                push(+v, txt, txt);
            });
        return out;
    }""") or []


def _sync_authorization_result(page, auth_ids, renew_ids):
    """接口成功后就地同步远端列表，避免旧 DOM 继续显示“未授权”。"""
    return page.evaluate(r"""(payload) => {
        const auth = new Set((payload.auth_ids || []).map(String));
        const renew = new Set((payload.renew_ids || []).map(String));
        const items = document.querySelectorAll(
            '#vpnInfoList .layui-form-item');
        let updated = 0;
        const seen = new Set();
        const apply = (container, id) => {
            const kind = auth.has(id) ? 'auth' : (renew.has(id) ? 'renew' : '');
            if (!kind || seen.has(id)) return;
            seen.add(id);
            const labels = [...container.querySelectorAll('label, td, span')];
            const status = container.querySelector('label[id^="status"]') ||
                labels.find(node => /未授权|未激活|已授权|到期|过期/.test(
                    (node.textContent || '').trim()));
            if (status) {
                status.textContent = kind === 'auth' ? '已授权（刚刚）' : '已续期（刚刚）';
                status.style.color = '#059669';
                status.style.fontWeight = '600';
            }
            const action = container.querySelector('div[id^="loginBtnDiv"]') || container;
            [...action.querySelectorAll('button')]
                .filter(button => /授权|激活|续期/.test(button.textContent || ''))
                .forEach(button => {
                    button.disabled = true;
                    button.textContent = kind === 'auth' ? '已授权' : '续期完成';
                    button.style.opacity = '.72';
                    button.style.cursor = 'default';
                });
            updated += 1;
        };
        items.forEach(item => {
            const inp = item.querySelector(
                'input[name="batchVpnId"], input[type="hidden"][value]');
            const id = inp ? String(inp.value || '').trim() : '';
            apply(item, id);
        });
        document.querySelectorAll('input[type="checkbox"][value]').forEach(input => {
            const row = input.closest('tr');
            if (row) apply(row, String(input.value || '').trim());
        });
        if (document.body) {
            let banner = document.querySelector('#cx-authorization-success');
            if (!banner) {
                banner = document.createElement('div');
                banner.id = 'cx-authorization-success';
                banner.setAttribute('role', 'status');
                banner.setAttribute('aria-live', 'polite');
                banner.style.cssText = [
                    'position:fixed', 'top:18px', 'left:50%',
                    'z-index:2147483646', 'display:flex',
                    'align-items:center', 'gap:8px', 'max-width:calc(100vw - 40px)',
                    'padding:9px 14px', 'border:1px solid rgba(255,255,255,.12)',
                    'border-radius:999px', 'box-sizing:border-box',
                    'color:#f8fafc', 'background:rgba(17,24,39,.94)',
                    'box-shadow:0 10px 28px rgba(15,23,42,.24)',
                    'font:500 12px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
                    'white-space:nowrap', 'overflow:hidden', 'text-overflow:ellipsis',
                    'opacity:0', 'transform:translate(-50%,-8px)',
                    'transition:opacity .2s ease,transform .2s ease',
                    'pointer-events:none'
                ].join(';');
                const dot = document.createElement('span');
                dot.style.cssText = [
                    'width:7px', 'height:7px', 'border-radius:50%',
                    'background:#34d399', 'box-shadow:0 0 0 3px rgba(52,211,153,.16)',
                    'flex:0 0 auto'
                ].join(';');
                const text = document.createElement('span');
                text.className = 'cx-authorization-success-text';
                text.style.cssText = 'overflow:hidden;text-overflow:ellipsis';
                banner.append(dot, text);
                document.body.appendChild(banner);
            }
            const text = banner.querySelector('.cx-authorization-success-text');
            if (text) text.textContent = '已同步 ' + updated +
                ' 个 VPN 状态，可返回管理器连接';
            clearTimeout(banner._cxHideTimer);
            clearTimeout(banner._cxRemoveTimer);
            requestAnimationFrame(() => {
                banner.style.opacity = '1';
                banner.style.transform = 'translate(-50%,0)';
            });
            banner._cxHideTimer = setTimeout(() => {
                banner.style.opacity = '0';
                banner.style.transform = 'translate(-50%,-8px)';
                banner._cxRemoveTimer = setTimeout(() => banner.remove(), 220);
            }, 3600);
        }
        return updated;
    }""", {'auth_ids': auth_ids, 'renew_ids': renew_ids}) or 0


def ensure_authorized(page, cfg, log=print, manual=None,
                     sms_poll=None, sms_show=None, progress=None, cancel=None):
    """确保页面所有 VPN 行完成授权/续期 (与配置目标无关), 成功返回 True"""
    def report(step, title, detail='', status='running'):
        if progress:
            progress(step, title, detail, status)

    _checkpoint(cancel)
    report(2, '正在打开授权页', '正在加载 remote.chaoxing.com')
    log('[web] 导航到 remote.chaoxing.com …')
    page.goto(URL, wait_until='domcontentloaded')
    _sleep(2, cancel)
    log('[web] 页面已加载')

    phone = cfg['phone']
    vpn_list = None
    # 已登录且直接出现 VPN 列表 -> 跳过登录
    if not _list_ready(page):
        report(3, '等待用户完成登录授权', '请完成登录与短信验证')
        log('[web] 未登录, 进入登录流程')
        phone_inp = page.query_selector('input[placeholder*="手机"]') \
            or page.query_selector('input[type="tel"]')
        if phone_inp is None:
            log('[web] 未找到手机号输入框, 页面结构可能变化')
            page.bring_to_front()
            return False
        phone_inp.fill(phone)
        log(f'[web] 已填入手机号 {phone}')
        # 点击前就启动后台捕获: 弹窗/校验期间到达的短信也不会丢
        catcher = sms_receiver.Catcher(settings=cfg.get('sms'), log=log)
        code, src, entered = None, None, False
        try:
            if not catcher.wait_ready(timeout=12):
                log(f'[sms] {catcher.source_label}初始化超时，后台继续重试；'
                    '本次也可手动输入验证码')
            # 冷启动时站点 layui/验证码 SDK 异步加载: 点击过早则按钮点击
            # 绑定未完成或验证码实例未就绪 (captchaIns && popUp() 静默
            # 空操作), 弹窗永不出现。先等就绪, 再以"弹窗出现"为准重试点击。
            t0 = time.time()
            while time.time() - t0 < 15:
                _checkpoint(cancel)
                if page.evaluate('() => typeof window.captchaIns !== '
                                 '"undefined" && !!window.captchaIns'):
                    log('[web] 验证码SDK已就绪')
                    break
                _sleep(0.5, cancel)
            else:
                log('[web] 验证码SDK就绪等待超时 '
                    '(站点脚本加载慢或失败), 仍尝试点击')
            log('[web] 点击获取验证码, 短信后台捕获已启动')
            popup = False
            for attempt in range(3):
                _checkpoint(cancel)
                btn = page.query_selector('text=获取验证码') \
                    or page.query_selector('button:has-text("登录")')
                if btn is None:
                    log('[web] 未找到获取验证码按钮, 页面结构可能变化')
                    return False
                btn.click()
                popup = bool(_wait_popup(page, timeout=5, cancel=cancel))
                if popup:
                    break
                log(f'[web] 第{attempt + 1}次点击后弹窗未出现 '
                    '(站点JS可能未就绪), 重试点击')
                _sleep(1, cancel)
            if popup:
                log('[web] 检测到图标点选验证码弹窗 '
                    '(须先通过图标验证码, 站点才会发送短信)')
                if captcha_handler.vlm_configured(cfg):
                    report(3, '正在识别图形验证码',
                           'AI 识别失败时将自动转为人工点击')
                else:
                    report(3, '等待人工完成图形验证',
                           '未配置完整 AI 模型，已跳过自动识别')
                if not captcha_handler.handle_captcha(
                        page, cfg, log=log, manual_provider=manual,
                        cancel=cancel):
                    _checkpoint(cancel)
                    # 误判兜底: 通过后弹窗可能切到短信步骤而仍在,
                    # "弹窗仍在" 不再权威, 以"已收到短信"为通过信号
                    r = None
                    deadline0 = time.time() + 8
                    while time.time() < deadline0 and not r:
                        _checkpoint(cancel)
                        r = catcher.poll()
                        _sleep(0.5, cancel)
                    if r:
                        code, src = r[0], catcher.source_label
                        log('[web] 验证码判定未通过但已捕获短信 '
                            '(站点实际已通过图标验证), 继续')
                    else:
                        return False
            # 等短信码: 自动捕获与手动输入并行, 谁先取到用哪个
            if sms_show:
                sms_show(True)
            log('[web] 等待短信验证码… (自动捕获中; 也可直接在浏览器'
                '页面或弹窗手动输入, 谁先取到用哪个)')
            deadline = time.time() + cfg.get('sms_timeout', 120)
            while time.time() < deadline:
                _checkpoint(cancel)
                if _list_ready(page):
                    entered = True
                    log('[web] 检测到已进入续期页 '
                        '(浏览器内手动完成登录), 跳过短信步骤')
                    break
                if code is not None:
                    break
                r = catcher.poll()
                if r:
                    code, body = r
                    src = catcher.source_label
                    break
                if sms_poll:
                    mc = sms_poll()
                    if mc:
                        code, src = mc, '手动输入'
                        break
                _sleep(0.5, cancel)
        finally:
            catcher.stop()
            if sms_show:
                sms_show(False)
        if not entered:
            if not code:
                log(f'[web] 等待短信验证码超时 ({catcher.source_label}未捕获且'
                    '未手动输入；请检查“自动化配置”中的接收方式，或下次直接在'
                    '浏览器页面/弹窗输入)')
                return False
            log(f'[web] 取到短信验证码 ({src}): {code}, 填入页面输入框')
            # 走页面点击事件 (不直接调 checkCode API): 只有点击登录
            # 才会触发页面跳转和会话建立, 直接调 API 不触发跳转
            code_inp = page.query_selector(
                'input[placeholder*="验证码"]') \
                or page.query_selector('input[placeholder*="码"]')
            if code_inp is None:
                # 兜底: 取最后一个可见文本输入框 (第一个是手机号)
                inps = page.query_selector_all(
                    'input[type="text"], input[type="tel"]')
                code_inp = inps[-1] if inps else None
                if code_inp:
                    log('[web] 验证码输入框按 placeholder 未命中, '
                        '按 input 顺序取最后一个')
            if code_inp is None:
                log('[web] 未找到验证码输入框, 页面结构可能变化')
                page.bring_to_front()
                return False
            code_inp.fill(code)
            log(f'[web] 验证码 {code} 已填入输入框')
            _sleep(0.5, cancel)
            login_btn = page.query_selector('button:has-text("登录")') \
                or page.query_selector('text=登录')
            if login_btn is None:
                log('[web] 未找到登录按钮, 页面结构可能变化')
                page.bring_to_front()
                return False
            login_btn.click()
            log('[web] 已点击登录按钮, 等待页面跳转…')
            # 等待页面跳转到 VPN 列表页 (列表弹框/行就绪)
            deadline_nav = time.time() + 15
            entered_nav = False
            while time.time() < deadline_nav:
                _checkpoint(cancel)
                if _list_ready(page):
                    entered_nav = True
                    break
                _sleep(1, cancel)
            if not entered_nav:
                log('[web] 登录后未跳转到 VPN 列表页 '
                    '(验证码错误/过期?)')
                return False
            log('[web] 登录成功, 已进入 VPN 列表页')
            vpn_list = _read_vpn_rows(page)
            log(f'[web] 从页面读取到 {len(vpn_list)} 个 VPN')
    else:
        report(3, '已检测到登录状态', '无需重复输入短信验证码')
        log('[web] 检测到已登录状态, 跳过登录')

    # ---------- 授权/续期: 页面有哪些行就批量处理哪些行 ----------
    # (与工具配置的目标 VPN 无关; 站点批量接口 vpnIdsSel 为逗号拼接 id)
    report(4, '正在验证授权状态', '正在读取 VPN 授权与续期信息')
    _checkpoint(cancel)
    if vpn_list is None:
        vpn_list = _read_vpn_rows(page)
    auth_ids = [v.get('id') for v in vpn_list if v.get('need_auth')]
    renew_ids = [v.get('id') for v in vpn_list if not v.get('need_auth')]
    names = ', '.join(v.get('name') or '?' for v in vpn_list)
    log(f'[web] 页面 {len(vpn_list)} 行 (授权 {len(auth_ids)} / '
        f'续期 {len(renew_ids)}): {names or "无"}')
    report(5, '正在完成授权/续期',
           f'待授权 {len(auth_ids)} 项，待续期 {len(renew_ids)} 项')
    api_ok = True
    expiries = {}
    if auth_ids:
        _checkpoint(cancel)
        r = _api_post(page, '/vpn/batchLoginVpn',
                      {'phoneNumber': phone,
                       'vpnIdsSel': ','.join(str(i) for i in auth_ids)},
                      log=log)
        if r and r.get('code') == 200:
            expiries.update(_response_expiries(r))
            log(f'[web] 批量授权 {len(auth_ids)} 行成功: {r.get("msg")}')
        else:
            api_ok = False
            log(f'[web] 批量授权失败: {(r or {}).get("msg")}, 回退页面点击')
    if renew_ids:
        _checkpoint(cancel)
        r = _api_post(page, '/vpn/batchReLoginVpn/addTime',
                      {'phoneNumber': phone,
                       'vpnIdsSel': ','.join(str(i) for i in renew_ids)},
                      log=log)
        if r and r.get('code') == 200:
            expiries.update(_response_expiries(r))
            log(f'[web] 批量续期 {len(renew_ids)} 行成功: {r.get("msg")}')
        else:
            api_ok = False
            log(f'[web] 批量续期失败: {(r or {}).get("msg")}, 回退页面点击')
    if api_ok and (auth_ids or renew_ids):
        updated = _sync_authorization_result(page, auth_ids, renew_ids)
        log(f'[web] 已将授权结果同步到远端列表 DOM ({updated} 行)，无需刷新')
        return {'ok': True, 'expiries': expiries}
    if not (auth_ids or renew_ids):
        log('[web] 页面无可用 VPN 行, 继续兜底检查')

    # ---------- 兜底: 页面点击 (逐行按状态点 授权/续期, 每行只处理一次,
    # 与配置目标无关; 用行 id 去重防页面重渲染后重复点击) ----------
    if not _list_ready(page):
        _checkpoint(cancel)
        sel = page.query_selector('text=选择 VPN')
        if sel:
            sel.click()
            _sleep(1, cancel)
    handled = set()
    clicked = 0
    deadline2 = time.time() + 90
    while time.time() < deadline2 and clicked < 30:
        _checkpoint(cancel)
        r = page.evaluate("""(a) => {
            const items = document.querySelectorAll(
                '#vpnInfoList .layui-form-item');
            for (const item of items) {
                const inp = item.querySelector(
                    'input[name="batchVpnId"], input[type="hidden"][value]');
                const id = inp ? (inp.value || '').trim() : '';
                if (id && a.handled.indexOf(id) >= 0) continue;
                const nameEl = item.querySelector('label:not([id])');
                const stEl = item.querySelector('label[id^="status"]');
                const st = stEl ? (stEl.textContent || '').trim() : '';
                const auth = /未授权|未激活/.test(st);
                const div = item.querySelector('div[id^="loginBtnDiv"]');
                const btns = div ? [...div.querySelectorAll('button')] : [];
                const find = t => btns.find(
                    b => (b.textContent || '').trim() === t);
                const b = auth ? (find('授权') || find('激活'))
                               : find('续期');
                if (!b || b.disabled) continue;
                b.click();
                return {id: id, st: st,
                        name: nameEl
                            ? (nameEl.textContent || '').trim() : '',
                        clicked: (b.textContent || '').trim()};
            }
            return null;
        }""", {'handled': list(handled)}) or None
        if not r:
            break
        clicked += 1
        if r.get('id'):
            handled.add(r['id'])
        log(f'[web] 已点击 {r.get("clicked")} (页面兜底, '
            f'行={r.get("name") or "?"}, 状态={r.get("st") or "无"})')
        _sleep(1.5, cancel)
    if not clicked:
        log('[web] 页面兜底未处理任何行 (页面兜底失败)')
        return False
    _sleep(2, cancel)
    # 授权/续期成功页在窗口内导航 (VPNLoginSuccess)
    for _ in range(10):
        _checkpoint(cancel)
        if 'VPNLoginSuccess' in (page.url or ''):
            log('[web] 已进入授权成功页, 流程完成')
            return {'ok': True, 'expiries': {}}
        _sleep(1, cancel)
    return {'ok': True, 'expiries': {}}
