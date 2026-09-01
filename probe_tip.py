# -*- coding: utf-8 -*-
"""probe_tip.py - 保存指令条各候选元素的截图与包围盒, 核对裁显区域"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from core import config as cfgmod, web_flow

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
        if web_flow._wait_popup(page, timeout=10):
            for sel, name in [('.cx_click-tip', 'tip_wrap'),
                              ('.cx_tips__answer_img', 'tip_img'),
                              ('.cx_tips__answer_div', 'tip_div'),
                              ('#cx_imgBg', 'imgbg')]:
                el = page.query_selector(sel)
                if el is None:
                    print(sel, '-> None')
                    continue
                print(sel, '->', el.bounding_box())
                try:
                    el.screenshot(path=f'captcha_cache/probe_{name}.png')
                except Exception as e:
                    print('  shot fail:', e)
        else:
            print('弹窗未出现')
    finally:
        ctx.close()
        shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_probe'),
                      ignore_errors=True)
