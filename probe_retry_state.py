# -*- coding: utf-8 -*-
"""probe_retry_state.py - 主动触发「失败过多，点此重试」态并验证复位

假手机号开弹窗后连续点错误位置耗站点失败次数, 等「点此重试」横幅
出现; 调 captcha_handler._click_retry_if_blocked 验证: 能检测、能点击、
点击后横幅消失且能取到新 sprite。
"""
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import captcha_handler, config as cfgmod, web_flow  # noqa: E402
from core.browser_win import INSTALL_JS  # noqa: E402
from probe_embed_click import PWShim  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')

FAKE_PHONE = '13000000000'
WRONG = [(8, 8), (16, 16), (24, 8)]


def banner_visible(page):
    return page.evaluate(
        '() => { const els = [...document.querySelectorAll("*")];'
        ' return els.some(e => e.getClientRects().length'
        ' && (e.textContent || "").includes("点此重试")); }')


def main():
    cfg = cfgmod.load()
    cfg['phone'] = FAKE_PHONE
    cfg['browser_data_dir'] = './browser_data_retrystate'
    with sync_playwright() as pw:
        ctx = web_flow.start_browser(pw, cfg)
        ctx.add_init_script(f'({INSTALL_JS})()')
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(web_flow.URL, wait_until='domcontentloaded')
            time.sleep(2)
            inp_id = page.evaluate(
                '(s) => window.__cx.q(s, false, null)',
                'input[placeholder*="手机"]')
            page.evaluate('(a) => window.__cx.act(a[0], a[1], a[2])',
                          [inp_id, 'fill', FAKE_PHONE])
            btn_id = page.evaluate('(s) => window.__cx.q(s, false, null)',
                                   'text=获取验证码')
            page.evaluate('(a) => window.__cx.act(a[0], a[1], a[2])',
                          [btn_id, 'click', None])
            try:
                page.wait_for_selector('#eject', timeout=8000)
            except Exception:
                print('弹窗未出现')
                return
            print('[setup] 弹窗已出现, 开始连点错误位置触发失败态')

            shim = PWShim(page)
            blocked = False
            for rnd in range(6):
                for i, (x, y) in enumerate(WRONG, 1):
                    # 与 app 同路径: cx.clickAt dispatchEvent, 无 actionability
                    # (Playwright 真鼠标点击会被站点标记 div 拦截)
                    page.evaluate(
                        '(a) => window.__cx.clickAt(a[0], a[1], a[2], a[3])',
                        ['#cx_imgBg', x, y, i])
                    time.sleep(0.4)
                time.sleep(1.5)
                if banner_visible(page):
                    blocked = True
                    print(f'[round {rnd}] 「点此重试」横幅出现')
                    break
                print(f'[round {rnd}] 横幅未出现, 继续')
            if not blocked:
                print('6 轮错误点击仍未触发失败态 (站点阈值更高), 结束')
                return

            old = captcha_handler.get_sprite_bytes(shim)
            ok = captcha_handler._click_retry_if_blocked(shim, log=print)
            print(f'[retry] _click_retry_if_blocked = {ok}')
            time.sleep(1)
            gone = not banner_visible(page)
            new = captcha_handler.get_sprite_bytes(shim)
            print(f'[retry] 横幅消失={gone}, 新sprite={bool(new)}, '
                  f'与旧图不同={new != old if (new and old) else "n/a"}')
            page.screenshot(path='captcha_cache/retrystate_after.png')
            print('截图 captcha_cache/retrystate_after.png')
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE,
                                       'browser_data_retrystate'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
