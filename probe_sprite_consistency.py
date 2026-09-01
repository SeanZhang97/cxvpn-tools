# -*- coding: utf-8 -*-
"""probe_sprite_consistency.py - 定位 sprite 与页面显示不一致的机制

同一弹窗内:
  A. get_sprite_bytes 两次 (urllib 重请求同一 URL) 是否相同
  B. urllib 取的 sprite 主图区 vs #cx_imgBg 元素截图 是否相同
"""
import io
import os
import shutil
import sys
import time

from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import captcha_handler, config as cfgmod, web_flow  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')


def diff_ratio(a, b):
    """两图 resize 到同尺寸后平均绝对差 (0-255)"""
    b = b.resize(a.size)
    pa, pb = a.convert('L').load(), b.convert('L').load()
    w, h = a.size
    s = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            s += abs(pa[x, y] - pb[x, y])
    return s / ((h // 4 + 1) * (w // 4 + 1))


def main():
    with sync_playwright() as pw:
        cfg = cfgmod.load()
        cfg['phone'] = '13000000000'
        cfg['browser_data_dir'] = './browser_data_consist'
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
            time.sleep(1.5)  # 等组件加载稳定
            s1 = captcha_handler.get_sprite_bytes(page)
            s2 = captcha_handler.get_sprite_bytes(page)
            el = page.query_selector('#cx_imgBg')
            shot = el.screenshot()
            i1 = Image.open(io.BytesIO(s1)).crop((0, 0, 320, 160))
            i2 = Image.open(io.BytesIO(s2)).crop((0, 0, 320, 160))
            ishot = Image.open(io.BytesIO(shot))
            print(f'元素截图尺寸: {ishot.size}')
            print(f'A. 两次 urllib 请求差异: {diff_ratio(i1, i2):.1f} '
                  f'(0=完全相同)')
            print(f'B. urllib vs 页面显示差异: {diff_ratio(i1, ishot):.1f}')
            with open('captcha_cache/consist_s1.jpg', 'wb') as f:
                f.write(s1)
            with open('captcha_cache/consist_shot.png', 'wb') as f:
                f.write(shot)
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_consist'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
