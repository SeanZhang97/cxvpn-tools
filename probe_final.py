# -*- coding: utf-8 -*-
"""probe_final.py - 点击层决定性实验: 点击 3 个分割图标中心, 标记应=3"""
import base64
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import captcha_handler, config as cfgmod, web_flow  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')


def main():
    cfg = cfgmod.load()
    cfg['phone'] = '13000000000'
    cfg['browser_data_dir'] = './browser_data_final'
    with sync_playwright() as pw:
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
            time.sleep(1)
            sprite = captcha_handler.get_sprite_bytes(page)
            seg = page.evaluate(captcha_handler.SEG_JS,
                                base64.b64encode(sprite).decode())
            comps = seg['comps']
            print('comps centers:', [(c['x'], c['y']) for c in comps])
            rect = page.eval_on_selector(
                '#cx_imgBg',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y]; }")
            for i, c in enumerate(comps[:3]):
                page.mouse.click(rect[0] + c['x'], rect[1] + c['y'])
                time.sleep(1.5)  # 加大间隔验证站点节流假设
                n = len(page.query_selector_all('.cx_imgBtn img'))
                print(f'click{i + 1} at icon center '
                      f'({c["x"]},{c["y"]}) interval=1.5s -> marks={n}')
            time.sleep(2)
            print('popup gone:', not captcha_handler._popup_visible(page))
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_final'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
