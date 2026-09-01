# -*- coding: utf-8 -*-
"""probe_grid.py - 网格坐标法测通过率: 2x 网格主图 + 全图第一行顺序, 3 轮"""
import base64
import json
import os
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from core import captcha_handler, config as cfgmod, web_flow

GRID_HTML = """<!DOCTYPE html><html><body style="margin:0">
<div style="position:relative;width:640px;height:320px">
<img src="%s" style="position:absolute;left:0;top:0;width:640px;height:320px">
<canvas id="c" width="640" height="320" style="position:absolute;left:0;top:0"></canvas>
</div>
<script>
const g = document.getElementById('c').getContext('2d');
g.strokeStyle = 'rgba(255,0,0,.55)'; g.fillStyle = '#f00';
g.font = 'bold 11px sans-serif'; g.lineWidth = 1;
for (let x = 0; x <= 320; x += 20) {
  g.beginPath(); g.moveTo(x*2+.5, 0); g.lineTo(x*2+.5, 320); g.stroke();
  if (x %% 40 === 0) g.fillText(String(x), x*2+2, 12);
}
for (let y = 0; y <= 160; y += 20) {
  g.beginPath(); g.moveTo(0, y*2+.5); g.lineTo(640, y*2+.5); g.stroke();
  if (y %% 40 === 0 && y > 0) g.fillText(String(y), 2, y*2-2);
}
</script></body></html>"""

PROMPT = (
    'Image 1: CAPTCHA main photo (2x scale) with red grid every 20 sprite px '
    '(axis labels in sprite coordinates). Image 2: full sprite; the bottom '
    'FIRST row of black/white icons (y 160-185), left to right, is the '
    'required click order. Locate the center of each of those 3 icons in the '
    'main photo using the grid. Reply ONLY JSON: '
    '{"clicks": [[x1,y1],[x2,y2],[x3,y3]]} in sprite coordinates '
    '(x 0..320, y 0..160).')


def solve(page, cfg):
    sprite = captcha_handler.get_sprite_bytes(page)
    b64 = base64.b64encode(sprite).decode()
    gp = page.context.new_page()
    gp.set_content(GRID_HTML % ('data:image/jpeg;base64,' + b64))
    gp.wait_for_timeout(500)
    grid_b64 = base64.b64encode(gp.screenshot()).decode()
    gp.close()
    vlm = cfg['vlm']
    payload = {'model': vlm['model'], 'messages': [{'role': 'user',
               'content': [
                   {'type': 'text', 'text': PROMPT},
                   {'type': 'image_url', 'image_url': {
                       'url': f'data:image/png;base64,{grid_b64}'}},
                   {'type': 'image_url', 'image_url': {
                       'url': f'data:image/jpeg;base64,{b64}'}}]}],
               'temperature': 0}
    req = urllib.request.Request(
        vlm['base'].rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + vlm['key'],
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        text = json.load(r)['choices'][0]['message']['content']
    print('AI:', text[:300], flush=True)
    m = json.loads(text[text.index('{'):text.rindex('}') + 1])
    return [(int(p[0]), int(p[1])) for p in m['clicks']]


def main():
    results = []
    with sync_playwright() as pw:
        for i in range(1, 4):
            cfg = cfgmod.load()
            cfg['phone'] = '13000000000'
            cfg['browser_data_dir'] = f'./browser_data_test_{i}'
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
                    print(f'[round {i}] 弹窗未出现', flush=True)
                    continue
                clicks = solve(page, cfg)
                print(f'[round {i}] 点击: {clicks}', flush=True)
                for x, y in clicks:
                    captcha_handler._click_sprite(page, x, y)
                ok = False
                for _ in range(8):
                    if not captcha_handler._popup_visible(page):
                        ok = True
                        break
                    time.sleep(1)
                print(f'[round {i}] 结果: {"通过" if ok else "未通过"}',
                      flush=True)
                results.append(ok)
            finally:
                ctx.close()
                shutil.rmtree(os.path.join(cfgmod.BASE,
                              f'browser_data_test_{i}'), ignore_errors=True)
    print(f'=== 网格法通过率: {sum(results)}/{len(results)} ===', flush=True)


if __name__ == '__main__':
    main()
