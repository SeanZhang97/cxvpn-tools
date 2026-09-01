# -*- coding: utf-8 -*-
"""probe_dualmatch.py - 黑/白双模板独立匹配 + 一致性互检实验

结构 (用户确认): 底部四排, 行1=黑图标真顺序, 行2=黑图标反转,
                  行3=白图标黑底同行1, 行4=白图标黑底同行2。
主图图标: 可能为白色或黑色版本, 尺寸约 strip 模板的 1.5~2.5 倍。

本实验:
  1. 行1 黑阈值 -> 3 个黑模板; 行3 白阈值(反相) -> 3 个白模板 (同顺序)
  2. 主图候选: 白/黑掩码分割, 填洞成实心剪影
  3. 匹配: 候选缩到模板原生尺度 (±15% 尺度搜索, ±2px 位移搜索), 取 IoU
  4. 黑模板与白模板独立跑贪心分配, 比较两者结论是否一致 (自校验)
  5. 输出: 一致性、最佳/次佳分数差 (区分度)
"""
import glob
import os
import sys
from collections import deque

import cv2
import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

K3 = np.ones((3, 3), np.uint8)


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
    """按 x 排序并相邻合并到恰好 3 个"""
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


def extract_strip_templates(gray, y0, y1, white_mode):
    """行 y0~y1 分割 3 模板; white_mode=True 时取亮像素(白图标黑底)"""
    strip = gray[y0:y1]
    if white_mode:
        m = (strip > 200).astype(np.uint8)
    else:
        m = (strip < 60).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, K3)
    comps = [c for c in label(m)
             if c[0] >= 30 and 8 <= c[2] - c[1] + 1 <= 60
             and 8 <= c[4] - c[3] + 1 <= 60]
    comps = merge_to3(comps)
    if not comps:
        return None
    out = []
    for n, x1, x2, yy1, yy2 in comps:
        sub = m[yy1:yy2 + 1, x1:x2 + 1]
        out.append((sub, (x1, yy1 + y0, x2, yy2 + y0)))
    return out


def fill_solid(mask):
    """填洞 -> 实心剪影 (边界洪泛背景, 其余为实心)"""
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


def score_pair(tpl_mask, cand_mask, scales=(0.85, 0.925, 1.0, 1.075, 1.15)):
    """候选实心剪影缩放到模板尺度, 尺度+位移搜索取最大 IoU"""
    th, tw = tpl_mask.shape
    ch, cw = cand_mask.shape
    base = (tw + th) / (cw + ch)  # 平均边长匹配的缩放比
    best = 0.0
    for s in scales:
        f = base * s
        nw, nh = max(2, round(cw * f)), max(2, round(ch * f))
        r = cv2.resize(cand_mask * 255, (nw, nh),
                       interpolation=cv2.INTER_AREA)
        r = (r > 127).astype(np.uint8)
        # 居中放置到 tw x th 画布, 位移搜索 ±2
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                canvas = np.zeros((th, tw), dtype=np.uint8)
                oy = (th - nh) // 2 + dy
                ox = (tw - nw) // 2 + dx
                ys = max(0, oy); ye = min(th, oy + nh)
                xs = max(0, ox); xe = min(tw, ox + nw)
                if ye <= ys or xe <= xs:
                    continue
                canvas[ys:ye, xs:xe] = r[ys - oy:ye - oy, xs - ox:xe - ox]
                inter = np.logical_and(tpl_mask, canvas).sum()
                union = np.logical_or(tpl_mask, canvas).sum()
                if union:
                    best = max(best, inter / union)
    return best


def main():
    files = sorted(glob.glob('captcha_cache/captcha_*.jpg'))
    agree_n = total_n = 0
    for f in files:
        name = os.path.basename(f)[:-4]
        img = cv2.imread(f)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        black_t = extract_strip_templates(gray, 160, 180, False)
        white_t = extract_strip_templates(gray, 200, 220, True)

        # 主图候选
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

        def run_match(templates):
            """返回 assign: [(score, ci, second_best)] 或 None"""
            if not templates:
                return None
            tmask = [fill_solid(t[0]) for t in templates]
            pairs = []
            for ti in range(3):
                for ci in range(len(cand_masks)):
                    pairs.append((score_pair(tmask[ti], cand_masks[ci]),
                                  ti, ci))
            pairs.sort(reverse=True)
            used_t, used_c, assign = set(), set(), [None] * 3
            for s, ti, ci in pairs:
                if ti in used_t or ci in used_c:
                    continue
                assign[ti] = (s, ci)
                used_t.add(ti)
                used_c.add(ci)
                if len(used_t) == 3:
                    break
            # 次佳分数 (同模板未分配候选的最高分)
            out = []
            for ti in range(3):
                if assign[ti] is None:
                    out.append(None)
                    continue
                s, ci = assign[ti]
                second = max((p[0] for p in pairs
                              if p[1] == ti and p[2] != ci), default=0)
                out.append((s, ci, second))
            return out

        ab = run_match(black_t)
        aw = run_match(white_t)
        if ab is None or aw is None or any(a is None for a in ab) \
                or any(a is None for a in aw):
            print(f'{name}: FAIL 分配不齐 '
                  f'(黑模板={"ok" if black_t else "无"}, '
                  f'白模板={"ok" if white_t else "无"})')
            continue
        total_n += 1
        agree = all(ab[i][1] == aw[i][1] for i in range(3))
        agree_n += agree
        desc = []
        for i in range(3):
            sb, sw = ab[i], aw[i]
            cb = ccomps[sb[1]]
            cw_ = ccomps[sw[1]]
            cx = (cb[1] + cb[2]) // 2
            cy = (cb[3] + cb[4]) // 2
            desc.append(f'#{i + 1} 黑{sb[0]:.2f}/白{sw[0]:.2f}'
                        f'@({cx},{cy})'
                        f'{"" if sb[1] == sw[1] else " ⚠不一致"
                          + f"(白@({(cw_[1] + cw_[2]) // 2},"
                            f"{(cw_[3] + cw_[4]) // 2}))"}'
                        f' 次{max(sb[2], sw[2]):.2f}')
        print(f'{name}: {"✓黑白一致" if agree else "✗不一致"}  '
              + '  '.join(desc))
    print(f'\n=== 黑白一致性: {agree_n}/{total_n} ===')


if __name__ == '__main__':
    main()
