# -*- coding: utf-8 -*-
"""probe_chamfer.py - 4 渲染融合 + 距离变换匹配 (区分度终极实验)

结构: 行1/行2 = 黑图标正/反序, 行3/行4 = 白图标黑底正/反序。
=> 每个目标图标有 4 个独立渲染 (12~20px), 可融合降噪。

匹配: chamfer 距离 (候选剪影 DT 场上采样模板轮廓点),
     亚像素敏感, 对边界噪声容忍; 分数 = exp(-mean_dt/sigma)。
融合: 每图标 4 个渲染分别打分取均值; 黑白同时给出互检。

输出: 每模板的 (best, second) 与差值 gap —— 判定区分度是否够用。
"""
import glob
import math
import os
import sys
from collections import deque

import cv2
import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

K3 = np.ones((3, 3), np.uint8)
SIGMA = 3.0  # chamfer -> [0,1] 的尺度参数


def label(m):
    h, w = m.shape
    seen = np.zeros_like(m, dtype=bool)
    out = []
    for sy in range(h):
        for sx in range(w):
            if not m[sy, sx] or seen[sy, sx]:
                continue
            q = deque([(sx, sy)])
            seen[sy, sx] = True
            n, minx, maxx, miny, maxy = 0, w, 0, h, 0
            while q:
                tx, ty = q.popleft()
                n += 1
                minx, maxx = min(minx, tx), max(maxx, tx)
                miny, maxy = min(miny, ty), max(maxy, ty)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if not dx and not dy:
                            continue
                        nx, ny = tx + dx, ty + dy
                        if 0 <= nx < w and 0 <= ny < h \
                                and m[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            q.append((nx, ny))
            out.append((n, minx, maxx, miny, maxy))
    return out


def merge_to3(comps):
    comps = sorted(comps, key=lambda c: c[1])
    while len(comps) > 3:
        gaps = [comps[i + 1][1] - comps[i][2]
                for i in range(len(comps) - 1)]
        i = int(np.argmin(gaps))
        a, c2 = comps[i], comps[i + 1]
        comps[i:i + 2] = [(a[0] + c2[0], min(a[1], c2[1]),
                           max(a[2], c2[2]), min(a[3], c2[3]),
                           max(a[4], c2[4]))]
    return comps if len(comps) == 3 else None


def fill_solid(mask):
    h, w = mask.shape
    bg = np.zeros((h, w), dtype=bool)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not mask[y, x] and not bg[y, x]:
                bg[y, x] = True
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not mask[y, x] and not bg[y, x]:
                bg[y, x] = True
                q.append((x, y))
    while q:
        tx, ty = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = tx + dx, ty + dy
            if 0 <= nx < w and 0 <= ny < h \
                    and not mask[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True
                q.append((nx, ny))
    return (mask | ~bg).astype(np.uint8)


def extract_row(gray, y0, y1, white_mode):
    strip = gray[y0:y1]
    m = (strip > 200).astype(np.uint8) if white_mode \
        else (strip < 60).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, K3)
    comps = [c for c in label(m)
             if c[0] >= 30 and 8 <= c[2] - c[1] + 1 <= 60
             and 8 <= c[4] - c[3] + 1 <= 60]
    comps = merge_to3(comps)
    if not comps:
        return None
    return [fill_solid(m[y1_:y2_ + 1, x1:x2 + 1])
            for n, x1, x2, y1_, y2_ in comps]


def chamfer_score(tpl, cand, scales=(0.8, 0.9, 1.0, 1.1, 1.25),
                  shifts=((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 0),
                          (0, 1), (1, -1), (1, 0), (1, 1))):
    """候选 DT 场上采样模板像素; 归一化到 [0,1] (1=完美)"""
    th, tw = tpl.shape
    ch, cw = cand.shape
    base = (tw + th) / (cw + ch)
    best = 0.0
    for s in scales:
        f = base * s
        nw, nh = max(3, round(cw * f)), max(3, round(ch * f))
        r = cv2.resize(cand * 255, (nw, nh),
                       interpolation=cv2.INTER_AREA)
        r = (r > 127).astype(np.uint8)
        dt = cv2.distanceTransform((1 - r), cv2.DIST_L2, 3)
        for dy, dx in shifts:
            canvas = np.zeros((th, tw), dtype=np.uint8)
            oy = (th - nh) // 2 + dy
            ox = (tw - nw) // 2 + dx
            ys, ye = max(0, oy), min(th, oy + nh)
            xs, xe = max(0, ox), min(tw, ox + nw)
            if ye <= ys or xe <= xs:
                continue
            canvas[ys:ye, xs:xe] = r[ys - oy:ye - oy, xs - ox:xe - ox]
            # 模板像素在 canvas 覆盖率高才有效
            cov = np.logical_and(tpl, canvas).sum() / max(1, tpl.sum())
            if cov < 0.6:
                continue
            pts = np.nonzero(tpl)
            if len(pts[0]) == 0:
                continue
            # 模板点落在 canvas 图标外 -> 查候选 DT (需把 canvas 外
            # 区域映射回候选坐标; 简化: 用 canvas 自身 DT 的反距离)
            dtc = cv2.distanceTransform((1 - canvas), cv2.DIST_L2, 3)
            d = dtc[pts]
            mean_d = float(d.mean())
            sc = math.exp(-mean_d / SIGMA) * (0.5 + 0.5 * cov)
            best = max(best, sc)
    return best


def main():
    files = sorted(glob.glob('captcha_cache/captcha_*.jpg'))
    gap_ok = total = 0
    for f in files:
        name = os.path.basename(f)[:-4]
        img = cv2.imread(f)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        r1 = extract_row(gray, 160, 180, False)   # 黑 正序
        r2 = extract_row(gray, 180, 200, False)   # 黑 反序
        r3 = extract_row(gray, 200, 220, True)    # 白 正序
        r4 = extract_row(gray, 220, 240, True)    # 白 反序
        if not (r1 and r2 and r3 and r4):
            print(f'{name}: FAIL 行提取 '
                  f'{bool(r1)},{bool(r2)},{bool(r3)},{bool(r4)}')
            continue
        # 图标 i 的 4 渲染: r1[i], r2[2-i], r3[i], r4[2-i]
        renders = [[r1[i], r2[2 - i], r3[i], r4[2 - i]]
                   for i in range(3)]

        main_img = img[0:160]
        b, g, r = (main_img[:, :, 0].astype(int),
                   main_img[:, :, 1].astype(int),
                   main_img[:, :, 2].astype(int))
        icon = (((r > 235) & (g > 235) & (b > 235))
                | ((r < 60) & (g < 60) & (b < 60))).astype(np.uint8)
        icon = cv2.morphologyEx(icon, cv2.MORPH_CLOSE, K3)
        ccomps = [c for c in label(icon)
                  if c[0] >= 100 and 8 <= c[2] - c[1] + 1 <= 60
                  and 8 <= c[4] - c[3] + 1 <= 60]
        ccomps.sort(key=lambda c: -c[0])
        ccomps = ccomps[:24]
        if not ccomps:
            print(f'{name}: FAIL 无候选')
            continue
        cand_masks = [fill_solid(icon[y1:y2 + 1, x1:x2 + 1])
                      for n, x1, x2, y1, y2 in ccomps]

        # 融合分数矩阵
        mat = np.zeros((3, len(cand_masks)))
        for ti in range(3):
            for ci in range(len(cand_masks)):
                ss = [chamfer_score(t, cand_masks[ci])
                      for t in renders[ti]]
                mat[ti, ci] = float(np.mean(ss))
        # 贪心分配
        pairs = sorted(((mat[ti, ci], ti, ci)
                        for ti in range(3)
                        for ci in range(len(cand_masks))), reverse=True)
        used_t, used_c, assign = set(), set(), [None] * 3
        for s, ti, ci in pairs:
            if ti in used_t or ci in used_c:
                continue
            assign[ti] = (s, ci)
            used_t.add(ti)
            used_c.add(ci)
            if len(used_t) == 3:
                break
        if any(a is None for a in assign):
            print(f'{name}: FAIL 分配不齐')
            continue
        total += 1
        line = []
        all_gap_ok = True
        for ti in range(3):
            s, ci = assign[ti]
            second = max((mat[ti, c] for c in range(len(cand_masks))
                          if c != ci), default=0)
            gap = s - second
            gap_ok_i = gap >= 0.05
            all_gap_ok &= gap_ok_i
            cc = ccomps[ci]
            line.append(f'#{ti + 1} {s:.3f}/次{second:.3f}'
                        f'(gap{gap:+.3f})'
                        f'@({(cc[1] + cc[2]) // 2},'
                        f'{(cc[3] + cc[4]) // 2})'
                        f'{"✓" if gap_ok_i else "?"}')
        gap_ok += all_gap_ok
        print(f'{name}: ' + '  '.join(line))
    print(f'\n=== 三模板全部 gap>=0.05: {gap_ok}/{total} ===')


if __name__ == '__main__':
    main()
