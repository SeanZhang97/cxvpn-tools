# -*- coding: utf-8 -*-
"""probe_tmpl.py - 离线验证本地模板匹配求解器

对 captcha_cache/ 中的历史 sprite 逐一运行 SOLVE_JS,
打印匹配得分/坐标, 标注图存 captcha_cache/tm_*.png 供肉眼复核。
"""
import base64
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from core import captcha_handler
from core.captcha_handler import SOLVE_JS


def main():
    files = sorted(glob.glob('captcha_cache/captcha_*.jpg'))
    extra = 'captcha_cache/live_sprite.jpg'
    if os.path.exists(extra):
        files.append(extra)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.goto('about:blank')
        ok_n = 0
        for f in files:
            sprite = open(f, 'rb').read()
            b64 = base64.b64encode(sprite).decode()
            res = page.evaluate(SOLVE_JS, {'b64': b64, 'min_iou': 0.45})
            name = os.path.basename(f)
            if res.get('ok'):
                ok_n += 1
                out = f'captcha_cache/tm_{name[:-4]}.png'
                with open(out, 'wb') as fo:
                    fo.write(base64.b64decode(res['marked'].split(',', 1)[1]))
                print(f'{name}: OK clicks={res["clicks"]} '
                      f'scores={res["scores"]} -> {out}')
            else:
                print(f'{name}: FAIL {res.get("err")}')
        browser.close()
        print(f'=== 离线匹配: {ok_n}/{len(files)} ===')


if __name__ == '__main__':
    main()
