# -*- coding: utf-8 -*-
"""dump_sitejs.py - 提取站点验证码 JS 点击处理逻辑片段"""
import sys

sys.stdout.reconfigure(encoding='utf-8')

js = open('captcha_cache/site_load.min.js', encoding='utf-8').read()
for pat in ["'.cx_imgBg')['on']", "'.cx_imgBg'][_0x", 'cx_obstacle_canvas']:
    i = js.find(pat)
    print(f'===== pattern {pat!r} at {i} =====')
    if i >= 0:
        print(js[i - 300:i + 2500])
    print()
