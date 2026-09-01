# -*- coding: utf-8 -*-
"""probe_sitejs.py - 抓取验证码组件 JS, 逆向点击坐标逻辑"""
import os
import re
import shutil
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfgmod, web_flow  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')


def main():
    cfg = cfgmod.load()
    cfg['phone'] = '13000000000'
    cfg['browser_data_dir'] = './browser_data_js'
    with sync_playwright() as pw:
        ctx = web_flow.start_browser(pw, cfg)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(web_flow.URL, wait_until='domcontentloaded')
            time.sleep(2)
            html = page.content()
            srcs = re.findall(r'<script[^>]*src="([^"]+)"', html)
            cand = [s for s in srcs if re.search(
                r'captcha|verify|icon|cx|pass', s, re.I)]
            print('候选脚本:', cand)
            print('全部脚本:', srcs)
            for s in cand:
                try:
                    req = urllib.request.Request(
                        s if s.startswith('http') else web_flow.URL + s,
                        headers={'User-Agent': 'Mozilla/5.0',
                                 'Referer': 'https://remote.chaoxing.com/'})
                    js = urllib.request.urlopen(req, timeout=15).read()
                    js = js.decode('utf-8', 'ignore')
                    out = 'captcha_cache/sitejs_%d.js' % abs(hash(s)) % 100000
                    with open(out, 'w', encoding='utf-8') as f:
                        f.write(js)
                    print(f'{s} -> {out} ({len(js)} bytes)')
                    # 关键片段: 点击坐标处理
                    for kw in ('offsetX', 'pageX', 'clientX', 'offsetY',
                               'click', 'obstacle', 'imgBg'):
                        for m in re.finditer(re.escape(kw), js):
                            st = max(0, m.start() - 150)
                            print(f'  [{kw}] ...{js[st:m.start() + 200]}...')
                            break
                except Exception as e:
                    print(f'{s} 抓取失败: {e}')
        finally:
            ctx.close()
            shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_js'),
                          ignore_errors=True)


if __name__ == '__main__':
    main()
