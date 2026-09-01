# -*- coding: utf-8 -*-
"""probe_click2.py - 同弹窗内逐个变体测试点击注册方式 (以标记增量判定)

变体A: 直接点 #cx_obstacle_canvas (站点可能监听 canvas)
变体B: canvas 设 pointer-events:none 后点 #cx_imgBg
变体C: page.mouse.click 绝对坐标 (target=最上层可见元素)
"""
import base64
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from core import captcha_handler, config as cfgmod, web_flow


def mark_count(page):
    return len(page.query_selector_all('.cx_imgBtn img'))


with sync_playwright() as pw:
    cfg = cfgmod.load()
    cfg['phone'] = '13000000000'
    cfg['browser_data_dir'] = './browser_data_probe'
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
            sys.exit(0)
        base = mark_count(page)
        print('基线标记数 =', base)
        box = page.eval_on_selector(
            '#cx_imgBg', "el => { const r = el.getBoundingClientRect();"
                         " return {x: r.x, y: r.y}; }")

        def try_clicks(tag, fn, pts):
            n0 = mark_count(page)
            for x, y in pts:
                fn(x, y)
                time.sleep(0.5)
            d = mark_count(page) - n0
            print(f'{tag}: 标记增量={d}')
            return d

        pts = [(60, 75), (160, 78), (210, 92)]
        # 变体A: 点 canvas
        d = try_clicks('A(canvas)', lambda x, y: page.click(
            '#cx_obstacle_canvas', position={'x': x, 'y': y}, timeout=3000), pts)
        if d == 0:
            # 变体B: canvas 透明后点 #cx_imgBg
            page.eval_on_selector(
                '#cx_obstacle_canvas',
                "el => el.style.pointerEvents = 'none'")
            d = try_clicks('B(imgBg)', lambda x, y: page.click(
                '#cx_imgBg', position={'x': x, 'y': y}, timeout=3000), pts)
        if d == 0:
            # 变体C: 恢复 canvas, 用 mouse.click 绝对坐标
            page.eval_on_selector(
                '#cx_obstacle_canvas',
                "el => el.style.pointerEvents = ''")
            d = try_clicks('C(mouse)', lambda x, y: page.mouse.click(
                box['x'] + x, box['y'] + y), pts)
        gone = not captcha_handler._popup_visible(page)
        print('弹窗关闭 =', gone)
    finally:
        ctx.close()
        shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_probe'),
                      ignore_errors=True)
