# -*- coding: utf-8 -*-
"""probe_match2.py - 同资产多尺度剪影匹配判别力验证

指令行图标与主图图标是同一矢量资产的两尺度渲染。用保持宽高比的
填充归一 + 多尺度搜索做剪影 IoU, 验证正确配对是否高分且区分度大。
"""
import glob
import os
import sys

import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding='utf-8')


def binarize_main(a):
    """主图: 纯白或纯黑像素为前景"""
    r, g, b = a[..., 0].astype(int), a[..., 1].astype(int), a[..., 2].astype(int)
    white = (r > 235) & (g > 235) & (b > 235)
    black = (r < 60) & (g < 60) & (b < 60)
    return (white | black).astype(np.uint8)


def components(mask, min_n=120, lo=10, hi=60):
    """8 邻域 BFS 连通块 (无 scipy 依赖)"""
    h, w = mask.shape
    seen = np.zeros_like(mask, bool)
    out = []
    for y0 in range(h):
        for x0 in range(w):
            if mask[y0, x0] and not seen[y0, x0]:
                stack = [(y0, x0)]
                seen[y0, x0] = True
                ys, xs = [y0], [x0]
                while stack:
                    cy, cx = stack.pop()
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < h and 0 <= nx < w \
                                    and mask[ny, nx] and not seen[ny, nx]:
                                seen[ny, nx] = True
                                stack.append((ny, nx))
                                ys.append(ny)
                                xs.append(nx)
                if len(ys) < min_n:
                    continue
                bh, bw = max(ys) - min(ys) + 1, max(xs) - min(xs) + 1
                if lo <= bw <= hi and lo <= bh <= hi:
                    out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


def tight(mask, x0, y0, x1, y1):
    return mask[y0:y1 + 1, x0:x1 + 1]


def pad_square(m):
    h, w = m.shape
    s = max(h, w)
    out = np.zeros((s, s), m.dtype)
    out[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = m
    return out


def norm(m, size=48):
    img = Image.fromarray((pad_square(m) * 255))
    img = img.resize((size, size), Image.NEAREST)
    return (np.array(img) > 127).astype(np.uint8)


def iou(a, b):
    u = a | b
    return (a & b).sum() / u.sum() if u.sum() else 0.0


def best_iou(t, c):
    """模板 t 与候选 c 的多尺度剪影 IoU (t/c 均先归一 48, 再对 t 做 +-尺度搜索)"""
    tn, cn = norm(t), norm(c)
    best = 0.0
    for scale in (0.8, 0.9, 1.0, 1.1, 1.2):
        sz = int(48 * scale)
        img = Image.fromarray((tn * 255)).resize((sz, sz), Image.NEAREST)
        ts = (np.array(img) > 127).astype(np.uint8)
        # 居中贴回 64 画布, 候选也贴回, 允许 +-4 位移
        canvas_t = np.zeros((64, 64), np.uint8)
        canvas_t[(64 - sz) // 2:(64 - sz) // 2 + sz,
                 (64 - sz) // 2:(64 - sz) // 2 + sz] = ts
        canvas_c = np.zeros((64, 64), np.uint8)
        canvas_c[8:56, 8:56] = cn
        for dy in range(-4, 5, 2):
            for dx in range(-4, 5, 2):
                sh = np.roll(canvas_t, (dy, dx), (0, 1))
                v = iou(sh, canvas_c)
                best = max(best, v)
    return best


def main():
    files = sorted(glob.glob('captcha_cache/manual_sprite.jpg'))
    files += sorted(glob.glob('captcha_cache/captcha_20260827_0*.jpg'))[-4:]
    for f in files:
        img = Image.open(f).convert('RGB')
        a = np.array(img)
        main_mask = binarize_main(a[:160])
        cands = components(main_mask)
        # 行1 模板 (黑图标)
        row = (np.array(img.crop((0, 160, 320, 180)).convert('L')) < 60)
        tcomps = components(row.astype(np.uint8), min_n=25, lo=6, hi=40)
        tcomps = sorted(tcomps, key=lambda c: c[0])[:3]
        print(f'\n{os.path.basename(f)}: 候选{len(cands)} 模板{len(tcomps)}')
        if not tcomps or len(cands) < 3:
            continue
        for ti, tc in enumerate(tcomps):
            t = tight(row.astype(np.uint8), *tc)
            scores = []
            for ci, cc in enumerate(cands):
                c = tight(main_mask, *cc)
                scores.append((best_iou(t, c), ci, cc))
            scores.sort(reverse=True)
            top = scores[:3]
            print(f'  模板{ti + 1}: ' + ' '.join(
                f'候选{ci + 1}@({cc[0]},{cc[1]})={v:.2f}'
                for v, ci, cc in top)
                + f'  gap={top[0][0] - top[1][0]:.2f}')


if __name__ == '__main__':
    main()
