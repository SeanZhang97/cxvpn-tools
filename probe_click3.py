# -*- coding: utf-8 -*-
"""probe_click3.py - 点击精确中心后截图主区 + dump 相关 DOM, 肉眼确认标记是否出现"""
import base64
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
  return {comps: comps};
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
        seg = page.evaluate(SEG_JS, base64.b64encode(sprite).decode())
        comps = seg['comps']
        print('中心:', [(c['x'], c['y']) for c in comps])
        for c in comps[:3]:
            page.click('#cx_imgBg', position={'x': c['x'], 'y': c['y']},
                       timeout=3000)
            time.sleep(0.6)
        page.query_selector('#cx_imgBg').screenshot(
            path='captcha_cache/after_clicks.png')
        print('imgBtn HTML:', page.eval_on_selector(
            '.cx_imgBtn', 'el => el.innerHTML')[:300])
        print('已截图 captcha_cache/after_clicks.png')
    finally:
        ctx.close()
        shutil.rmtree(os.path.join(cfgmod.BASE, 'browser_data_probe'),
                      ignore_errors=True)
