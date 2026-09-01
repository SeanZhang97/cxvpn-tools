# -*- coding: utf-8 -*-
"""probe_clicklayer.py - 点击层逐次观测

同一题点击 3 次, 每次点击后: 标记数 / 点击位置顶层元素
(elementFromPoint) / 是否异常。并对比 page.click(position) 与
page.mouse.click(绝对坐标) 两种派发方式。
"""
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import captcha_handler, config as cfgmod, web_flow  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')


def marks(page):
    try:
        return len(page.query_selector_all('.cx_imgBtn img'))
    except Exception:
        return -1


def main():
    with sync_playwright() as pw:
        cfg = cfgmod.load()
        cfg['phone'] = '13000000000'
        cfg['browser_data_dir'] = './browser_data_clicklayer'
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
            vlm = cfg.get('vlm') or {}
            clicks = captcha_handler.local_solve(page, sprite, log=print)
            if not clicks:
                clicks = captcha_handler.hybrid_solve(
                    page, sprite, None, vlm, log=print)
            print('clicks =', clicks)
            if not clicks:
                return
            rect = page.eval_on_selector(
                '#cx_imgBg',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }")
            print('#cx_imgBg rect =', rect)
            for i, (x, y) in enumerate(clicks):
                ax, ay = rect[0] + x, rect[1] + y
                hit = page.evaluate(
                    "([x, y]) => { const el = document.elementFromPoint(x, y);"
                    " return el ? (el.id || el.className || el.tagName) : 'none'; }",
                    [ax, ay])
                print(f'--- 点击{i + 1} ({x},{y}) 绝对({ax:.0f},{ay:.0f}) '
                      f'顶层元素={hit}')
                try:
                    page.mouse.click(ax, ay)  # 绝对坐标派发, 绕过 actionability
                    time.sleep(0.8)
                    print(f'    mouse.click 后标记数={marks(page)}')
                except Exception as e:
                    print(f'    mouse.click 异常: {e}')
            time.sleep(2)
            print('最终: 弹窗关闭=', not captcha_handler._popup_visible(page),
                  '标记数=', marks(page))
            page.screenshot(path='captcha_cache/clicklayer_final.png')
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE,
                                       'browser_data_clicklayer'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
