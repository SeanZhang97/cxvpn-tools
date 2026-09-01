# -*- coding: utf-8 -*-
"""probe_diag.py - 双行互检匹配分数矩阵诊断

复用 SOLVE_JS 前半段 (工具函数+模板提取+主图候选), 后半替换为
分数矩阵输出: twin (行1/行3 同位互检) + 每目标对全部候选的
IoU 分数 (黑/白/均值), 用于校准 MIN_IOU/MIN_GAP/MIN_TWIN 阈值。
"""
import base64
import glob
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'core'))
import captcha_handler as ch  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')

HEAD = ch.SOLVE_JS[:ch.SOLVE_JS.index('    // ---- 全局最优分配')]
DIAG_TAIL = r"""
    const diag = {
      twin: twin,
      cands: mainComps.map(c =>
        [c.minx, c.miny, c.maxx, c.maxy, c.n]),
      targets: []
    };
    for (let i = 0; i < 3; i++) {
      const s1 = candSigs.map(sg => iou(t1s[i], sg));
      const s3 = candSigs.map(sg => iou(t3s[i], sg));
      diag.targets.push({
        s1: s1.map(v => +v.toFixed(3)),
        s3: s3.map(v => +v.toFixed(3)),
        avg: s1.map((v, k) => +((v + s3[k]) / 2).toFixed(3))
      });
    }
    return { ok: true, diag: diag };
  } catch (e) {
    return { ok: false, err: 'JS 异常: ' + (e && e.message || e) };
  }
}"""
DIAG_JS = HEAD + DIAG_TAIL


def main():
    files = sorted(glob.glob('captcha_cache/captcha_*.jpg'))
    if os.path.exists('captcha_cache/live_sprite.jpg'):
        files.append('captcha_cache/live_sprite.jpg')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.goto('about:blank')
        for f in files:
            sprite = open(f, 'rb').read()
            name = os.path.basename(f)
            b64 = base64.b64encode(sprite).decode()
            res = page.evaluate(DIAG_JS, {'b64': b64, 'min_twin': 0})
            if not res.get('ok'):
                print(f'{name}: {res.get("err")}')
                continue
            d = res['diag']
            print(f'\n{name}: twin={d["twin"]} 候选={len(d["cands"])}')
            for k, c in enumerate(d['cands']):
                print(f'  cand#{k + 1}: bbox={c[:4]} n={c[4]}')
            for i, t in enumerate(d['targets']):
                # 按均值降序列出前 3
                order = sorted(range(len(t['avg'])),
                               key=lambda k: -t['avg'][k])[:3]
                parts = [f'#{k + 1}:avg{t["avg"][k]:.2f}'
                         f'(黑{t["s1"][k]:.2f}/白{t["s3"][k]:.2f})'
                         for k in order]
                gap = t['avg'][order[0]] - t['avg'][order[1]] \
                    if len(order) > 1 else 9
                print(f'  目标{i + 1}: ' + ' '.join(parts)
                      + f'  gap={gap:.2f}')
        browser.close()


if __name__ == '__main__':
    main()
