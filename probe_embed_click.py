# -*- coding: utf-8 -*-
"""probe_embed_click.py - 假手机号复现内嵌面板点击机制并走通图形验证码

与内嵌浏览器同机制: add_init_script 注入 browser_win.INSTALL_JS (等价
WebView2 document-created 钩子), 全程只走 cx.q/cx.act 路径 (PageShim 同款
表达式, 注意 cx.q 返回注册表 id, 0 为合法值, 判定用 is not None)。步骤:
1. 诊断 #vpnSmsCodeBtn 的 disabled/class 与 captchaIns 就绪态 (读 DOM);
2. 按钮挂点击监听计数, 对比 现实现 el.click() 与 dispatchEvent 合成派发;
3. 弹窗出现后走 captcha_handler.handle_captcha 完成图标点选;
4. 排空 INSTALL_JS 抓包钩子 (cx.netlog) 打印 getSmsCode 请求作通过证据。
"""
import json
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import captcha_handler, config as cfgmod, web_flow  # noqa: E402
from core.browser_win import INSTALL_JS  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')

FAKE_PHONE = '13000000000'


class _El:
    """cx 注册表句柄, 与 browser_win.El 同语义"""

    def __init__(self, p, eid):
        self._p = p
        self._eid = eid

    def _act(self, a, v=None):
        return self._p.evaluate('(x) => window.__cx.act(x[0], x[1], x[2])',
                                [self._eid, a, v])

    def click(self):
        return self._act('click')

    def fill(self, v):
        return self._act('fill', v)

    def is_visible(self):
        return bool(self._act('visible'))

    def is_enabled(self):
        return bool(self._act('enabled'))

    def text_content(self):
        return self._act('text') or ''


class PWShim:
    """Playwright 传输 + 页内 __cx 引擎: 与内嵌 PageShim 完全同语义
    (选择器/元素点击全走 cx, 避免 Playwright actionability 与选择器
    语义差异导致探针结论失真)"""

    def __init__(self, p):
        self._p = p

    def evaluate(self, fn, arg=None):
        return self._p.evaluate(fn, arg)

    def eval_on_selector(self, sel, fn):
        return self._p.eval_on_selector(sel, fn)

    def query_selector(self, sel):
        eid = self._p.evaluate('(s) => window.__cx ? '
                               'window.__cx.q(s, false, null) : null', sel)
        return _El(self._p, eid) if eid is not None else None

    def query_selector_all(self, sel):
        ids = self._p.evaluate('(s) => window.__cx ? '
                               'window.__cx.q(s, true, null) : []', sel) or []
        return [_El(self._p, i) for i in ids]

    def click(self, sel, position=None, timeout=5000, mark=None):
        if position is not None:
            return self._p.evaluate(
                '(a) => window.__cx.clickAt(a[0], a[1], a[2], a[3])',
                [sel, position['x'], position['y'], mark])
        el = self.query_selector(sel)
        if el is None:
            raise Exception(f'click 目标不存在: {sel}')
        return el.click()

    def wait_for_selector(self, sel, timeout=8, **_kw):
        dl = time.time() + timeout
        while time.time() < dl:
            if self.query_selector(sel) is not None:
                return True
            time.sleep(0.4)
        return False

    def reload(self):
        return self._p.reload()

    @property
    def url(self):
        return self._p.url

    def bring_to_front(self):
        return self._p.bring_to_front()


def popup_within(page, sec):
    try:
        page.wait_for_selector('#eject', timeout=sec * 1000)
        return True
    except Exception:
        return False


