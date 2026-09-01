# -*- coding: utf-8 -*-
"""probe_tiprow.py - 验证页面指令条 (tip) 对应 sprite 的哪一排

假号触发弹窗, 取 tip 截图与 sprite, 二值化后把 tip 与 sprite 四排
(y160-180 黑正序 / 180-200 黑反序 / 200-220 白正序 / 220-240 白反序)
逐排比对 IoU, 确定点击顺序方向 (正序 or 反序)。
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

FAKE_PHONE = '13000000000'


def binarize(img, mode):
    """black=取黑图标前景, white=取白图标前景"""
    g = img.convert('L')
    w, h = g.size
    px = g.load()
    out = Image.new('L', (w, h), 0)
    op = out.load()
    for y in range(h):
        for x in range(w):
            v = px[x, y]
            op[x, y] = 255 if (v < 100 if mode == 'black' else v > 155) else 0
    return out


def iou_img(a, b):
    a = a.resize((320, 20), Image.NEAREST)
    b = b.resize((320, 20), Image.NEAREST)
    pa, pb = a.load(), b.load()
    inter = union = 0
    for y in range(20):
        for x in range(320):
            va, vb = pa[x, y] > 0, pb[x, y] > 0
            union += va or vb
            inter += va and vb
    return inter / union if union else 0


def main():
    cfg = cfgmod.load()
    cfg['phone'] = FAKE_PHONE
    data_dir = os.path.join(cfgmod.BASE, 'browser_data_tiprow')
    cfg['browser_data_dir'] = data_dir
    with sync_playwright() as pw:
        ctx = web_flow.start_browser(pw, cfg)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(web_flow.URL, wait_until='domcontentloaded')
            time.sleep(2)
            inp = page.query_selector('input[placeholder*="手机"]') \
                or page.query_selector('input[type="tel"]')
            inp.fill(FAKE_PHONE)
            (page.query_selector('text=获取验证码')
             or page.query_selector('button:has-text("登录")')).click()
            if not web_flow._wait_popup(page, timeout=10):
                print('弹窗未出现')
                return
            sprite = captcha_handler.get_sprite_bytes(page)
            tip = captcha_handler.compose_tip_strip(
        captcha_handler.get_tip_icons(page))
            if not sprite or not tip:
                print(f'获取失败 sprite={bool(sprite)} tip={bool(tip)}')
                return
            with open('captcha_cache/tiprow_sprite.jpg', 'wb') as f:
                f.write(sprite)
            with open('captcha_cache/tiprow_tip.png', 'wb') as f:
                f.write(tip)
            sp = Image.open(io.BytesIO(sprite))
            tp = Image.open(io.BytesIO(tip))
            print(f'tip 原始尺寸: {tp.size}')
            rows = {
                '行1(黑,正序)': (160, 180, 'black'),
                '行2(黑,反序)': (180, 200, 'black'),
                '行3(白,正序)': (200, 220, 'white'),
                '行4(白,反序)': (220, 240, 'white'),
            }
            for tip_mode in ('black', 'white'):
                tb = binarize(tp, tip_mode)
                print(f'--- tip 二值化模式: {tip_mode} ---')
                for name, (y0, y1, mode) in rows.items():
                    rb = binarize(sp.crop((0, y0, 320, y1)), mode)
                    print(f'  {name}: IoU={iou_img(tb, rb):.3f}')
        finally:
            ctx.close()
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
