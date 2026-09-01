# -*- coding: utf-8 -*-
"""probe_measure.py - 测量验证码弹窗元素真实渲染尺寸

确认 #cx_imgBg 与 .cx_tips__answer_img 的 bounding box、
background-size, 定准点击坐标换算关系。
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfgmod, web_flow  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')

FAKE_PHONE = '13000000000'


def main():
    cfg = cfgmod.load()
    cfg['phone'] = FAKE_PHONE
    data_dir = os.path.join(cfgmod.BASE, 'browser_data_measure')
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
            info = page.evaluate("""() => {
              const pick = el => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return {rect: [r.x, r.y, r.width, r.height],
                        bgSize: cs.backgroundSize,
                        bgImg: (cs.backgroundImage || '').slice(0, 60)};
              };
              return {
                imgBg: pick(document.querySelector('#cx_imgBg')),
                tip: pick(document.querySelector('.cx_tips__answer_img')),
                tipWrap: pick(document.querySelector('.cx_click-tip')),
                eject: pick(document.querySelector('#eject'))
              };
            }""")
            for k, v in info.items():
                print(f'{k}: {v}')
        finally:
            ctx.close()
            import shutil
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
