# -*- coding: utf-8 -*-
"""probe_clickdiag.py - 点击层现场诊断

同一题: 本地/混合求解 -> 正序点击 -> 截图看标记落点 -> 失败且标记清空后
反序重试。判定失败根因是顺序方向 / 坐标落点 / 识别错误。
"""
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import captcha_handler, config as cfgmod, web_flow  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')


def popup_gone(page):
    return not captcha_handler._popup_visible(page)


def marks(page):
    try:
        return len(page.query_selector_all('.cx_imgBtn img'))
    except Exception:
        return -1


def main():
    with sync_playwright() as pw:
        cfg = cfgmod.load()
        cfg['phone'] = '13000000000'
        cfg['browser_data_dir'] = './browser_data_diag'
        ctx = web_flow.start_browser(pw, cfg)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(web_flow.URL, wait_until='domcontentloaded')
            time.sleep(2)
            inp = page.query_selector('input[placeholder*="手机"]') \
                or page.query_selector('input[type="tel"]')
            inp.fill(cfg['phone'])
            (page.query_selector('text=获取验证码')
             or page.query_selector('button:has-text("登录")')).click()
            if not web_flow._wait_popup(page, timeout=10):
                print('弹窗未出现')
                return
            sprite = captcha_handler.get_sprite_bytes(page)
            if not sprite:
                print('sprite 获取失败')
                return
            with open('captcha_cache/diag_sprite.jpg', 'wb') as f:
                f.write(sprite)
            vlm = cfg.get('vlm') or {}
            clicks = captcha_handler.local_solve(page, sprite, log=print)
            src = '本地'
            if not clicks:
                clicks = captcha_handler.hybrid_solve(
                    page, sprite, None, vlm, log=print)
                src = '混合'
            if not clicks:
                clicks = captcha_handler.vlm_solve(sprite, vlm, log=print)
                src = 'VLM'
            print(f'求解源={src} clicks={clicks}')
            if not clicks:
                print('无解')
                return
            for x, y in clicks:
                captcha_handler._click_sprite(page, x, y)
            time.sleep(2)
            page.screenshot(path='captcha_cache/diag_after_clicks.png')
            n_mark = marks(page)
            print(f'正序点击后: 标记数={n_mark}, 弹窗关闭={popup_gone(page)}')
            if popup_gone(page):
                print('结论: 正序通过')
                return
            time.sleep(3)
            if marks(page) == 0:
                rev = list(reversed(clicks))
                print('标记已清空, 同题反序重试:', rev)
                for x, y in rev:
                    captcha_handler._click_sprite(page, x, y)
                time.sleep(2)
                page.screenshot(path='captcha_cache/diag_after_rev.png')
                print('反序结果:', '通过!' if popup_gone(page)
                      else f'未通过, 标记数={marks(page)}')
            else:
                print('标记未清空, 无法同题反序 (看 diag_after_clicks.png '
                      '核对标记落点)')
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_diag'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
