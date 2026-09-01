# -*- coding: utf-8 -*-
"""probe_manual_click.py - 人工标注点击验证 (点击层/顺序方向决定性实验)

阶段1: 开弹窗存 sprite 到 captcha_cache/manual_sprite.jpg, 清空
       manual_clicks.json;
阶段2: 轮询 manual_clicks.json 直到出现 [[x,y]x3] (我看图标注写入);
阶段3: 按标注坐标点击, 报告标记数/弹窗关闭; 若不通过且标记清空,
       自动按反序再试一次 (验证顺序方向)。
"""
import json
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import captcha_handler, config as cfgmod, web_flow  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')

CLICKS_FILE = os.path.join('captcha_cache', 'manual_clicks.json')


def marks(page):
    try:
        return len(page.query_selector_all('.cx_imgBtn img'))
    except Exception:
        return -1


def main():
    if os.path.exists(CLICKS_FILE):
        os.remove(CLICKS_FILE)
    with sync_playwright() as pw:
        cfg = cfgmod.load()
        cfg['phone'] = '13000000000'
        cfg['browser_data_dir'] = './browser_data_manual'
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
            with open('captcha_cache/manual_sprite.jpg', 'wb') as f:
                f.write(sprite)
            tip = captcha_handler.compose_tip_strip(
        captcha_handler.get_tip_icons(page))
            if tip:
                with open('captcha_cache/manual_tip.png', 'wb') as f:
                    f.write(tip)
            el = page.query_selector('.cx_tips__answer_img')
            if el:
                el.screenshot(path='captcha_cache/manual_tip_full.png')
            bar = page.query_selector('.cx_click-tip')
            if bar:
                bar.screenshot(path='captcha_cache/manual_tipbar.png')
            print('sprite+tip+tipbar 已存, 等待 manual_clicks.json...',
                  flush=True)
            clicks = None
            deadline = time.time() + 180
            while time.time() < deadline:
                if os.path.exists(CLICKS_FILE):
                    try:
                        clicks = json.load(open(CLICKS_FILE))
                        if isinstance(clicks, list) and len(clicks) == 3:
                            break
                    except Exception:
                        pass
                time.sleep(1)
            if not clicks:
                print('超时未等到标注')
                return
            print('标注点击:', clicks, flush=True)
            for x, y in clicks:
                captcha_handler._click_sprite(page, x, y)
                print(f'  点击({x},{y}) 后 marks={marks(page)}', flush=True)
            for wait in (2, 4, 6, 8):
                time.sleep(2)
                gone = not captcha_handler._popup_visible(page)
                print(f'  +{wait}s: 弹窗关闭={gone}', flush=True)
                if gone:
                    break
            page.screenshot(path='captcha_cache/manual_after.png')
            gone = not captcha_handler._popup_visible(page)
            print(f'正序: 弹窗关闭={gone}', flush=True)
            if gone:
                print('结论: 人工标注正序通过! 点击层+顺序方向均正常')
                return
            time.sleep(3)
            if marks(page) == 0:
                rev = list(reversed(clicks))
                print('标记清空, 同题反序:', rev, flush=True)
                for x, y in rev:
                    captcha_handler._click_sprite(page, x, y)
                time.sleep(2)
                gone = not captcha_handler._popup_visible(page)
                print(f'反序: 标记={marks(page)} 弹窗关闭={gone}')
                if gone:
                    print('结论: 反序才通过! 真实点击顺序=行1反序!')
            else:
                print('标记未清空, 无法同题反序')
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_manual'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
