# -*- coding: utf-8 -*-
"""probe_captcha.py - 打印验证码弹窗 DOM 结构, 用于核对交互机制"""
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
            html = page.eval_on_selector('#eject', 'el => el.outerHTML')
            print('===== #eject outerHTML =====')
            print(html[:4000])
        else:
            print('弹窗未出现')
    finally:
        ctx.close()
        shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_probe'),
                      ignore_errors=True)
