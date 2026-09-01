# -*- coding: utf-8 -*-
"""probe_verify.py - 离线验证 captcha_handler 新链路 (预检 + 本地双行互检匹配)

对 captcha_cache/ 历史样本:
  1. assess() 预检判定分布 (难图应被门控刷新)
  2. SOLVE_JS 行1/行3 双行互检匹配: 命中率与 twin/IoU/gap 分数分布
     (无标注数据, 命中的正确性用 marked 调试图人工抽查)
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


def main():
    files = sorted(glob.glob('captcha_cache/captcha_*.jpg'))
    if os.path.exists('captcha_cache/live_sprite.jpg'):
        files.append('captcha_cache/live_sprite.jpg')
    if not files:
        print('captcha_cache/ 无历史样本')
        return
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.goto('about:blank')
        n_easy = n_hard = n_hit = 0
        for f in files:
            sprite = open(f, 'rb').read()
            name = os.path.basename(f)
            a = ch.assess(page, sprite)
            tag = '易' if a['ok'] else '难'
            print(f'{name}: 预检={tag} 有效{a.get("n_valid")} '
                  f'低对比{a.get("n_low")} 行1={a.get("strip1")} '
                  f'行3={a.get("strip3")}'
                  + ('' if a['ok'] else f' [{a.get("reason")}]'))
            if a['ok']:
                n_easy += 1
            else:
                n_hard += 1
            # 混合方案只依赖候选召回，不要求纯 CV 能区分图标。
            b64 = base64.b64encode(sprite).decode()
            comps = ch._extract_hybrid_candidates(sprite)
            gallery_ok = ch._compose_candidate_gallery(sprite, comps) is not None
            guides_ok = ch._build_hybrid_guides(sprite, None) is not None
            print(f'   混合候选={len(comps)} 图册={"可用" if gallery_ok else "失败"} '
                  f'双行目标卡={"可用" if guides_ok else "失败"}')
            # 无论预检结果都跑本地匹配, 观察分数分布以校准阈值
            res = page.evaluate(ch.SOLVE_JS,
                                {'b64': b64, 'min_iou': 0.45,
                                 'min_gap': 0.06, 'min_twin': 0.70})
            if res.get('ok'):
                n_hit += 1
                # 存调试图供人工抽查命中正确性
                out = os.path.join('captcha_cache', 'verify_'
                                   + name.replace('.jpg', '') + '.png')
                try:
                    with open(out, 'wb') as fo:
                        fo.write(base64.b64decode(
                            res['marked'].split(',', 1)[1]))
                except Exception:
                    pass
                print(f'   本地匹配命中: clicks={res["clicks"]} '
                      f'twin={res["twin"]} IoU={res["scores"]} '
                      f'weak={res["weak"]} perm_gap={res["perm_gap"]}')
            else:
                print(f'   本地匹配未命中: {res.get("err")}')
        browser.close()
        print(f'\n汇总: 样本{len(files)} 预检易{n_easy}/难{n_hard} '
              f'本地匹配命中{n_hit} (调试图存 captcha_cache/verify_*.png)')


if __name__ == '__main__':
    main()
