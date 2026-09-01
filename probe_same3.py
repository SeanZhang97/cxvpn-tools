# -*- coding: utf-8 -*-
"""probe_same3.py - 同一坐标连点 3 次, 观察标记累加行为"""
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
        cfg['browser_data_dir'] = './browser_data_same3'
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
            rect = page.eval_on_selector(
                '#cx_imgBg',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y]; }")
            # 点同一坐标 3 次 (任意位置, 含空白), 观察 marks 变化
            for i in range(3):
                page.mouse.click(rect[0] + 160, rect[1] + 80)
                time.sleep(1)
                print(f'同点第{i + 1}次点击后 marks={marks(page)}', flush=True)
            # 再 dump #cx_imgBg 内部 DOM 看标记结构
            html = page.eval_on_selector('#cx_imgBg', 'el => el.innerHTML')
            print('imgBg innerHTML 长度:', len(html))
            print(html[:800])
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_same3'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
