# -*- coding: utf-8 -*-
"""probe_cx_dbg.py - 聚焦诊断: INSTALL_JS 注入后 cx.q 为何找不到输入框"""
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfgmod, web_flow  # noqa: E402
from core.browser_win import INSTALL_JS  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')

SEL = 'input[placeholder*="手机"]'


def main():
    cfg = cfgmod.load()
    cfg['phone'] = '13000000000'
    cfg['browser_data_dir'] = './browser_data_dbg'
    with sync_playwright() as pw:
        ctx = web_flow.start_browser(pw, cfg)
        ctx.add_init_script(f'({INSTALL_JS})()')
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(web_flow.URL, wait_until='domcontentloaded')
            time.sleep(2)
            print('cx installed =',
                  page.evaluate('() => typeof window.__cx'))
            print('q result =', page.evaluate(
                '(s) => window.__cx ? window.__cx.q(s, false, null) : null',
                SEL))
            print('raw css count =', page.evaluate(
                '() => document.querySelectorAll('
                '\'input[placeholder*="手机"]\').length'))
            print('vis check =', page.evaluate("""() => {
                const el = document.querySelector(
                    'input[placeholder*="手机"]');
                return !!el && !!(el.getClientRects
                    && el.getClientRects().length
                    && getComputedStyle(el).display !== 'none'
                    && getComputedStyle(el).visibility !== 'hidden');
            }"""))
            # 直接调 match 内部逻辑, 看 filter(vis) 前后数量
            print('match debug =', page.evaluate("""() => {
                const el = document.querySelector(
                    'input[placeholder*="手机"]');
                if (!el) return 'no el';
                const r = el.getClientRects();
                const cs = getComputedStyle(el);
                return {rects: r.length, disp: cs.display,
                        vis: cs.visibility};
            }"""))
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_dbg'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