def main():
    cfg = cfgmod.load()
    cfg['phone'] = FAKE_PHONE
    cfg['browser_data_dir'] = './browser_data_embedclick'
    with sync_playwright() as pw:
        ctx = web_flow.start_browser(pw, cfg)
        ctx.add_init_script(f'({INSTALL_JS})()')
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(web_flow.URL, wait_until='domcontentloaded')
            time.sleep(2)

            def ev(src, arg=None):
                args = '' if arg is None else json.dumps(arg)
                return page.evaluate(f'({src})({args})')

            def q(sel):
                eid = ev('(s) => window.__cx ? '
                         'window.__cx.q(s, false, null) : null', sel)
                return eid if eid is not None else None

            def act(i, a, v=None):
                return ev('(a) => window.__cx.act(a[0], a[1], a[2])',
                          [i, a, v])

            diag = ev("""() => {
                const b = document.getElementById('vpnSmsCodeBtn');
                return {has_btn: !!b, disabled: b ? b.disabled : null,
                        cls: b ? b.className : null,
                        captchaIns: typeof window.captchaIns !== 'undefined'
                            ? !!window.captchaIns : 'undef'};
            }""")
            print('[diag] t+2s 按钮/SDK 状态:', diag)

            inp = q('input[placeholder*="手机"]')
            if inp is None:
                inp = q('input[type="tel"]')
            if inp is None:
                print('未找到手机号输入框')
                return
            act(inp, 'fill', FAKE_PHONE)
            print('[fill] 输入框值 =', ev(
                '() => document.getElementById("phoneNumber") && '
                'document.getElementById("phoneNumber").value'))

            # 等 captchaIns 就绪 (与 web_flow 修复同款)
            t0 = time.time()
            ready = False
            while time.time() - t0 < 15:
                ready = bool(ev('() => typeof window.captchaIns !== '
                                '"undefined" && !!window.captchaIns'))
                if ready:
                    break
                time.sleep(0.5)
            print(f'[diag] captchaIns ready={ready} '
                  f'({time.time() - t0:.1f}s)')

            # 点击事件观测: 记录 click 是否到达按钮/layui 提示是否出现
            ev("""() => {
                window.__clicklog = 0;
                const b = document.getElementById('vpnSmsCodeBtn');
                if (b) b.addEventListener('click',
                    () => { window.__clicklog++; });
            }""")

            btn = q('text=获取验证码')
            if btn is None:
                btn = q('button:has-text("登录")')
            if btn is None:
                print('未找到获取验证码按钮')
                return
            act(btn, 'click')
            time.sleep(0.5)
            print('[click] 方式A el.click(): clicklog =',
                  ev('() => window.__clicklog'), '弹窗 =',
                  popup_within(page, 5))
            print('[click] layui 提示 =', ev(
                '() => { const m = document.querySelector('
                '".layui-layer-msg"); return m ? m.textContent : null; }'))

            if not ev('() => !!document.getElementById("eject")'):
                # 方式 B: dispatchEvent 合成派发 (拟议修复)
                ev("""() => {
                    const el = document.getElementById('vpnSmsCodeBtn');
                    if (el) el.dispatchEvent(new MouseEvent('click',
                        {bubbles: true, cancelable: true, view: window}));
                }""")
                pb = popup_within(page, 5)
                print('[click] 方式B dispatchEvent: clicklog =',
                      ev('() => window.__clicklog'), '弹窗 =', pb)

            if not ev('() => !!document.getElementById("eject")'):
                page.screenshot(path='captcha_cache/embedclick_fail.png')
                print('两方式均无弹窗, 截图 captcha_cache/embedclick_fail.png')
                return

            # 走通图形验证码 (经 PWShim 适配 mark 参数)
            ok = captcha_handler.handle_captcha(PWShim(page), cfg, log=print,
                                                manual_provider=None)
            print(f'[captcha] 通过={ok}')
            time.sleep(1.5)
            net = ev('() => window.__cx ? window.__cx.netlog.splice(0) : []')
            for r in net or []:
                u = str(r.get('url', ''))
                if 'SmsCode' in u or 'checkCode' in u or 'captcha' in u:
                    print(f'[net] {r.get("method")} {r.get("status")} {u}')
            page.screenshot(path='captcha_cache/embedclick_after.png')
            print('截图 captcha_cache/embedclick_after.png')
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE,
                                       'browser_data_embedclick'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
