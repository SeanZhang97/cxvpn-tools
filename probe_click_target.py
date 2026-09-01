# -*- coding: utf-8 -*-
"""probe_click_target.py - 定位 text=获取验证码 匹配元素与点击落点

事实: cx.act click 后按钮上的监听 clicklog=0, 点击未到达按钮。
本探针打印: #eject 初始存在性/显隐; text=获取验证码 匹配到的元素
(tag/id/是否按钮/是否detached); document 级 capture 点击日志(落点 target)。
"""
import json
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfgmod, web_flow  # noqa: E402
from core.browser_win import INSTALL_JS  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')

FAKE_PHONE = '13000000000'


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

            print('[eject] 初始状态 =', ev("""() => {
                const e = document.getElementById('eject');
                if (!e) return 'absent';
                const cs = getComputedStyle(e);
                const r = e.getBoundingClientRect();
                return {disp: cs.display, vis: cs.visibility,
                        w: r.width, h: r.height,
                        cls: String(e.className).slice(0, 80)};
            }"""))

            # document 级 capture 点击日志
            ev("""() => {
                window.__clicks = [];
                document.addEventListener('click', (e) => {
                    const t = e.target;
                    window.__clicks.push({
                        tag: t && t.tagName, id: t && t.id,
                        cls: t && String(t.className).slice(0, 40),
                        attached: !!(t && t.isConnected)});
                }, true);
            }""")

            btn = ev('(s) => window.__cx.q(s, false, null)', 'text=获取验证码')
            print('[match] text=获取验证码 -> id =', btn)
            print('[match] 匹配元素 =', ev("""(i) => {
                const el = window.__cx.els[i];
                if (!el) return 'undefined!';
                const r = el.getBoundingClientRect();
                return {tag: el.tagName, id: el.id,
                        cls: String(el.className).slice(0, 60),
                        txt: (el.textContent || '').trim().slice(0, 20),
                        attached: el.isConnected,
                        rects: el.getClientRects().length, w: r.width};
            }""", btn))
            print('[btn] 真实按钮 vis =', ev("""() => {
                const b = document.getElementById('vpnSmsCodeBtn');
                if (!b) return 'absent';
                return {rects: b.getClientRects().length,
                        disp: getComputedStyle(b).display,
                        vis: getComputedStyle(b).visibility,
                        disabled: b.disabled};
            }"""))

            ev('(a) => window.__cx.act(a[0], a[1], a[2])', [btn, 'click', None])
            time.sleep(0.8)
            print('[click] document capture 日志 =',
                  ev('() => window.__clicks'))
            print('[click] 按钮 clicklog 监听 =', ev("""() => {
                return typeof window.__clicklog;
            }"""))
            print('[eject] 点击后 =', ev("""() => {
                const e = document.getElementById('eject');
                if (!e) return 'absent';
                const r = e.getBoundingClientRect();
                return {w: r.width, h: r.height,
                        disp: getComputedStyle(e).display};
            }"""))
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE,
                                       'browser_data_embedclick'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
