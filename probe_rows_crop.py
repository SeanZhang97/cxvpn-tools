# -*- coding: utf-8 -*-
"""probe_rows_crop.py - 裁出 sprite 底部四排图标, 放大拼图直视结构

每张样本: y160-180 / y180-200 / y200-220 / y220-240 四条各裁出,
4x 放大 (最近邻), 带分隔条纵向拼接存 captcha_cache/rows_*.png。
同时输出行间像素差 (判断哪些行是重复渲染)。
"""
import glob
import os
import sys

import cv2
import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

S = 4  # 放大倍数


def main():
    files = sorted(glob.glob('captcha_cache/captcha_*.jpg'))
    # 取最近 3 张 + 一张中间样本
    picks = files[-3:] if len(files) >= 3 else files
    for f in picks:
        name = os.path.basename(f)[:-4]
        img = cv2.imread(f)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        rows = []
        labels = ['row1 y160-180', 'row2 y180-200',
                  'row3 y200-220', 'row4 y220-240']
        raws = []
        for i, y0 in enumerate((160, 180, 200, 220)):
            r = img[y0:y0 + 20]
            raws.append(cv2.cvtColor(r, cv2.COLOR_BGR2GRAY).astype(int))
            big = cv2.resize(r, (320 * S, 20 * S),
                             interpolation=cv2.INTER_NEAREST)
            # 顶部加标签条
            bar = np.full((18, 320 * S, 3), 255, np.uint8)
            cv2.putText(bar, labels[i], (6, 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
            sep = np.full((4, 320 * S, 3), 200, np.uint8)
            rows.append(np.vstack([bar, big, sep]))
        out = np.vstack(rows)
        p = f'captcha_cache/rows_{name}.png'
        cv2.imwrite(p, out)
        # 行间差分: 判断重复
        diffs = []
        for a, b in ((0, 1), (2, 3), (0, 2), (1, 3)):
            d = np.abs(raws[a] - raws[b]).mean()
            diffs.append(f'{labels[a][:4]}~{labels[b][:4]}:{d:.1f}')
        # 每行的黑白占比
        stats = []
        for i, r in enumerate(raws):
            pb = (r < 60).mean()
            pw = (r > 235).mean()
            stats.append(f'{labels[i][:4]} 黑{pb:5.1%} 白{pw:5.1%}')
        print(f'{name}: {p}')
        print(f'   差分: {"  ".join(diffs)}')
        print(f'   占比: {"  ".join(stats)}')


if __name__ == '__main__':
    main()
