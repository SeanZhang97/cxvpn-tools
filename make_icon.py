# -*- coding: utf-8 -*-
"""make_icon.py - 合成学习通风格圆角红底流星图标 (icon.ico + ui/logo.png)"""
import os

import numpy as np
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))
SYM = r'C:\Users\29511\.qoder\vibe_images\star_symbol_1787726082.png'
S = 1024

# 1) 抠出白色流星符号
gray = np.array(Image.open(SYM).convert('L'))
star = (gray > 110).astype('uint8') * 255
ys, xs = np.where(star > 0)
star = star[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
sw = int(S * 0.74)
sh = max(2, int(sw * star.shape[0] / star.shape[1]))
star_img = Image.fromarray(star).resize((sw, sh), Image.LANCZOS)

# 2) 圆角黑底渐变 (顶部深灰蓝 -> 底部近黑, 与工具深色 UI 统一)
top = np.array([42, 47, 60])
bot = np.array([8, 10, 15])
t = np.linspace(0, 1, S)[:, None, None]
grad = (top * (1 - t) + bot * t).astype('uint8')
grad = np.tile(grad, (1, S, 1))
rgba = np.dstack([grad, np.full((S, S), 255, np.uint8)])
img = Image.fromarray(rgba, 'RGBA')

round_mask = Image.new('L', (S, S), 0)
ImageDraw.Draw(round_mask).rounded_rectangle(
    [0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)
img.putalpha(round_mask)

# 3) 居中贴白色流星
pos = ((S - sw) // 2, (S - sh) // 2 - int(S * 0.02))
img.paste(Image.new('RGBA', (sw, sh), (255, 255, 255, 255)), pos,
          mask=star_img)

# 4) 输出
logo = img.resize((256, 256), Image.LANCZOS)
logo.save(os.path.join(BASE, 'ui', 'logo.png'))
img.save(os.path.join(BASE, 'icon.ico'),
         sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                (128, 128), (256, 256)])
print('icon ok')
