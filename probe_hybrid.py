# -*- coding: utf-8 -*-
"""probe_hybrid.py - 混合方案验证: 本地像素分割取精确图标中心 + AI 命名匹配顺序

1. 页面 canvas 解码 sprite (data URL 不跨域), 分割纯白/纯黑连通块得图标中心;
2. 主图叠加编号标记导出给 AI, AI 命名各编号图标 + 读 sprite 第一行顺序;
3. 按第一行顺序点击精确中心, 观察标记数与弹窗是否关闭。
"""
import base64
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from core import captcha_handler, config as cfgmod, web_flow

SEG_JS = """async (b64) => {
  const img = new Image();
  img.src = 'data:image/jpeg;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = 320; c.height = 240;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, 320, 240).data;
  const mask = new Uint8Array(320 * 240);
  for (let y = 0; y < 160; y++) for (let x = 0; x < 320; x++) {
    const i = (y * 320 + x) * 4;
    const white = d[i] > 235 && d[i+1] > 235 && d[i+2] > 235;
    const black = d[i] < 25 && d[i+1] < 25 && d[i+2] < 25;
    if (white || black) mask[y * 320 + x] = 1;
  }
  const seen = new Uint8Array(320 * 240);
  const comps = [];
  for (let y = 0; y < 160; y++) for (let x = 0; x < 320; x++) {
    const s = y * 320 + x;
    if (!mask[s] || seen[s]) continue;
    const q = [s]; seen[s] = 1;
    let px = 0, py = 0, n = 0, minx = 320, maxx = 0, miny = 160, maxy = 0;
    while (q.length) {
      const t = q.pop();
      const tx = t % 320, ty = (t / 320) | 0;
      px += tx; py += ty; n++;
      if (tx < minx) minx = tx; if (tx > maxx) maxx = tx;
      if (ty < miny) miny = ty; if (ty > maxy) maxy = ty;
      const nb = [[1,0],[-1,0],[0,1],[0,-1]];
      for (const dd of nb) {
        const nx = tx + dd[0], ny = ty + dd[1];
        if (nx < 0 || nx >= 320 || ny < 0 || ny >= 160) continue;
        const nt = ny * 320 + nx;
        if (mask[nt] && !seen[nt]) { seen[nt] = 1; q.push(nt); }
      }
    }
    const w = maxx - minx, h = maxy - miny;
    if (n >= 150 && w >= 12 && w <= 48 && h >= 12 && h <= 48)
      comps.push({x: Math.round(px/n), y: Math.round(py/n), n: n, w: w, h: h});
  }
  // 编号叠加图
  const c2 = document.createElement('canvas');
  c2.width = 320; c2.height = 160;
  const g2 = c2.getContext('2d');
  g2.drawImage(img, 0, 0);
  g2.font = 'bold 14px sans-serif';
  comps.forEach((p, i) => {
    g2.strokeStyle = '#f00'; g2.lineWidth = 2;
    g2.beginPath(); g2.arc(p.x, p.y, 14, 0, 7); g2.stroke();
    g2.fillStyle = '#f00';
    g2.fillText(String(i + 1), Math.min(306, p.x + 12), Math.max(12, p.y - 12));
  });
  return {comps: comps, marked: c2.toDataURL('image/png')};
}"""

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
        b64 = base64.b64encode(sprite).decode()
        seg = page.evaluate(SEG_JS, b64)
        comps = seg['comps']
        print('分割中心:', comps)
        with open('captcha_cache/marked.png', 'wb') as f:
            f.write(base64.b64decode(seg['marked'].split(',')[1]))
        if len(comps) < 3:
            print('分割不足 3 个图标')
            sys.exit(0)
        vlm = cfg.get('vlm') or {}
        prompt = (
            'Image 1: main CAPTCHA photo with numbered icons (red circles). '
            'Image 2: full sprite; its bottom FIRST row (y 160-185) shows 3 '
            'target icons left to right = required click order. '
            'Reply ONLY JSON: {"names": {"1": "icon name", ...}, '
            '"row": ["name", "name", "name"]} using the same names.')
        payload = {'model': vlm['model'], 'messages': [{'role': 'user',
                   'content': [
                       {'type': 'text', 'text': prompt},
                       {'type': 'image_url', 'image_url': {'url': seg['marked']}},
                       {'type': 'image_url', 'image_url': {
                           'url': f'data:image/jpeg;base64,{b64}'}}]}],
                   'temperature': 0}
        import urllib.request
        req = urllib.request.Request(
            vlm['base'].rstrip('/') + '/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + vlm['key'],
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=180) as r:
            text = json.load(r)['choices'][0]['message']['content']
        print('AI:', text[:400])
        m = json.loads(text[text.index('{'):text.rindex('}') + 1])
        names = {str(k): v.strip().lower() for k, v in m['names'].items()}
        row = [x.strip().lower() for x in m['row']]
        order = []
        for want in row:
            for num, nm in names.items():
                if nm == want and int(num) - 1 < len(comps):
                    order.append(comps[int(num) - 1])
                    break
        if len(order) != 3:
            print('命名匹配失败', names, row)
            sys.exit(0)
        print('点击顺序:', [(o['x'], o['y']) for o in order])
        for o in order:
            captcha_handler._click_sprite(page, o['x'], o['y'])
        time.sleep(3)
        n_marks = len(page.query_selector_all('.cx_imgBtn img'))
        gone = not captcha_handler._popup_visible(page)
        print(f'标记数={n_marks}, 弹窗关闭={gone}')
        print('结果:', '通过!' if gone else '未通过')
    finally:
        ctx.close()
        shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_probe'),
                      ignore_errors=True)
