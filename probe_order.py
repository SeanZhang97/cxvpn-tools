# -*- coding: utf-8 -*-
"""probe_order.py - 对照实验: 同一题先按 AI 读序点击, 失败且标记清空后按反序点击,
判定站点真实指令顺序; 同时保存 sprite 与指令条截图供人工核对"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from core import captcha_handler, config as cfgmod, web_flow


def popup_gone(page):
    return not captcha_handler._popup_visible(page)


def marks(page):
    try:
        return len(page.query_selector_all('.cx_imgBtn img'))
    except Exception:
        return -1


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
        sprite = captcha_handler.get_sprite_bytes(page)
        tip = captcha_handler.compose_tip_strip(
        captcha_handler.get_tip_icons(page))
        if sprite:
            with open('captcha_cache/live_sprite.jpg', 'wb') as f:
                f.write(sprite)
        if tip:
            with open('captcha_cache/live_tip.png', 'wb') as f:
                f.write(tip)
        vlm = cfg.get('vlm') or {}
        clicks = captcha_handler.vlm_solve(sprite, vlm, log=print,
                                           tip_bytes=tip)
        print('AI 点击:', clicks)
        if not clicks:
            print('AI 无结果')
            sys.exit(0)
        for x, y in clicks:
            captcha_handler._click_sprite(page, x, y)
        time.sleep(3)
        if popup_gone(page):
            print('结果: 按 AI 读序通过')
            sys.exit(0)
        print('AI 读序未通过; 点击标记数 =', marks(page))
        time.sleep(2)
        n_after = marks(page)
        if n_after == 0:
            rev = list(reversed(clicks))
            print('标记已清空, 尝试反序:', rev)
            for x, y in rev:
                captcha_handler._click_sprite(page, x, y)
            time.sleep(3)
            print('结果:', '反序通过!' if popup_gone(page) else '反序也未通过')
        else:
            print('标记未清空, 无法在同题重试')
    finally:
        ctx.close()
        shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_probe'),
                      ignore_errors=True)
