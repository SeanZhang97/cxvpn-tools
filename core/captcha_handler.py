# -*- coding: utf-8 -*-
"""
captcha_handler.py - 图标点选验证码处理

sprite 结构 (320x240): 上 160px 主图 (图标可白可黑, 实心带内部镂空,
20~45px); 底部四排指令区 (每排 20px): 行1 黑图标=真实点击顺序,
行2=行1 反序, 行3 白图标黑底=行1 同序, 行4=行3 反序。

流程:
  1. 混合候选编号优先：本地多尺度分割主图候选，生成“原始裁图 +
     归一化剪影”图册；VLM 先给目标/候选统一命名，名称与编号映射完全
     自洽才采用,
     AI 只返回三个互斥候选编号, 点击坐标完全由本地候选质心产生; 指令图标
     同时提供黑白归一化及水平镜像视图, 消除颜色/翻转干扰。
  2. 混合识别不可用时回退端到端 VLM: 2x 放大 sprite + 指令条直发 AI;
     AI 坐标参考系按"像素帧/归一化帧"双轨换算并经墨迹评分仲裁验证。
  3. 失败刷新重试 (最多 max_attempts 次) + 人工兜底。
"""
import base64
import io
import json
import os
import re
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request

try:
    from . import config as _cfg
except ImportError:  # 直接运行旧目录时兼容
    import config as _cfg


_VLM_TIMEOUT = 35
_VLM_CIRCUIT_THRESHOLD = 3
_VLM_CIRCUIT_COOLDOWN = 120
_vlm_state_lock = threading.Lock()
_vlm_failures = 0
_vlm_block_until = 0.0


def _vlm_circuit_open():
    with _vlm_state_lock:
        return time.monotonic() < _vlm_block_until


def _vlm_mark_success():
    global _vlm_failures, _vlm_block_until
    with _vlm_state_lock:
        _vlm_failures = 0
        _vlm_block_until = 0.0


def _vlm_mark_failure():
    global _vlm_failures, _vlm_block_until
    with _vlm_state_lock:
        _vlm_failures += 1
        if _vlm_failures >= _VLM_CIRCUIT_THRESHOLD:
            _vlm_block_until = time.monotonic() + _VLM_CIRCUIT_COOLDOWN

PROMPT = """Two images are provided.
Image 1: a 2x-enlarged icon-click CAPTCHA sprite. Its TOP 2/3 (the photo
area) is a photo overlaid with 4~9 small monochrome icons; each icon is a
solid silhouette (with internal cutouts) rendered in PURE WHITE or PURE
BLACK, upright (no rotation). Some icons are decoys. The BOTTOM 1/3 is an
instruction area — IGNORE it completely and rely on Image 2.
Image 2: the instruction strip: exactly 3 target icons (black solid
silhouettes on white) in the required click order, left to right.
IMPORTANT: a target icon may appear in the photo in the OPPOSITE color
(white instead of black). Match by SHAPE only, never by color.
Locate each of the 3 target icons in the photo area of Image 1 and report
its center as NORMALIZED coordinates: x and y are integers in 0..1000
relative to the FULL sprite image (x=1000 means the right edge, y=1000
means the bottom edge). Every point MUST lie in the photo area (y <= 660);
never report a point in the instruction area.
If you cannot confidently locate an icon, return {"clicks": []} rather
than guessing or repeating locations.
Reply with ONLY JSON: {"clicks": [[x1,y1],[x2,y2],[x3,y3]]}"""

PROMPT_FALLBACK = """One image is provided: a 2x-enlarged icon-click CAPTCHA
sprite. Its TOP 2/3 (the photo area) is a photo overlaid with 4~9
monochrome icons (pure white or pure black solid silhouettes, upright, no
rotation). Some icons are decoys.
The BOTTOM 1/3 contains FOUR instruction rows of the SAME 3 target icons:
ROW 1 (topmost row, black icons on white background) is the TRUE click
order, left to right; ROW 2 shows the same icons in REVERSED order;
ROWS 3 and 4 repeat rows 1 and 2 as white icons on black background.
Use ROW 1 ONLY, and NEVER report a point inside the instruction area.
IMPORTANT: a target icon may appear in the photo in the OPPOSITE color —
match by SHAPE only, never by color.
For each ROW 1 icon, left to right, locate the SAME icon in the photo area
and report its center as NORMALIZED coordinates: x and y are integers in
0..1000 relative to the FULL image (x=1000 = right edge, y=1000 = bottom
edge). Every point MUST lie in the photo area (y <= 660).
If you cannot confidently locate an icon, return {"clicks": []} rather
than guessing or repeating locations.
Reply with ONLY JSON: {"clicks": [[x1,y1],[x2,y2],[x3,y3]]}"""

HYBRID_PROMPT = """Four images are provided.
Image 1 has an annotated full CAPTCHA photo at the top and a candidate gallery
below. Red boxes and numbers identify candidates without covering their shape.
Every gallery tile repeats the same candidate as an ORIGINAL RGB crop on the
left and a COLOR-NORMALIZED black silhouette on white on the right.
Images 2, 3, 4 are the three target icons in the required click order. Each
target card contains one or more normalized renderings; horizontal-mirror
renderings are included because a target may be mirrored in the photo.
Match by SHAPE only. Ignore color, scale, JPEG noise, and horizontal mirroring.
Candidate numbers are spatial labels and are NOT related to target order.
Choose three DIFFERENT candidate numbers. Never invent an absent number. If
any target is uncertain, return an empty order instead of guessing.
Reply with ONLY JSON: {"order": [n1, n2, n3]}"""

HYBRID_PROMPT_STRIP = """Two images are provided.
Image 1: a photo overlaid with small monochrome icons (pure white or pure
black solid silhouettes, 20~45 px, upright), each marked with a thin red
circle and a number. A few marks may sit on background noise — ignore those.
Image 2: the instruction strip: exactly 3 target icons (black solid
silhouettes on white) in the required click order, left to right.
IMPORTANT: an icon in Image 1 may be the OPPOSITE color — match by SHAPE only.
For each of the 3 icons in Image 2, in left-to-right order, find the
numbered icon in Image 1 with the SAME shape and report its number.
Reply with ONLY JSON: {"order": [n1, n2, n3]}"""

HYBRID_PROMPT_RAW = """Four images are provided.
Image 1 has an annotated CAPTCHA photo and a numbered candidate gallery.
Images 2, 3, 4 are the ORIGINAL three target icon crops in required order.
Candidate labels are spatial IDs and are unrelated to target order. Match the
exact shape while ignoring black/white color, scale, JPEG noise, and possible
horizontal mirroring. Choose three DIFFERENT existing candidate numbers. If
uncertain, return an empty order. Reply ONLY JSON: {"order": [n1,n2,n3]}"""

HYBRID_PROMPT_SEMANTIC = """Four images are provided. Image 1 contains an
annotated CAPTCHA photo and a numbered candidate gallery. Images 2, 3, 4 are
the ORIGINAL target icons in required order. Work in two explicit stages:
first give each target and every candidate a short SINGLE canonical English
noun; then map identical shapes, allowing color inversion, scale change and
horizontal mirroring. For every selected pair, target_names[i] and
candidate_names[str(order[i])] MUST be exactly the same noun. Candidate IDs
are unrelated to target order. If a target has no matching candidate, return
an empty order instead of substituting a similar object. Reply ONLY JSON:
{"target_names":["noun1","noun2","noun3"],
"candidate_names":{"1":"noun",...},"order":[n1,n2,n3]}"""

# 页面内分割: canvas 解码 sprite -> 主图区(y<160)纯白/纯黑连通块 -> 各图标质心;
# 再在主图上叠加红色圆圈编号导出 320x160 PNG, 供 AI 只做语义命名无需猜坐标。
# 过滤参数 (面积>=150px, 宽高 12~48px) 来自 probe_hybrid PoC 实测。
SEG_JS = """async (b64) => {
  const img = new Image();
  img.src = 'data:image/jpeg;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = 320; c.height = 240;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, 320, 240).data;
  const mask = new Uint8Array(320 * 160);
  for (let y = 0; y < 160; y++) for (let x = 0; x < 320; x++) {
    const i = (y * 320 + x) * 4;
    const white = d[i] > 235 && d[i+1] > 235 && d[i+2] > 235;
    const black = d[i] < 60 && d[i+1] < 60 && d[i+2] < 60;
    if (white || black) mask[y * 320 + x] = 1;
  }
  function dilate(m, w, h) {
    const o = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      let v = 0;
      for (let dy = -1; dy <= 1 && !v; dy++)
        for (let dx = -1; dx <= 1 && !v; dx++) {
          const nx = x + dx, ny = y + dy;
          if (nx >= 0 && nx < w && ny >= 0 && ny < h && m[ny * w + nx]) v = 1;
        }
      o[y * w + x] = v;
    }
    return o;
  }
  function erode(m, w, h) {
    const o = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      let v = 1;
      for (let dy = -1; dy <= 1 && v; dy++)
        for (let dx = -1; dx <= 1 && v; dx++) {
          const nx = x + dx, ny = y + dy;
          if (nx < 0 || nx >= w || ny < 0 || ny >= h || !m[ny * w + nx]) v = 0;
        }
      o[y * w + x] = v;
    }
    return o;
  }
  const cmask = erode(dilate(mask, 320, 160), 320, 160);
  // 对比度过滤: 照片噪声块 (白毛/阴影) 也会过纯白/纯黑掩码,
  // 用图标灰度 vs 背景环灰度中位数差 >= 60 排除 (同 ASSESS_JS 校准)
  const gray = new Uint8Array(320 * 160);
  for (let y = 0; y < 160; y++) for (let x = 0; x < 320; x++) {
    const i = (y * 320 + x) * 4;
    gray[y * 320 + x] = (d[i] + d[i + 1] + d[i + 2]) / 3;
  }
  function median(a) { a.sort((x, y) => x - y); return a[(a.length / 2) | 0]; }
  function contrastOf(minx, maxx, miny, maxy) {
    const iv = [];
    for (let y = miny; y <= maxy; y++)
      for (let x = minx; x <= maxx; x++)
        if (cmask[y * 320 + x]) iv.push(gray[y * 320 + x]);
    if (!iv.length) return -1;
    const iconG = median(iv);
    const rv = [];
    const x1 = Math.max(0, minx - 5), x2 = Math.min(319, maxx + 5);
    const y1 = Math.max(0, miny - 5), y2 = Math.min(159, maxy + 5);
    for (let y = y1; y <= y2; y++) for (let x = x1; x <= x2; x++) {
      if (x >= minx && x <= maxx && y >= miny && y <= maxy) continue;
      const j = (y * 320 + x) * 4;
      const w2 = d[j] > 235 && d[j + 1] > 235 && d[j + 2] > 235;
      const b2 = d[j] < 60 && d[j + 1] < 60 && d[j + 2] < 60;
      if (!w2 && !b2) rv.push(gray[y * 320 + x]);
    }
    return rv.length >= 8 ? Math.abs(iconG - median(rv)) : 100;
  }
  const seen = new Uint8Array(320 * 160);
  const comps = [];
  for (let y = 0; y < 160; y++) for (let x = 0; x < 320; x++) {
    const s = y * 320 + x;
    if (!cmask[s] || seen[s]) continue;
    const q = [s]; seen[s] = 1;
    let px = 0, py = 0, pg = 0, n = 0;
    let minx = 320, maxx = 0, miny = 160, maxy = 0;
    while (q.length) {
      const t = q.pop();
      const tx = t % 320, ty = (t / 320) | 0;
      px += tx; py += ty; pg += gray[t]; n++;
      if (tx < minx) minx = tx; if (tx > maxx) maxx = tx;
      if (ty < miny) miny = ty; if (ty > maxy) maxy = ty;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        if (!dx && !dy) continue;
        const nx = tx + dx, ny = ty + dy;
        if (nx < 0 || nx >= 320 || ny < 0 || ny >= 160) continue;
        const nt = ny * 320 + nx;
        if (cmask[nt] && !seen[nt]) { seen[nt] = 1; q.push(nt); }
      }
    }
    const w = maxx - minx + 1, h = maxy - miny + 1;
    if (n >= 120 && w >= 10 && w <= 60 && h >= 12 && h <= 60
        && contrastOf(minx, maxx, miny, maxy) >= 60)
      comps.push({x: Math.round((minx + maxx) / 2),
                  y: Math.round((miny + maxy) / 2), n: n, w: w, h: h,
                  minx: minx, maxx: maxx, miny: miny, maxy: maxy,
                  polarity: pg / n >= 128 ? 'white' : 'black'});
  }
  const c2 = document.createElement('canvas');
  // 候选编号按主图 x 坐标排序，避免扫描顺序偶然形成 1,2,3 后让 VLM
  // 直接照抄序号；真实目标顺序与空间顺序无关。
  comps.sort((a, b) => a.x - b.x || a.y - b.y);
  c2.width = 320; c2.height = 160;
  const g2 = c2.getContext('2d');
  g2.drawImage(img, 0, 0, 320, 160, 0, 0, 320, 160);
  g2.font = 'bold 14px sans-serif';
  comps.forEach((p, i) => {
    g2.strokeStyle = '#f00'; g2.lineWidth = 1;
    g2.beginPath(); g2.arc(p.x, p.y, 15, 0, 7); g2.stroke();
    g2.font = 'bold 12px sans-serif';
    g2.strokeStyle = '#fff'; g2.lineWidth = 3;
    g2.strokeText(String(i + 1), Math.min(306, p.x + 13), Math.max(12, p.y - 13));
    g2.fillStyle = '#f00';
    g2.fillText(String(i + 1), Math.min(306, p.x + 13), Math.max(12, p.y - 13));
  });
  return {comps: comps, marked: c2.toDataURL('image/png')};
}"""


# 本地模板匹配求解器 (页面内执行, canvas 解码 sprite, 无需外部依赖)
#
# 算法 (行1/行3 黑白双行互检, 严格置信, 宁缺毋滥):
#  1. sprite 320x240: 上 160px 主图; 底部四排指令区 (每排 20px):
#     行1 黑图标白底=真实点击顺序, 行2=行1 反序, 行3 白图标黑底=行1
#     同序, 行4=行3 反序 (每个目标图标共 4 个渲染)。
#  2. 指令行按"单色阈值"分割 (旧版纯白/纯黑都当前景, 与纯白行背景
#     粘连成巨型连通块, 是长期 0 命中的根因): 行1 取黑 (gray<60),
#     行3 取白 (gray>200); close 后取连通块, 相邻部件合并至恰好 3 个。
#  3. 行1[i] 与行3[i] 是同一图标 (13 同序): 填洞归一化剪影 IoU 互检,
#     验证模板分割质量。
#  4. 主图候选: 纯白|纯黑掩码 -> close -> 连通块过滤 (面积/尺寸)。
#  5. 全局最优分配: 每目标先算行1/行3 双模板对全部候选的平均 IoU
#     (黑白双行证据融合), 再全排列 (3 目标互斥, 候选<=24 时千级
#     排列毫秒完成) 取总分最高分配; 要求排列内最弱匹配 IoU
#     >= min_iou 且最优/次优排列总分差 >= min_gap (区分度)。
#     不满足即整体失败 (上层转 AI)。
#  6. 匹配块包围盒中心即点击坐标 (像素级精确)。
SOLVE_JS = r"""async (args) => {
  const b64 = args.b64;
  const MIN_IOU = args.min_iou || 0.45;
  const MIN_GAP = args.min_gap || 0.06;
  const MIN_TWIN = args.min_twin || 0.70;
  const W = 320, HM = 160, HA = 240;
  try {
    const img = new Image();
    img.src = 'data:image/jpeg;base64,' + b64;
    await img.decode();
    if (img.width !== W || img.height !== HA)
      return { ok: false, err: 'sprite 尺寸异常 ' + img.width + 'x' + img.height };
    const cv = document.createElement('canvas');
    cv.width = W; cv.height = HA;
    const g = cv.getContext('2d');
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, W, HA).data;
    const gray = new Uint8Array(W * HA);
    for (let i = 0; i < W * HA; i++) {
      const j = i * 4;
      gray[i] = (d[j] + d[j + 1] + d[j + 2]) / 3;
    }
    // 形态学 close (dilate+erode, 3x3): 抗 JPEG 压缩破碎
    function dilate(m, w, h) {
      const o = new Uint8Array(w * h);
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        let v = 0;
        for (let dy = -1; dy <= 1 && !v; dy++)
          for (let dx = -1; dx <= 1 && !v; dx++) {
            const nx = x + dx, ny = y + dy;
            if (nx >= 0 && nx < w && ny >= 0 && ny < h && m[ny * w + nx]) v = 1;
          }
        o[y * w + x] = v;
      }
      return o;
    }
    function erode(m, w, h) {
      const o = new Uint8Array(w * h);
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        let v = 1;
        for (let dy = -1; dy <= 1 && v; dy++)
          for (let dx = -1; dx <= 1 && v; dx++) {
            const nx = x + dx, ny = y + dy;
            if (nx < 0 || nx >= w || ny < 0 || ny >= h || !m[ny * w + nx]) v = 0;
          }
        o[y * w + x] = v;
      }
      return o;
    }
    function argmax(a) {
      let bi = 0;
      for (let k = 1; k < a.length; k++) if (a[k] > a[bi]) bi = k;
      return bi;
    }
    // (argmax 供调试用途保留)
    // 通用 8 邻域连通块标记 (m 为 w*h 一维掩码)
    function label(m, w, h) {
      const seen = new Uint8Array(w * h);
      const out = [];
      for (let s = 0; s < w * h; s++) {
        if (!m[s] || seen[s]) continue;
        const q = [s]; seen[s] = 1;
        let n = 0, px = 0, py = 0, minx = w, maxx = 0, miny = h, maxy = 0;
        while (q.length) {
          const t = q.pop();
          const tx = t % w, ty = (t / w) | 0;
          n++; px += tx; py += ty;
          if (tx < minx) minx = tx; if (tx > maxx) maxx = tx;
          if (ty < miny) miny = ty; if (ty > maxy) maxy = ty;
          for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
            if (!dx && !dy) continue;
            const nx = tx + dx, ny = ty + dy;
            if (nx < 0 || nx >= w || ny < 0 || ny >= h) continue;
            const nt = ny * w + nx;
            if (m[nt] && !seen[nt]) { seen[nt] = 1; q.push(nt); }
          }
        }
        out.push({ n: n, minx: minx, maxx: maxx, miny: miny, maxy: maxy,
                   cx: px / n, cy: py / n });
      }
      return out;
    }
    // 签名: bbox 子掩码直接重采样 (保留内部镂空 —— 填洞会把
    // 蝴蝶/洗衣机/提包都变实心方块丧失判别力, 是匹配混乱根因;
    // 图标本就实心带镂空, 镂空图案是关键判别特征)
    const N = 32;
    function sigFrom(m, w, c) {
      const bw = c.maxx - c.minx + 1, bh = c.maxy - c.miny + 1;
      const r = new Uint8Array(N * N);
      for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) {
        const x = Math.min(bw - 1, ((i + 0.5) * bw / N) | 0);
        const y = Math.min(bh - 1, ((j + 0.5) * bh / N) | 0);
        r[j * N + i] = m[(c.miny + y) * w + (c.minx + x)];
      }
      return r;
    }
    function iou(a, b) {
      let inter = 0, union = 0;
      for (let k = 0; k < a.length; k++) {
        if (a[k] || b[k]) union++;
        if (a[k] && b[k]) inter++;
      }
      return union ? inter / union : 0;
    }
    function shapeIou(a, b) {
      // 主图图标可能水平翻转；模板互检仍用原始 iou，候选匹配取
      // 正常/水平镜像两种形态的较高分。
      let direct = iou(a, b), inter = 0, union = 0;
      for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) {
        const av = a[y * N + (N - 1 - x)], bv = b[y * N + x];
        if (av || bv) union++;
        if (av && bv) inter++;
      }
      const mirrored = union ? inter / union : 0;
      return Math.max(direct, mirrored);
    }
    // ---- 指令行模板 (单色阈值: 行1 黑图标白底, 行3 白图标黑底) ----
    function stripRow(y0, y1, whiteMode) {
      const h = y1 - y0;
      const m = new Uint8Array(W * h);
      for (let y = y0; y < y1; y++) for (let x = 0; x < W; x++) {
        const gv = gray[y * W + x];
        if (whiteMode ? gv > 200 : gv < 60) m[(y - y0) * W + x] = 1;
      }
      // 指令行背景干净不做 close: dilate 会把小模板的镂空填掉,
      // 模板变实心方块丧失判别力 (蝴蝶/洗衣机/提包混淆根因)
      const mm = m;
      let cs = label(mm, W, h).filter(cc => {
        const w = cc.maxx - cc.minx + 1, hh = cc.maxy - cc.miny + 1;
        return cc.n >= 25 && w >= 6 && w <= 52 && hh >= 6 && hh <= 52;
      }).sort((a, b) => a.minx - b.minx);
      while (cs.length > 3) {  // 相邻部件合并 (间距最小者优先), 直至恰好 3 个
        let bi = 0, bg = 1e9;
        for (let i = 0; i < cs.length - 1; i++) {
          const gap = cs[i + 1].minx - cs[i].maxx;
          if (gap < bg) { bg = gap; bi = i; }
        }
        const a = cs[bi], b2 = cs[bi + 1];
        cs.splice(bi, 2, { n: a.n + b2.n,
          minx: Math.min(a.minx, b2.minx), maxx: Math.max(a.maxx, b2.maxx),
          miny: Math.min(a.miny, b2.miny), maxy: Math.max(a.maxy, b2.maxy) });
      }
      return cs.length === 3 ? { m: mm, cs: cs } : null;
    }
    const r1 = stripRow(160, 180, false);
    if (!r1) return { ok: false, err: '行1 (黑图标) 模板分割异常' };
    const r3 = stripRow(200, 220, true);
    if (!r3) return { ok: false, err: '行3 (白图标) 模板分割异常' };
    const t1s = r1.cs.map(c => sigFrom(r1.m, W, c));
    const t3s = r3.cs.map(c => sigFrom(r3.m, W, c));
    // 行1[i] 与行3[i] 是同一图标 (13 同序): 分割质量互检
    const twin = [];
    for (let i = 0; i < 3; i++) {
      const s = iou(t1s[i], t3s[i]);
      twin.push(+s.toFixed(3));
      if (s < MIN_TWIN)
        return { ok: false, err: '行1/行3 同位互检失败 #' + (i + 1)
          + ': IoU=' + s.toFixed(2) + ' < ' + MIN_TWIN };
    }
    // ---- 主图候选 (RGB 三通道纯白/纯黑掩码, close 抗破碎) ----
    // 注: 不能用灰度判定 —— 照片背景暗彩区 (如深红) 灰度可 <60,
    // 会被误掩进来与图标粘连成巨型块而被滤掉, 实测候选数锐减
    const mm = new Uint8Array(W * HM);
    for (let y = 0; y < HM; y++) for (let x = 0; x < W; x++) {
      const j = (y * W + x) * 4;
      const white = d[j] > 235 && d[j + 1] > 235 && d[j + 2] > 235;
      const black = d[j] < 60 && d[j + 1] < 60 && d[j + 2] < 60;
      if (white || black) mm[y * W + x] = 1;
    }
    const mc = erode(dilate(mm, W, HM), W, HM);
    // 对比度过滤: 照片噪声块 (白毛/阴影) 也过纯白/纯黑掩码,
    // 图标灰度 vs 背景环灰度中位数差 >= 60 才保留 (同 ASSESS_JS 校准)
    function median(a) { a.sort((x, y) => x - y); return a[(a.length / 2) | 0]; }
    function contrastOf(c) {
      const iv = [];
      for (let y = c.miny; y <= c.maxy; y++)
        for (let x = c.minx; x <= c.maxx; x++)
          if (mc[y * W + x]) iv.push(gray[y * W + x]);
      if (!iv.length) return -1;
      const iconG = median(iv);
      const rv = [];
      const x1 = Math.max(0, c.minx - 5), x2 = Math.min(W - 1, c.maxx + 5);
      const y1 = Math.max(0, c.miny - 5), y2 = Math.min(HM - 1, c.maxy + 5);
      for (let y = y1; y <= y2; y++) for (let x = x1; x <= x2; x++) {
        if (x >= c.minx && x <= c.maxx && y >= c.miny && y <= c.maxy) continue;
        const j = (y * W + x) * 4;
        const w2 = d[j] > 235 && d[j + 1] > 235 && d[j + 2] > 235;
        const b2 = d[j] < 60 && d[j + 1] < 60 && d[j + 2] < 60;
        if (!w2 && !b2) rv.push(gray[y * W + x]);
      }
      return rv.length >= 8 ? Math.abs(iconG - median(rv)) : 100;
    }
    const mainComps = label(mc, W, HM).filter(c => {
      const w = c.maxx - c.minx + 1, h = c.maxy - c.miny + 1;
      // n/w/h 下限收紧: 滤小碎片 (碎片填洞后近矩形, 会与任意模板
      // 万能匹配, 实测 n~99 碎片 IoU 可达 0.7+)
      return c.n >= 120 && w >= 10 && w <= 60 && h >= 12 && h <= 60
        && contrastOf(c) >= 60;
    }).sort((a, b) => b.n - a.n).slice(0, 24);
    if (mainComps.length < 3)
      return { ok: false, err: '主图候选图标不足 3: ' + mainComps.length };
    const candSigs = mainComps.map(c => sigFrom(mc, W, c));
    // ---- 全局最优分配 + 严格置信 (低置信整体失败, 由上层转 AI) ----
    // 每目标行1/行3 双模板对全部候选的平均 IoU (黑白双行证据融合),
    // 全排列取总分最高分配; 弱匹配/总分差不足均放弃。
    const avg = [0, 1, 2].map(i => {
      const s1 = candSigs.map(sg => shapeIou(t1s[i], sg));
      const s3 = candSigs.map(sg => shapeIou(t3s[i], sg));
      return s1.map((v, k) => (v + s3[k]) / 2);
    });
    const nc = candSigs.length;
    let bestPerm = null, bestT = -1, secondT = -1;
    for (let a = 0; a < nc; a++) for (let b = 0; b < nc; b++) {
      if (b === a) continue;
      for (let c = 0; c < nc; c++) {
        if (c === a || c === b) continue;
        const t = avg[0][a] + avg[1][b] + avg[2][c];
        if (t > bestT) { secondT = bestT; bestT = t; bestPerm = [a, b, c]; }
        else if (t > secondT) secondT = t;
      }
    }
    if (!bestPerm) return { ok: false, err: '全排列分配失败' };
    const weak = Math.min(avg[0][bestPerm[0]], avg[1][bestPerm[1]],
                          avg[2][bestPerm[2]]);
    if (weak < MIN_IOU)
      return { ok: false, err: '最优分配存在弱匹配: 最弱 IoU='
        + weak.toFixed(2) + ' < ' + MIN_IOU };
    const permGap = bestT - secondT;
    if (permGap < MIN_GAP)
      return { ok: false, err: '分配方案区分度不足: 总分差='
        + permGap.toFixed(2) + ' < ' + MIN_GAP };
    const picks = bestPerm.map((ci, i) => ({ i: i, ci: ci, score: avg[i][ci] }));
    const clicks = picks.map(p => {
      const c = mainComps[p.ci];
      return [Math.round((c.minx + c.maxx) / 2), Math.round((c.miny + c.maxy) / 2)];
    });
    // ---- 调试标注图 (黄=行1 模板, 青=行3 模板, 绿框+红圈=命中) ----
    const dc = document.createElement('canvas');
    dc.width = W; dc.height = HA;
    const dg = dc.getContext('2d');
    dg.drawImage(img, 0, 0);
    dg.lineWidth = 1.5;
    dg.strokeStyle = '#fc0';
    r1.cs.forEach(c => dg.strokeRect(c.minx, c.miny + 160,
                                     c.maxx - c.minx, c.maxy - c.miny));
    dg.strokeStyle = '#0cf';
    r3.cs.forEach(c => dg.strokeRect(c.minx, c.miny + 200,
                                     c.maxx - c.minx, c.maxy - c.miny));
    picks.forEach(p => {
      const c = mainComps[p.ci], x = clicks[p.i][0], y = clicks[p.i][1];
      dg.strokeStyle = '#0c0';
      dg.strokeRect(c.minx, c.miny, c.maxx - c.minx, c.maxy - c.miny);
      dg.strokeStyle = '#f00';
      dg.beginPath(); dg.arc(x, y, 12, 0, 7); dg.stroke();
      dg.fillStyle = '#f00'; dg.font = 'bold 14px sans-serif';
      dg.fillText(String(p.i + 1), Math.min(W - 12, x + 10), Math.max(13, y - 10));
      dg.fillText(p.score.toFixed(2),
                  c.minx, Math.max(12, c.miny - 3));
    });
    return {
      ok: true, clicks: clicks, twin: twin,
      scores: picks.map(p => +p.score.toFixed(3)),
      perm_gap: +permGap.toFixed(3), weak: +weak.toFixed(3),
      templates: r1.cs.map(c => [c.minx, c.miny + 160, c.maxx, c.maxy + 160]),
      marked: dc.toDataURL('image/png')
    };
  } catch (e) {
    return { ok: false, err: 'JS 异常: ' + (e && e.message || e) };
  }
}"""


# 难度预检 (页面内执行, 亚秒级): 预测本地/AI 识别成功率, 难图直接刷新换图
#
# 指标 (probe_assess.py 11 张历史样本校准):
#   - 指令行结构: 行1/行3 分割数须恰好 3 (异常 => AI 读指令易错)
#   - 主图候选对比度 (同色伪装检测): 图标灰度 (bbox 内掩码像素中位数)
#     vs 背景环灰度 (bbox 外扩 5px, 排除纯白/纯黑像素取中位数)。
#     真实图标 contrast 100~180 (iconG≈0-10 或 ≈250);
#     同色伪装/噪声块 contrast<60 (iconG 15~50)
#   - n_valid = contrast>=min_contrast 的候选数 (<3 => 目标可能被伪装)
ASSESS_JS = r"""async (args) => {
  const b64 = args.b64, MIN_C = args.min_contrast || 60;
  const W = 320, HA = 240;
  try {
    const img = new Image();
    img.src = 'data:image/jpeg;base64,' + b64;
    await img.decode();
    if (img.width !== W || img.height !== HA)
      return { ok: false, err: 'sprite 尺寸异常 ' + img.width + 'x' + img.height };
    const c = document.createElement('canvas');
    c.width = W; c.height = HA;
    const g = c.getContext('2d');
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, W, HA).data;
    const gray = new Uint8Array(W * HA);
    const mask = new Uint8Array(W * 160);
    for (let y = 0; y < HA; y++) for (let x = 0; x < W; x++) {
      const i = y * W + x, j = i * 4;
      gray[i] = (d[j] + d[j + 1] + d[j + 2]) / 3;
      if (y < 160) {
        const white = d[j] > 235 && d[j + 1] > 235 && d[j + 2] > 235;
        const black = d[j] < 60 && d[j + 1] < 60 && d[j + 2] < 60;
        if (white || black) mask[i] = 1;
      }
    }
    function dilate(m, w, h) {
      const o = new Uint8Array(w * h);
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        let v = 0;
        for (let dy = -1; dy <= 1 && !v; dy++)
          for (let dx = -1; dx <= 1 && !v; dx++) {
            const nx = x + dx, ny = y + dy;
            if (nx >= 0 && nx < w && ny >= 0 && ny < h && m[ny * w + nx]) v = 1;
          }
        o[y * w + x] = v;
      }
      return o;
    }
    function erode(m, w, h) {
      const o = new Uint8Array(w * h);
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        let v = 1;
        for (let dy = -1; dy <= 1 && v; dy++)
          for (let dx = -1; dx <= 1 && v; dx++) {
            const nx = x + dx, ny = y + dy;
            if (nx < 0 || nx >= w || ny < 0 || ny >= h || !m[ny * w + nx]) v = 0;
          }
        o[y * w + x] = v;
      }
      return o;
    }
    function label(m, w, h) {
      const seen = new Uint8Array(w * h);
      const out = [];
      for (let s = 0; s < w * h; s++) {
        if (!m[s] || seen[s]) continue;
        const q = [s]; seen[s] = 1;
        let n = 0, minx = w, maxx = 0, miny = h, maxy = 0;
        while (q.length) {
          const t = q.pop();
          const tx = t % w, ty = (t / w) | 0;
          n++;
          if (tx < minx) minx = tx; if (tx > maxx) maxx = tx;
          if (ty < miny) miny = ty; if (ty > maxy) maxy = ty;
          for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
            if (!dx && !dy) continue;
            const nx = tx + dx, ny = ty + dy;
            if (nx < 0 || nx >= w || ny < 0 || ny >= h) continue;
            const nt = ny * w + nx;
            if (m[nt] && !seen[nt]) { seen[nt] = 1; q.push(nt); }
          }
        }
        out.push({n: n, minx: minx, maxx: maxx, miny: miny, maxy: maxy});
      }
      return out;
    }
    function median(a) {
      if (!a.length) return -1;
      a.sort((x, y) => x - y);
      return a[(a.length / 2) | 0];
    }
    // ---- 主图候选 (close 后连通块) ----
    const mc = erode(dilate(mask, W, 160), W, 160);
    const comps = label(mc, W, 160).filter(cc => {
      const w = cc.maxx - cc.minx + 1, h = cc.maxy - cc.miny + 1;
      return cc.n >= 120 && w >= 10 && w <= 60 && h >= 12 && h <= 60;
    }).sort((a, b) => b.n - a.n).slice(0, 24);
    const icons = [];
    for (const cc of comps) {
      // 图标灰度: bbox 内掩码像素的中位数
      const iv = [];
      for (let y = cc.miny; y <= cc.maxy; y++)
        for (let x = cc.minx; x <= cc.maxx; x++)
          if (mc[y * W + x]) iv.push(gray[y * W + x]);
      const iconG = median(iv);
      // 背景环: bbox 外扩 5px, 排除纯白/纯黑 (其他图标), 取中位数
      const rv = [];
      const x1 = Math.max(0, cc.minx - 5), x2 = Math.min(W - 1, cc.maxx + 5);
      const y1 = Math.max(0, cc.miny - 5), y2 = Math.min(159, cc.maxy + 5);
      for (let y = y1; y <= y2; y++) for (let x = x1; x <= x2; x++) {
        if (x >= cc.minx && x <= cc.maxx && y >= cc.miny && y <= cc.maxy) continue;
        const j = (y * W + x) * 4;
        const white = d[j] > 235 && d[j + 1] > 235 && d[j + 2] > 235;
        const black = d[j] < 60 && d[j + 1] < 60 && d[j + 2] < 60;
        if (!white && !black) rv.push(gray[y * W + x]);
      }
      const ringG = rv.length >= 8 ? median(rv) : -1;
      const contrast = ringG >= 0 ? Math.abs(iconG - ringG) : -1;
      // 贴边候选: bbox 触碰主图边界, 疑似图标被裁切 (不完整 ->
      // 本地形状匹配失真 + AI 点击中心偏移, 判难刷新)
      const edge = (cc.miny <= 1 || cc.maxy >= 158
                    || cc.minx <= 1 || cc.maxx >= 318) ? 1 : 0;
      icons.push({x: (cc.minx + cc.maxx) / 2 | 0, y: (cc.miny + cc.maxy) / 2 | 0,
                  iconG: iconG, ringG: ringG, contrast: contrast,
                  edge: edge});
    }
    const n_valid = icons.filter(ic => ic.contrast >= MIN_C).length;
    // ---- 行1/行3 模板分割计数 ----
    function stripCount(y0, y1, whiteMode) {
      const h = y1 - y0;
      const m = new Uint8Array(W * h);
      for (let y = y0; y < y1; y++) for (let x = 0; x < W; x++) {
        const gv = gray[y * W + x];
        if (whiteMode ? gv > 200 : gv < 60) m[(y - y0) * W + x] = 1;
      }
      const mm2 = erode(dilate(m, W, h), W, h);
      let cs = label(mm2, W, h).filter(cc => {
        const w = cc.maxx - cc.minx + 1, hh = cc.maxy - cc.miny + 1;
        return cc.n >= 25 && w >= 6 && w <= 52 && hh >= 6 && hh <= 52;
      }).sort((a, b) => a.minx - b.minx);
      while (cs.length > 3) {
        let bi = 0, bg = 1e9;
        for (let i = 0; i < cs.length - 1; i++) {
          const gap = cs[i + 1].minx - cs[i].maxx;
          if (gap < bg) { bg = gap; bi = i; }
        }
        const a = cs[bi], b2 = cs[bi + 1];
        cs.splice(bi, 2, {n: a.n + b2.n,
          minx: Math.min(a.minx, b2.minx), maxx: Math.max(a.maxx, b2.maxx),
          miny: Math.min(a.miny, b2.miny), maxy: Math.max(a.maxy, b2.maxy)});
      }
      return cs.length;
    }
    const strip1 = stripCount(160, 180, false);
    const strip3 = stripCount(200, 220, true);
    const n_edge = icons.filter(ic => ic.edge).length;
    return { ok: true, icons: icons, n_cands: comps.length,
             n_valid: n_valid, n_low: icons.length - n_valid,
             n_edge: n_edge,
             strip1: strip1, strip3: strip3 };
  } catch (e) {
    return { ok: false, err: 'JS 异常: ' + (e && e.message || e) };
  }
}"""


# 点击墨迹评分: 实际评分已改 PIL 本地复刻 (见 _ink_scores), 本 JS 仅保留
# 供 probe/回放参考 —— 嵌入式 WebView2 的 ExecuteScriptAsync 不等待
# Promise (实测 async 脚本返回 '{}'), 页面内 async 验证在工具内永远不可用
# (2026-08-27 探针实证, 是"AI 答案全部判空想"一轮 10 连弃用的直接原因)。
# 验证 AI 给出的点击点是否真的落在图标上。
# 照片区图标是"混色压印" (半透明叠加到照片上, 灰度介于背景之间),
# 纯白/纯黑阈值会整片漏检 (实测 10 张样本 0 命中); 而图标局部对比度
# 强: 以点为中心半径 8 的 patch 内, |像素灰度 - patch 中位灰度| >= 60
# 的"墨迹"像素数, 图标上实测 40~245, 干净背景 0~20, 判别力稳定。
VALIDATE_JS = """async (a) => {
  const img = new Image();
  img.src = 'data:image/jpeg;base64,' + a.b64;
  await img.decode();
  const W = 320, HA = 240, HM = 160;
  const c = document.createElement('canvas');
  c.width = W; c.height = HA;
  const g = c.getContext('2d');
  // 必须 1:1 全图解码后只取 y<160: 5 参 drawImage(img,0,0,W,HM)
  // 会把整张 sprite 压进 320x160 画布, 照片区被纵向压缩 2/3,
  // 墨迹分对不上任何真实图标 (一次全假阴性事故的根因)
  g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, W, HA).data;
  const gray = new Uint8Array(W * HM);
  for (let y = 0; y < HM; y++) for (let x = 0; x < W; x++) {
    const i = y * W + x, j = i * 4;
    gray[i] = Math.round((d[j] + d[j + 1] + d[j + 2]) / 3);
  }
  const ink = (cx, cy) => {
    const R = 8;
    const vals = [];
    for (let y = Math.max(0, Math.round(cy) - R);
         y <= Math.min(HM - 1, Math.round(cy) + R); y++)
      for (let x = Math.max(0, Math.round(cx) - R);
           x <= Math.min(W - 1, Math.round(cx) + R); x++)
        vals.push(gray[y * W + x]);
    vals.sort((p, q) => p - q);
    const med = vals[vals.length >> 1];
    let n = 0;
    for (const v of vals) if (Math.abs(v - med) >= 60) n++;
    return n;
  };
  return (a.pts || []).map(p => ink(+p[0], +p[1]));
}"""


def assess(page, sprite_bytes, log=None, min_contrast=60):
    """难度预检 (亚秒级), 返回 {ok, n_valid, n_low, n_edge, strip1, strip3, reason}

    判"难"标准 (任一命中即建议刷新换图):
      - 指令行结构异常 (行1/行3 分割数 != 3): 本地/AI 读指令都易错
      - 高对比有效图标 < 3: 目标图标可能被同色伪装
      - 低对比伪装/噪声块多于有效块: 整体干扰过重
    贴边候选仅记录观测 (n_edge), 不再判难: 实测该站点图标常贴边,
    旧规则导致连刷 4 次换不到非贴边图白白耗时, 且 AI 对裁切图标仍能定位。
    预检自身异常时放行 (fail-open), 不阻塞主流程。
    """
    b64 = base64.b64encode(sprite_bytes).decode()
    try:
        res = page.evaluate(
            ASSESS_JS, {'b64': b64, 'min_contrast': min_contrast})
    except Exception as e:
        if log:
            log(f'[captcha] 预检执行异常 (放行): {e}')
        return {'ok': True, 'reason': '预检异常放行'}
    if not isinstance(res, dict) or not res.get('ok'):
        if log:
            log(f'[captcha] 预检无有效结果 (放行): {res}')
        return {'ok': True, 'reason': '预检异常放行'}
    hard = []
    if res.get('strip1') != 3 or res.get('strip3') != 3:
        hard.append(f"指令行结构异常 行1={res.get('strip1')} "
                    f"行3={res.get('strip3')}")
    if res.get('n_valid', 0) < 3:
        hard.append(f"有效图标不足({res.get('n_valid')})")
    if res.get('n_low', 0) > res.get('n_valid', 0):
        hard.append(f"低对比块({res.get('n_low')})多于有效块")
    out = {'ok': not hard,
           'n_valid': res.get('n_valid'), 'n_low': res.get('n_low'),
           'n_edge': res.get('n_edge'), 'n_cands': res.get('n_cands'),
           'strip1': res.get('strip1'), 'strip3': res.get('strip3')}
    if hard:
        out['reason'] = '; '.join(hard)
    return out


def local_solve(page, sprite_bytes, log=None, min_iou=0.45,
                min_gap=0.06, min_twin=0.70, save_debug=True):
    """本地模板匹配求解 (行1/行3 黑白双行互检), 返回 [(x,y)*3] 或 None

    在页面 canvas 内完成分割/填洞/归一化 IoU 双行互检匹配, 亚秒级。
    严格置信: 任一目标不满足 (双行指向一致/IoU/gap) 即整体放弃,
    失败返回 None, 由上层回退混合方案/VLM。
    """
    b64 = base64.b64encode(sprite_bytes).decode()
    try:
        res = page.evaluate(SOLVE_JS,
                            {'b64': b64, 'min_iou': min_iou,
                             'min_gap': min_gap, 'min_twin': min_twin})
    except Exception as e:
        if log:
            log(f'[captcha] 本地模板匹配执行异常: {e}')
        return None
    if not isinstance(res, dict):
        return None
    if not res.get('ok'):
        if log:
            log(f"[captcha] 本地模板匹配未命中: {res.get('err')}")
        return None
    clicks = [(int(p[0]), int(p[1])) for p in res['clicks']]
    if not all(0 <= x <= 320 and 0 <= y <= 160 for x, y in clicks):
        if log:
            log('[captcha] 本地匹配坐标越界, 弃用')
        return None
    if save_debug:
        try:
            marked = base64.b64decode(res['marked'].split(',', 1)[1])
            p = os.path.join(_cfg.BASE, 'captcha_cache',
                             time.strftime('solve_%Y%m%d_%H%M%S') + '.png')
            with open(p, 'wb') as f:
                f.write(marked)
        except Exception:
            pass
    if log:
        log(f"[captcha] 本地模板匹配命中: IoU={res.get('scores')} "
            f"最弱={res.get('weak')} 总分差={res.get('perm_gap')} "
            f"行1/行3互检={res.get('twin')}")
    return clicks


def _save_sprite(sprite_bytes):
    """验证码图存 captcha_cache/ 供日志追溯, 只保留最近 10 张"""
    d = os.path.join(_cfg.BASE, 'captcha_cache')
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, time.strftime('captcha_%Y%m%d_%H%M%S') + '.jpg')
    with open(p, 'wb') as f:
        f.write(sprite_bytes)
    try:
        files = sorted(x for x in os.listdir(d) if x.startswith('captcha_'))
        for old in files[:-10]:
            os.remove(os.path.join(d, old))
    except OSError:
        pass
    return p


def crop_row1(sprite_bytes):
    """裁剪 sprite 指令行1 (真实点击顺序, 黑图标白底) 作为 AI 顺序源兜底

    页面指令条截图 (tip) 缺失时的次优顺序源: 裁 sprite y[158,182)
    并 2x 放大, 比整图更聚焦; 行2 顶部最多带入 2px 噪声边缘。
    返回 PNG bytes 或 None。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(sprite_bytes))
        if img.size != (320, 240):
            return None
        row = img.crop((0, 158, 320, 182)).resize((640, 48), Image.LANCZOS)
        buf = io.BytesIO()
        row.save(buf, format='PNG')
        return buf.getvalue()
    except Exception:
        return None


def _upscale_sprite(sprite_bytes, scale=2):
    """送 AI 前 2x 放大: 原图 320x240 下图标仅 20~45px, 低于 VLM 有效
    分辨率, 形状判别困难; 坐标采用全图 0-1000 归一化与送图分辨率无关,
    放大只提升清晰度不影响换算"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(sprite_bytes))
        img = img.resize((img.width * scale, img.height * scale),
                         Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=92)
        return buf.getvalue()
    except Exception:
        return sprite_bytes


class VlmUnavailableError(RuntimeError):
    """视觉模型服务当前不可用，应立即降级到人工点选。"""


def _vlm_call(sprite_bytes, cfg, timeout=_VLM_TIMEOUT, log=None, tip_bytes=None):
    if _vlm_circuit_open():
        raise VlmUnavailableError('视觉模型连续失败，已暂时熔断')
    up = _upscale_sprite(sprite_bytes)
    b64 = base64.b64encode(up).decode()
    content = [{'type': 'text',
                'text': PROMPT if tip_bytes else PROMPT_FALLBACK},
               {'type': 'image_url', 'image_url': {
                   'url': f'data:image/jpeg;base64,{b64}'}}]
    if tip_bytes:
        content.append({'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,'
                   + base64.b64encode(tip_bytes).decode()}})
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'user', 'content': content}],
        'temperature': 0,
    }
    # 百炼 qwen3 系列为混合思考模型: 显式关思考保快速直答;
    # 非 qwen3 模型 (qwen-vl-max 等) 不支持该参数, 不传
    if 'qwen3' in cfg['model']:
        payload['enable_thinking'] = False
        payload['response_format'] = {'type': 'json_object'}
        payload['max_completion_tokens'] = 256
    endpoint = cfg['base'].rstrip('/') + '/chat/completions'
    if log:
        log(f"[vlm] POST {endpoint} model={cfg['model']} "
            f"图1={len(up)}B(2x sprite)"
            + (f' 指令条={len(tip_bytes)}B' if tip_bytes else ''))
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + cfg['key'],
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read()[:600].decode('utf-8', 'replace')
        except Exception:
            body = ''
        if log:
            log(f'[vlm] HTTP {e.code} {endpoint}: {body or e.reason}')
        _vlm_mark_failure()
        raise VlmUnavailableError(
            f'HTTP {e.code}: {body or e.reason}') from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if log:
            log(f'[vlm] 连接失败 {endpoint}: {e}')
        _vlm_mark_failure()
        raise VlmUnavailableError(str(e)) from e
    _vlm_mark_success()
    msg = data['choices'][0]['message']
    content = msg.get('content') or ''
    if log:
        reasoning = msg.get('reasoning_content') or ''
        if reasoning:
            log(f'[captcha] AI 思考过程: {str(reasoning)[:800]}')
        log(f'[captcha] AI 回复: {content[:1200]}')
    return content


def vlm_solve(sprite_bytes, cfg, tries=2, log=None, tip_bytes=None,
              page=None):
    """端到端 VLM 图标点选, 返回主图坐标 [(x,y)*3] 或 None

    实测 (2026-08-27, qwen-vl-max) AI 坐标参考系不稳定: 时而按提示词
    输出 0-1000 归一化, 时而直接按送图 (2x 放大后 640x480) 像素坐标
    作答, 时而对看不清的图输出近乎重复的"空想"坐标。单一归一化换算
    会把像素帧答案系统性压到错误位置 (x 误差达 36%, 图标半径级),
    是连续 10 次「点击位置不对」的直接原因。本版改造:
      1. 顺序源: 页面指令条截图 (单图标 2x 放大, 最清晰) > sprite
         行1 裁剪 (2x, 次之) > 整图自读 (兜底); 旧逻辑优先行1 导致
         页面指令条永远用不上。
      2. 双帧仲裁: 同一份答案按像素帧 (x/2, y/2) 与归一化帧
         (x*320/1000, y*240/1000) 各换算一次, 只保留全点落在主图区
         的帧。
      3. 指令区预检 (_strip_answer): 原始答案在两参考系下全点落在
         指令行带 y[158,240) = 模型报的是指令行图标自身坐标
         (单图模式最常见空想形态), 帧换算前直接弃用, 防止被归一化
         帧洗白进主图区顶部。
      4. 墨迹验证: 两帧由 _pick_frame 按 VALIDATE_JS 同公式在 PIL
         本地打分 (点周围 17x17 patch 内 |灰度-中位|>=60 的像素数;
         页面内 JS 版在嵌入式 WebView2 下不可用, 见 VALIDATE_JS 注
         释), 取"最弱分>=12 且中位分>=25 且点间距>=18px"的一帧;
         双过取总分高者。全部不过 = 模型空想, 不点击直接弃用, 避免
         无谓消耗尝试并触发站点「失败过多」锁态。
      5. 每次尝试内部可换顺序源重问一次 (tries), 提高单图产出率。
    page=None (probe/历史脚本) 时退回旧行为: 仅按归一化帧换算。
    """
    # 顺序源构建 (去重): 页面指令条优先 > 行1 裁剪 > 整图自读
    sources = []
    if tip_bytes:
        sources.append((tip_bytes, '页面指令条截图'))
    row1 = crop_row1(sprite_bytes)
    if row1:
        sources.append((row1, 'sprite 行1 裁剪'))
    if tip_bytes is None and row1 is None:
        sources.append((None, '无 (整图自读指令区)'))
    for idx, (tip, src) in enumerate(sources):
        if log:
            log(f'[captcha] AI 顺序源{idx + 1}/{len(sources)}: {src}')
        for _ in range(max(1, tries)):
            try:
                text = _vlm_call(sprite_bytes, cfg, log=log, tip_bytes=tip)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if log:
                    log(f'[captcha] AI 接口异常: {e}')
                continue
            m = re.search(r'\{.*\}', text, re.S)
            if not m:
                continue
            try:
                clicks = json.loads(m.group(0)).get('clicks')
            except Exception:
                continue
            if not (isinstance(clicks, list) and len(clicks) == 3 and all(
                    isinstance(p, list) and len(p) == 2 for p in clicks)):
                continue
            # 指令区预检: 报"指令行图标自身坐标"的空想答案在帧换算前拦截,
            # 避免被归一化帧洗白进主图区 (probe 兼容路径维持旧行为)
            if page is not None and _strip_answer(clicks):
                if log:
                    log(f'[captcha] AI 答案指向指令区 (空想形态), '
                        f'弃用: {clicks}')
                # temperature 0 下重问同源必得同答案, 换下一顺序源
                break
            # 双帧换算: 像素帧 (送图 2x=640x480) 与归一化帧 (0-1000)
            frames = _frame_candidates(clicks)
            if not frames:
                if log:
                    log(f'[captcha] AI 坐标越界或点到指令区, 弃用: {clicks}')
                # temperature 0 下重问同源必得同答案, 换下一顺序源
                break
            if page is None:
                # 兼容 probe 脚本: 无页面验证, 按归一化帧 (旧行为)
                pts = frames['norm'] if 'norm' in frames else frames['pixel']
            else:
                picked = _pick_frame(sprite_bytes, frames, log)
                if picked is None:
                    if log:
                        log(f'[captcha] AI 答案未过墨迹验证 (空想), '
                            f'弃用: {clicks}')
                    break
                _, pts = picked
            out = [(int(round(x)), int(round(y))) for x, y in pts]
            if any(y >= 160 for _, y in out):
                # 浮点取整越界 (y 逼近 160): 重问同源无意义, 换顺序源
                break
            return out
    return None


def _frame_candidates(clicks):
    """同一份 AI 答案按两种参考系换算为 320x160 主图坐标

    pixel 帧: AI 直接按送图 (2x 放大后 640x480) 的像素坐标作答。
    norm  帧: AI 按提示词 0-1000 归一化作答 (Qwen-VL 原生 grounding)。
    任一换算点落在主图区外则该帧整体无效 (两种答案都可能出现,
    由页面内墨迹评分仲裁谁对)。
    """
    frames = {}
    if all(0 <= p[0] <= 640 and 0 <= p[1] <= 480 for p in clicks):
        pts = [(p[0] / 2.0, p[1] / 2.0) for p in clicks]
        if all(0 <= x <= 320 and 0 <= y < 160 for x, y in pts):
            frames['pixel'] = pts
    if all(0 <= p[0] <= 1000 and 0 <= p[1] <= 1000 for p in clicks):
        pts = [(p[0] * 320.0 / 1000.0, p[1] * 240.0 / 1000.0) for p in clicks]
        if all(0 <= x <= 320 and 0 <= y < 160 for x, y in pts):
            frames['norm'] = pts
    return frames


# 墨迹验证阈值 (2026-08-27, 10 张失败样本回放校准):
#   图标上单点墨迹分 40~245, 干净背景 0~20 (最噪样本 19.9);
#   最弱分>=12 且中位>=25 即判定"三点都压在图标上";
#   点间距>=18px 排除模型把三点堆在同一个图标/小块上 (空想答案特征)
MIN_INK = 12
MED_INK = 25
MIN_SPREAD = 18

# 指令区行带 (全分辨率 320x240 空间, 行1~行4 共 80px, 上下各留 2px 余量)
STRIP_Y_MIN, STRIP_Y_MAX = 158, 240


def _ink_scores(sprite_bytes, pts):
    """墨迹评分: VALIDATE_JS 的 PIL 1:1 复刻 (等价性经 build_tmp/ink_audit.py
    回放实证)。点周围 17x17 patch 内 |灰度-patch中位|>=60 的像素数;
    sprite 1:1 解码后只取 y<160 主图区 (与 JS 全图解码后取 y<160 等价)。

    返回 [s1,s2,s3] 或 None (解码失败/尺寸异常)。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(sprite_bytes)).convert('RGB')
    except Exception:
        return None
    if img.size != (320, 240):
        return None
    px = img.load()
    gray = [[round((px[x, y][0] + px[x, y][1] + px[x, y][2]) / 3)
             for x in range(320)] for y in range(160)]
    out = []
    for cx, cy in pts:
        vals = []
        for y in range(max(0, round(cy) - 8), min(160, round(cy) + 9)):
            for x in range(max(0, round(cx) - 8), min(320, round(cx) + 9)):
                vals.append(gray[y][x])
        med = statistics.median(vals)
        out.append(sum(1 for v in vals if abs(v - med) >= 60))
    return out


def _strip_answer(clicks):
    """原始 AI 答案是否指向指令行区 (y[158,240))。

    qwen-vl-max 单图模式下最常见空想形态是报"指令行图标自身"的像素坐标
    (2x 送图下 行1 图标 y≈320-360, 行2≈360-400...), 归一化帧会把这类
    答案洗白进主图区顶部 (y≈80), 必须在双帧换算前按原始坐标拦截。
    像素帧/归一化帧任一参考系下全点落在指令带的答案都判空想。
    """
    pixel = [
        (p[0] / 2.0, p[1] / 2.0) for p in clicks
        if all(0 <= p[0] <= 640 and 0 <= p[1] <= 480 for p in clicks)]
    if pixel and all(STRIP_Y_MIN <= y < STRIP_Y_MAX for _, y in pixel):
        return True
    norm = [
        (p[0] * 320.0 / 1000.0, p[1] * 240.0 / 1000.0) for p in clicks
        if all(0 <= p[0] <= 1000 and 0 <= p[1] <= 1000 for p in clicks)]
    if norm and all(STRIP_Y_MIN <= y < STRIP_Y_MAX for _, y in norm):
        return True
    return False


def _pick_frame(sprite_bytes, frames, log=None):
    """双帧墨迹仲裁 (PIL 本地评分), 返回 ('pixel'|'norm', [(x,y)*3]) 或 None

    每帧点 int 截断后评分 (与旧页内 JS 路径传参一致); 最弱分>=MIN_INK
    且中位>=MED_INK 且三点最小间距>=MIN_SPREAD 才可接受, 双过取总分高者。
    未通过的帧逐帧打日志, 便于区分"真空想"与"阈值误杀"。
    """
    best = None
    verdicts = []
    for name in ('pixel', 'norm'):
        pts = frames.get(name)
        if pts is None:
            continue
        scores = _ink_scores(
            sprite_bytes, [(int(x), int(y)) for x, y in pts])
        if not scores:
            continue
        spread = min(
            ((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2) ** 0.5
            for i in range(3) for j in range(i + 1, 3))
        passed = (min(scores) >= MIN_INK and sorted(scores)[1] >= MED_INK
                  and spread >= MIN_SPREAD)
        verdicts.append(f'{name}={scores}(间距{spread:.0f},'
                        f'{"过" if passed else "拒"})')
        if passed and (best is None or sum(scores) > best[2]):
            best = (name, pts, sum(scores), scores, spread)
    if best is not None:
        if log:
            name = best[0]
            log(f'[captcha] AI 坐标选定 {name} 帧: 墨迹分 '
                f'{" / ".join(str(s) for s in best[3])} '
                f'(阈>=12, 中位>=25), 点间距={best[4]:.0f}px '
                f'[仲裁明细: {"; ".join(verdicts)}]')
        return best[:2]
    if log:
        log(f'[captcha] 墨迹验证未通过: '
            f'{"; ".join(verdicts) or "无有效帧"} '
            f'(阈: 最弱>={MIN_INK}, 中位>={MED_INK}, 间距>={MIN_SPREAD}px)')
    return None


def crop_instruction_icons(sprite_bytes, row=1, zoom=4):
    """逐图标裁出指令行并放大, 返回 [png bytes x3] 或 None。

    row=1 为黑图标白底真实顺序, row=3 为白图标黑底同序。两种光栅化
    分开保留可给 AI 交叉参考；切分复用背景自适应的 _slice_strip。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(sprite_bytes))
        if img.size != (320, 240):
            return None
        if row not in (1, 3):
            return None
        y0 = 160 if row == 1 else 200
        icons = _slice_strip(img.crop((0, y0, 320, y0 + 20)))
        if not icons:
            return None
        out = []
        for ic in icons:
            ic = ic.convert('RGB').resize(
                (ic.width * zoom, ic.height * zoom), Image.LANCZOS)
            buf = io.BytesIO()
            ic.save(buf, format='PNG')
            out.append(buf.getvalue())
        return out
    except Exception:
        return None


def crop_row1_icons(sprite_bytes, zoom=4):
    """兼容旧调用方：逐图标裁出 sprite 行1。"""
    return crop_instruction_icons(sprite_bytes, row=1, zoom=zoom)


def _normalized_icon_mask(raw, size=72):
    """把干净指令图标归一化为黑前景白背景, 保留内部镂空。"""
    try:
        from PIL import Image, ImageChops
        img = Image.open(io.BytesIO(raw)).convert('L')
    except Exception:
        return None
    if img.width < 2 or img.height < 2:
        return None
    px = img.load()
    corners = [px[0, 0], px[img.width - 1, 0],
               px[0, img.height - 1], px[img.width - 1, img.height - 1]]
    bg = statistics.median(corners)
    if bg >= 128:
        threshold = max(40, min(200, int(bg) - 40))
        mask = img.point(lambda v: 0 if v < threshold else 255)
    else:
        threshold = min(215, max(55, int(bg) + 40))
        mask = img.point(lambda v: 0 if v > threshold else 255)
    bbox = ImageChops.invert(mask).getbbox()
    if bbox is None:
        return None
    mask = mask.crop(bbox)
    scale = min((size - 8) / mask.width, (size - 8) / mask.height)
    nw = max(1, round(mask.width * scale))
    nh = max(1, round(mask.height * scale))
    mask = mask.resize((nw, nh), Image.Resampling.NEAREST)
    out = Image.new('L', (size, size), 255)
    out.paste(mask, ((size - nw) // 2, (size - nh) // 2))
    return out


def _compose_guide_card(variants):
    """合成单个目标卡：最多两种黑白渲染，各附水平镜像。"""
    try:
        from PIL import Image, ImageDraw
        masks = []
        fingerprints = set()
        for raw in variants:
            if not raw:
                continue
            mask = _normalized_icon_mask(raw)
            if mask is None:
                continue
            fingerprint = mask.tobytes()
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            masks.append(mask)
            if len(masks) == 2:
                break
        if not masks:
            return None
        panels = []
        for mask in masks:
            panels.append(mask)
            panels.append(mask.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
        gap = 6
        card = Image.new('RGB',
                         (len(panels) * 72 + (len(panels) - 1) * gap, 72),
                         (255, 255, 255))
        draw = ImageDraw.Draw(card)
        x = 0
        for idx, panel in enumerate(panels):
            card.paste(panel.convert('RGB'), (x, 0))
            if idx < len(panels) - 1:
                draw.line((x + 72 + gap // 2, 6, x + 72 + gap // 2, 66),
                          fill=(210, 210, 210), width=1)
            x += 72 + gap
        buf = io.BytesIO()
        card.save(buf, format='PNG')
        return buf.getvalue()
    except Exception:
        return None


def _build_hybrid_guides(sprite_bytes, tip_icons):
    """构建三个目标卡，页面指令优先，sprite 行1/行3作为双极性证据。"""
    row1 = crop_instruction_icons(sprite_bytes, row=1)
    row3 = crop_instruction_icons(sprite_bytes, row=3)
    guides = []
    for i in range(3):
        variants = []
        for source in (tip_icons, row1, row3):
            if source and len(source) == 3:
                variants.append(source[i])
        card = _compose_guide_card(variants)
        if card is None:
            return None
        guides.append((card, 'png'))
    return guides


def _build_raw_hybrid_guides(sprite_bytes, tip_icons):
    """构建第二路原始目标图，和归一化/镜像目标卡形成独立视图。"""
    icons = tip_icons if tip_icons and len(tip_icons) == 3 else None
    if icons is None:
        icons = crop_instruction_icons(sprite_bytes, row=1)
    if icons is None:
        icons = crop_instruction_icons(sprite_bytes, row=3)
    return [(raw, 'png') for raw in icons] if icons and len(icons) == 3 else None


def _extract_hybrid_candidates(sprite_bytes, max_candidates=16):
    """多尺度黑白分离候选提取，优先保证镂空/多部件图标的召回。

    单一 3x3 close 会漏掉地球网格、工具、电视等相互分离的部件；这里对
    黑/白极性分别执行 3x3、5x5、7x7 close，汇总后按位置/重叠去重。
    """
    try:
        from PIL import Image, ImageFilter
        image = Image.open(io.BytesIO(sprite_bytes)).convert('RGB')
        if image.size != (320, 240):
            return []
        image = image.crop((0, 0, 320, 160))
        pixels = image.load()
        gray = image.convert('L')

        def components(mask):
            mp = mask.load()
            seen = bytearray(320 * 160)
            found = []
            for y in range(160):
                for x in range(320):
                    start = y * 320 + x
                    if not mp[x, y] or seen[start]:
                        continue
                    stack = [start]
                    seen[start] = 1
                    n = 0
                    minx = maxx = x
                    miny = maxy = y
                    while stack:
                        current = stack.pop()
                        cx, cy = current % 320, current // 320
                        n += 1
                        minx, maxx = min(minx, cx), max(maxx, cx)
                        miny, maxy = min(miny, cy), max(maxy, cy)
                        for dy in (-1, 0, 1):
                            for dx in (-1, 0, 1):
                                if not dx and not dy:
                                    continue
                                nx, ny = cx + dx, cy + dy
                                if 0 <= nx < 320 and 0 <= ny < 160:
                                    index = ny * 320 + nx
                                    if mp[nx, ny] and not seen[index]:
                                        seen[index] = 1
                                        stack.append(index)
                    found.append((n, minx, maxx, miny, maxy))
            return found

        proposals = []
        for polarity in ('black', 'white'):
            base = Image.new('L', (320, 160), 0)
            bp = base.load()
            for y in range(160):
                for x in range(320):
                    r, g, b = pixels[x, y]
                    foreground = ((r < 60 and g < 60 and b < 60)
                                  if polarity == 'black'
                                  else (r > 235 and g > 235 and b > 235))
                    if foreground:
                        bp[x, y] = 255
            for radius in (1, 2, 3):
                size = radius * 2 + 1
                closed = base.filter(ImageFilter.MaxFilter(size)).filter(
                    ImageFilter.MinFilter(size))
                for n, minx, maxx, miny, maxy in components(closed):
                    width, height = maxx - minx + 1, maxy - miny + 1
                    if not (n >= 45 and 7 <= width <= 64 and 7 <= height <= 64):
                        continue
                    icon_values = [gray.getpixel((x, y))
                                   for y in range(miny, maxy + 1)
                                   for x in range(minx, maxx + 1) if bp[x, y]]
                    if not icon_values:
                        continue
                    ring_values = []
                    for y in range(max(0, miny - 5), min(160, maxy + 6)):
                        for x in range(max(0, minx - 5), min(320, maxx + 6)):
                            if minx <= x <= maxx and miny <= y <= maxy:
                                continue
                            r, g, b = pixels[x, y]
                            extreme = ((r < 60 and g < 60 and b < 60) or
                                       (r > 235 and g > 235 and b > 235))
                            if not extreme:
                                ring_values.append(gray.getpixel((x, y)))
                    background = (statistics.median(ring_values)
                                  if ring_values else 128)
                    contrast = abs(statistics.median(icon_values) - background)
                    if contrast < 35:
                        continue
                    proposals.append({
                        'x': round((minx + maxx) / 2),
                        'y': round((miny + maxy) / 2),
                        'n': n, 'w': width, 'h': height,
                        'minx': minx, 'maxx': maxx,
                        'miny': miny, 'maxy': maxy,
                        'polarity': polarity, 'contrast': contrast,
                        'radius': radius})

        def overlaps(first, second):
            ix = max(0, min(first['maxx'], second['maxx'])
                     - max(first['minx'], second['minx']) + 1)
            iy = max(0, min(first['maxy'], second['maxy'])
                     - max(first['miny'], second['miny']) + 1)
            intersection = ix * iy
            if not intersection:
                return False
            first_area = first['w'] * first['h']
            second_area = second['w'] * second['h']
            return intersection / min(first_area, second_area) >= 0.35

        proposals.sort(key=lambda item: (
            -item['contrast'], -item['radius'], -item['n']))
        kept = []
        for proposal in proposals:
            duplicate = any(
                (abs(proposal['x'] - old['x']) <= 12
                 and abs(proposal['y'] - old['y']) <= 12)
                or overlaps(proposal, old) for old in kept)
            if not duplicate:
                kept.append(proposal)
            if len(kept) >= max_candidates:
                break
        return sorted(kept, key=lambda item: (item['x'], item['y']))
    except Exception:
        return []


def _compose_candidate_gallery(sprite_bytes, comps):
    """合成主图框选预览和候选图册；每格左原始裁图，右归一化剪影。"""
    if not 3 <= len(comps) <= 16:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(sprite_bytes)).convert('RGB')
        if img.size != (320, 240):
            return None
        cell_w, cell_h = 188, 122
        cols = 3 if len(comps) <= 9 else 4
        rows = (len(comps) + cols - 1) // cols
        gallery = Image.new('RGB', (cols * cell_w, rows * cell_h),
                            (242, 242, 242))
        draw = ImageDraw.Draw(gallery)
        try:
            font = ImageFont.truetype('arial.ttf', 22)
        except OSError:
            font = ImageFont.load_default()

        def fit(source, box, resample):
            bw, bh = box
            scale = min(bw / source.width, bh / source.height)
            size = (max(1, round(source.width * scale)),
                    max(1, round(source.height * scale)))
            return source.resize(size, resample)

        for idx, comp in enumerate(comps):
            required = ('minx', 'maxx', 'miny', 'maxy', 'polarity')
            if not all(k in comp for k in required):
                return None
            col, row = idx % cols, idx // cols
            ox, oy = col * cell_w, row * cell_h
            draw.rectangle((ox + 2, oy + 2, ox + cell_w - 3, oy + cell_h - 3),
                           fill=(255, 255, 255), outline=(185, 185, 185), width=2)
            draw.text((ox + 8, oy + 5), f'#{idx + 1}', fill=(190, 0, 0), font=font)
            pad = 6
            x0 = max(0, int(comp['minx']) - pad)
            y0 = max(0, int(comp['miny']) - pad)
            x1 = min(320, int(comp['maxx']) + pad + 1)
            y1 = min(160, int(comp['maxy']) + pad + 1)
            crop = img.crop((x0, y0, x1, y1))
            original = fit(crop, (78, 78), Image.Resampling.LANCZOS)
            gallery.paste(original, (ox + 8 + (78 - original.width) // 2,
                                      oy + 36 + (78 - original.height) // 2))

            cp = crop.load()
            mask = Image.new('L', crop.size, 255)
            mp = mask.load()
            white = comp['polarity'] == 'white'
            for y in range(crop.height):
                for x in range(crop.width):
                    r, g, b = cp[x, y]
                    fg = (r > 205 and g > 205 and b > 205) if white else \
                         (r < 80 and g < 80 and b < 80)
                    if fg:
                        mp[x, y] = 0
            normalized = fit(mask, (78, 78), Image.Resampling.NEAREST)
            gallery.paste(normalized.convert('RGB'),
                          (ox + 102 + (78 - normalized.width) // 2,
                           oy + 36 + (78 - normalized.height) // 2))
            draw.line((ox + 94, oy + 34, ox + 94, oy + 116),
                      fill=(220, 220, 220), width=1)
        # 图册上方同时保留完整照片上下文；框线在 bbox 外，不覆盖图标。
        main = img.crop((0, 0, 320, 160)).resize(
            (640, 320), Image.Resampling.LANCZOS)
        main_draw = ImageDraw.Draw(main)
        for idx, comp in enumerate(comps):
            x0 = max(0, int(comp['minx']) * 2 - 4)
            y0 = max(0, int(comp['miny']) * 2 - 4)
            x1 = min(639, (int(comp['maxx']) + 1) * 2 + 3)
            y1 = min(319, (int(comp['maxy']) + 1) * 2 + 3)
            main_draw.rectangle((x0, y0, x1, y1),
                                outline=(255, 0, 0), width=2)
            label_y = y0 - 24 if y0 >= 24 else min(294, y1 + 3)
            main_draw.rectangle((x0, label_y, min(639, x0 + 34), label_y + 23),
                                fill=(255, 255, 255), outline=(255, 0, 0))
            main_draw.text((x0 + 3, label_y), f'#{idx + 1}',
                           fill=(190, 0, 0), font=font)
        combined_w = max(main.width, gallery.width)
        combined = Image.new('RGB', (combined_w, main.height + gallery.height + 8),
                             (242, 242, 242))
        combined.paste(main, ((combined_w - main.width) // 2, 0))
        combined.paste(gallery, ((combined_w - gallery.width) // 2,
                                 main.height + 8))
        buf = io.BytesIO()
        combined.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _candidate_icon_mask(sprite_bytes, comp, size=72):
    """按候选极性从主图 bbox 提取归一化剪影。"""
    try:
        from PIL import Image, ImageChops, ImageFilter
        img = Image.open(io.BytesIO(sprite_bytes)).convert('RGB')
        box = (int(comp['minx']), int(comp['miny']), int(comp['maxx']) + 1,
               int(comp['maxy']) + 1)
        crop = img.crop(box)
        white = comp.get('polarity') == 'white'
        mask = Image.new('L', crop.size, 255)
        src, dst = crop.load(), mask.load()
        for y in range(crop.height):
            for x in range(crop.width):
                r, g, b = src[x, y]
                fg = (r > 235 and g > 235 and b > 235) if white else \
                     (r < 60 and g < 60 and b < 60)
                if fg:
                    dst[x, y] = 0
        # 与 SEG_JS 一致的 3x3 close，连接 JPEG 破碎边缘。
        foreground = ImageChops.invert(mask)
        foreground = foreground.filter(ImageFilter.MaxFilter(3))
        foreground = foreground.filter(ImageFilter.MinFilter(3))
        mask = ImageChops.invert(foreground)
        bbox = foreground.getbbox()
        if bbox is None:
            return None
        mask = mask.crop(bbox)
        scale = min((size - 8) / mask.width, (size - 8) / mask.height)
        nw = max(1, round(mask.width * scale))
        nh = max(1, round(mask.height * scale))
        mask = mask.resize((nw, nh), Image.Resampling.NEAREST)
        out = Image.new('L', (size, size), 255)
        out.paste(mask, ((size - nw) // 2, (size - nh) // 2))
        return out
    except Exception:
        return None


def _shape_iou(first, second):
    """二值剪影 IoU，主图一侧允许水平镜像。"""
    try:
        from PIL import Image
        a = [v < 128 for v in first.getdata()]
        candidates = [second,
                      second.transpose(Image.Transpose.FLIP_LEFT_RIGHT)]
        best = 0.0
        for image in candidates:
            b = [v < 128 for v in image.getdata()]
            union = sum(1 for av, bv in zip(a, b) if av or bv)
            inter = sum(1 for av, bv in zip(a, b) if av and bv)
            best = max(best, inter / union if union else 0.0)
        return best
    except Exception:
        return 0.0


def _hybrid_shape_matrix(sprite_bytes, comps):
    """返回 3 x N 黑白双行镜像不变 IoU 矩阵。"""
    row1 = crop_instruction_icons(sprite_bytes, row=1)
    row3 = crop_instruction_icons(sprite_bytes, row=3)
    rows = [row for row in (row1, row3) if row and len(row) == 3]
    if not rows:
        return None
    candidate_masks = [_candidate_icon_mask(sprite_bytes, comp) for comp in comps]
    if any(mask is None for mask in candidate_masks):
        return None
    matrix = []
    for target_index in range(3):
        refs = [_normalized_icon_mask(row[target_index]) for row in rows]
        if any(ref is None for ref in refs):
            return None
        matrix.append([
            sum(_shape_iou(ref, candidate) for ref in refs) / len(refs)
            for candidate in candidate_masks])
    return matrix


def _hybrid_shape_scores(sprite_bytes, comps, order):
    """提取 AI 候选分配对应的三项形状分。"""
    matrix = _hybrid_shape_matrix(sprite_bytes, comps)
    if matrix is None or len(order) != 3:
        return None
    try:
        return [matrix[i][int(order[i]) - 1] for i in range(3)]
    except (ValueError, IndexError):
        return None


def _ai_name_order(marked_dataurl, guides, cfg, log=print,
                   timeout=_VLM_TIMEOUT,
                   candidate_count=None, prompt_text=None,
                   require_name_match=False):
    """AI 视觉匹配: 指令图标 -> 主图编号序列; 返回 [n1,n2,n3] 或 None

    marked_dataurl: 主图候选图册。
    guides: [(bytes, mime) x3] 逐图标模式 (HYBRID_PROMPT) 或
    [(bytes, mime) x1] 整条指令行模式 (HYBRID_PROMPT_STRIP)。
    旧版要求 AI 先命名再词汇对齐, 小图标命名易错; 单步直接匹配收窄错误面。
    """
    if _vlm_circuit_open():
        raise VlmUnavailableError('视觉模型连续失败，已暂时熔断')
    prompt = prompt_text or (
        HYBRID_PROMPT if len(guides) == 3 else HYBRID_PROMPT_STRIP)
    if candidate_count:
        prompt += (f'\nThere are exactly {candidate_count} candidates, labeled '
                   f'#1 through #{candidate_count}.')
    content = [
        {'type': 'text', 'text': prompt},
        {'type': 'image_url', 'image_url': {'url': marked_dataurl}}]
    for gb, gm in guides:
        content.append({'type': 'image_url', 'image_url': {
            'url': f'data:image/{gm};base64,'
                   + base64.b64encode(gb).decode()}})
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'user', 'content': content}],
        'temperature': 0,
    }
    if 'qwen3' in cfg['model']:
        payload['enable_thinking'] = False  # 同 _vlm_call
        payload['response_format'] = {'type': 'json_object'}
        payload['max_completion_tokens'] = 1024
    endpoint = cfg['base'].rstrip('/') + '/chat/completions'
    if log:
        log(f"[vlm] POST {endpoint} model={cfg['model']} "
            f'候选图册+目标图x{len(guides)}')
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + cfg['key'],
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read()[:600].decode('utf-8', 'replace')
        except Exception:
            body = ''
        if log:
            log(f'[vlm] HTTP {e.code} {endpoint}: {body or e.reason}')
        _vlm_mark_failure()
        raise VlmUnavailableError(
            f'HTTP {e.code}: {body or e.reason}') from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if log:
            log(f'[vlm] 连接失败 {endpoint}: {e}')
        _vlm_mark_failure()
        raise VlmUnavailableError(str(e)) from e
    _vlm_mark_success()
    msg = data['choices'][0]['message']
    text = msg.get('content') or ''
    if log:
        reasoning = msg.get('reasoning_content') or ''
        if reasoning:
            log(f'[captcha] AI 思考过程: {str(reasoning)[:800]}')
        log(f'[captcha] AI 命名回复: {text[:1200]}')
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        order = [str(x) for x in (obj.get('order') or [])]
    except Exception:
        return None
    if (len(order) != 3 or len(set(order)) != 3
            or not all(x.isdigit() for x in order)):
        return None
    if candidate_count and not all(1 <= int(x) <= candidate_count for x in order):
        return None
    if require_name_match:
        targets = obj.get('target_names')
        candidates = obj.get('candidate_names')
        if not (isinstance(targets, list) and len(targets) == 3
                and isinstance(candidates, dict)):
            return None

        def canonical(value):
            return re.sub(r'[^a-z0-9]+', '', str(value).casefold())

        pairs = [(canonical(targets[i]),
                  canonical(candidates.get(str(order[i]), '')))
                 for i in range(3)]
        if any(not target or target != candidate
               for target, candidate in pairs):
            if log:
                log(f'[captcha] AI 语义自检不一致, 弃用: {pairs}')
            return None
    return order


def hybrid_solve(page, sprite_bytes, tip_icons, cfg, log=print, tries=2):
    """混合方案: 本地像素分割取精确中心 + AI 候选编号匹配。

    页面指令条图标为权威目标，主图候选以“原始裁图 + 归一化剪影”图册
    送 AI，避免红圈覆盖小图标，也不让模型猜坐标；AI 输出的目标名、候选
    名和编号必须本地自洽。
    """
    comps = _extract_hybrid_candidates(sprite_bytes)
    if len(comps) < 3:
        if log:
            log(f'[captcha] 分割出 {len(comps)} 个图标 (<3), 混合方案不可用')
        return None
    gallery = _compose_candidate_gallery(sprite_bytes, comps)
    if gallery is None:
        if log:
            log(f'[captcha] 候选图册生成失败或候选过多({len(comps)}), '
                '混合方案不可用')
        return None
    raw_guides = _build_raw_hybrid_guides(sprite_bytes, tip_icons)
    if raw_guides is None:
        if log:
            log('[captcha] 三个指令图标未能完整切分, 混合方案不可用')
        return None
    if log:
        log(f'[captcha] 混合识别候选数={len(comps)}, '
            '使用原图/剪影图册 + 原始目标语义自检')
    for _ in range(tries):
        try:
            res = _ai_name_order(
                gallery, raw_guides, cfg, log=log,
                candidate_count=len(comps), prompt_text=HYBRID_PROMPT_SEMANTIC,
                require_name_match=True)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if log:
                log(f'[captcha] AI 命名接口异常: {e}')
            continue
        if not res:
            continue
        order = res
        shape_matrix = _hybrid_shape_matrix(sprite_bytes, comps)
        if shape_matrix is None:
            if log:
                log('[captcha] 混合候选剪影矩阵不可用, 弃用')
            continue
        try:
            shape_scores = [shape_matrix[i][int(order[i]) - 1]
                            for i in range(3)]
        except (ValueError, IndexError):
            continue
        shape_ratios = [shape_scores[i] / max(shape_matrix[i])
                        if max(shape_matrix[i]) else 0 for i in range(3)]
        if log:
            log('[captcha] 混合候选双行剪影分: '
                + ' / '.join(f'{score:.3f}' for score in shape_scores)
                + '; 最佳比: '
                + ' / '.join(f'{ratio:.2f}' for ratio in shape_ratios))
        if min(shape_scores) < 0.15 or min(shape_ratios) < 0.85:
            if log:
                log('[captcha] 语义候选未通过形状排名门控, 弃用')
            continue
        clicks = []
        for want in order:
            idx = int(want) - 1
            if 0 <= idx < len(comps):
                clicks.append((comps[idx]['x'], comps[idx]['y']))
        if len(clicks) == 3:
            return clicks
        if log:
            log(f'[captcha] 编号匹配不齐: order={order}, 分割数={len(comps)}')
    return None


def _download_url(url):
    """CDN 资源需 Referer, 否则 403"""
    req = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                      'Referer': 'https://remote.chaoxing.com/'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        # VPN 工具断开后可能残留不可用的本地系统代理。验证码资源属于
        # 当前已打开的超星站点，代理失败时改走真实路由重试一次。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as r:
            return r.read()


def get_sprite_bytes(page):
    bg = page.eval_on_selector(
        '#cx_imgBg', "el => getComputedStyle(el).backgroundImage")
    m = re.search(r'url\("?(.*?)"?\)', bg)
    if not m:
        return None
    return _download_url(m.group(1))


def _fetch_img(u):
    """指令图标下载 (data:/协议相对/根相对/CDN 全路径兼容, 带 Referer)"""
    if u.startswith('data:'):
        try:
            return base64.b64decode(u.split(',', 1)[1])
        except Exception:
            return None
    if u.startswith('//'):
        u = 'https:' + u
    elif u.startswith('/'):
        u = 'https://remote.chaoxing.com' + u
    try:
        return _download_url(u)
    except Exception:
        return None


def _slice_strip(img, log=None):
    """水平图标条按列投影切 3 个图标, 返回 [Image x3] 或 None

    背景色四角探测 (白底找黑/黑底找白); 只用条带中部 70% 高度做列投影,
    避开上下边缘的分隔线/窗口边框污染。要求恰好可归并成 3 段, 且三段
    宽度相近 (过碎说明空心图标内部被切开, 段合并不成功, 弃用)。
    """
    from PIL import Image
    g = img.convert('L')
    w, h = g.size
    if w < 12 or h < 3:
        return None
    px = g.load()
    corners = sorted((px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]))
    bg = corners[len(corners) // 2]
    white_mode = bg < 128
    thr = (lambda v: v > 200) if white_mode else (lambda v: v < 100)
    y0, y1 = h // 6, h * 5 // 6
    cols = [any(thr(px[x, y]) for y in range(y0, y1)) for x in range(w)]
    segs, s = [], None
    for x in range(w + 1):
        v = cols[x] if x < w else False
        if v and s is None:
            s = x
        elif not v and s is not None:
            if x - s >= 4:
                segs.append([s, x])
            s = None
    if len(segs) < 3:
        return None
    while len(segs) > 3:  # 空心图标内部切缝间墙比图标间距小, 优先合并最小缝
        gaps = [(segs[i + 1][0] - segs[i][1], i)
                for i in range(len(segs) - 1)]
        gi = min(gaps, key=lambda t: t[0])[1]
        a = segs.pop(gi)
        segs[gi][0] = a[0]
    widths = [(x1 - x0) for x0, x1 in segs]
    if max(widths) > min(widths) * 1.8 or min(widths) < 4:
        if log:
            log(f'[captcha] 指令雪碧图切分宽窄不均, 弃用: {widths}')
        return None
    return [img.crop((max(0, x0 - 1), 0, min(w, x1 + 1), h))
            for x0, x1 in segs]


def _tip_icons_from_geom(geom, log=None):
    """窗口雪碧图模板: 按同步 JS 采集的渲染几何, 把容器窗口 ∩ img 渲染框
    映射回原图裁剪 (含 natural 尺寸换算), 再按列投影切 3 个指令图标。
    返回 [png bytes x3] 或 None, 失败时保持旧行为 (顺序源退化)。
    """
    src = str(geom.get('src') or '')
    if not src:
        return None
    raw = _fetch_img(src)
    if not raw:
        return None
    nw, nh = geom.get('nw') or 0, geom.get('nh') or 0
    ir, br = geom.get('ir'), geom.get('br')
    if (not nw or not nh or not ir or not br
            or len(ir) != 4 or len(br) != 4 or ir[2] <= 0 or ir[3] <= 0):
        if log:
            log(f'[captcha] 指令条单图几何无效 (nw={nw}, ir={ir}, br={br})')
        return None
    ix = max(0.0, br[0] - ir[0])
    iy = max(0.0, br[1] - ir[1])
    iw = min(br[0] + br[2], ir[0] + ir[2]) - max(br[0], ir[0])
    ih = min(br[1] + br[3], ir[1] + ir[3]) - max(br[1], ir[1])
    if iw <= 0 or ih <= 0:
        if log:
            log(f'[captcha] 指令条单图与容器无可见交区 (ir={ir}, br={br})')
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        x0, y0 = ix / ir[2] * nw, iy / ir[3] * nh
        x1 = min(nw, (ix + iw) / ir[2] * nw)
        y1 = min(nh, (iy + ih) / ir[3] * nh)
        if int(x1) <= int(x0) or int(y1) <= int(y0):
            return None
        crop = img.crop((int(x0), int(y0), int(x1), int(y1)))
    except Exception:
        return None
    icons = _slice_strip(crop, log=log)
    if not icons:
        if log:
            log(f'[captcha] 指令条单图裁切 {crop.size} 未能切出 3 图标')
        return None
    out = []
    for ic in icons:
        ic = ic.resize((ic.width * 2, ic.height * 2), Image.LANCZOS)
        buf = io.BytesIO()
        ic.save(buf, format='PNG')
        out.append(buf.getvalue())
    return out


def get_tip_icons(page, log=None):
    """JS 提取页面指令条 3 个目标图标, 下载并 2x 放大, 返回 [png x3]

    原生窗口无元素截图能力; 直取图标图片比截图裁剪更精确
    (权威目标+顺序源, 左→右即点击顺序)。

    两种模板 (来自 SDK 实测):
      1. 3-img 模板 `.cx_tips__answer span img` (30x30): 逐容器收集
         img src 集齐 >=3 收敛 (旧版"命中第一个容器即停"会恒取 0 图);
      2. 窗口雪碧图模板 (2026-08-27 线上实锤): `.cx_tips__answer_div`
         (84x17 overflow:hidden 窗口) + 单张 `.cx_tips__answer_img`
         (width:340%, top:-144px 雪碧图)。单 img 收集不满 3, 旧逻辑
         恒 None; 现改同步编号几何采集 + Python 侧窗口映射裁切。
    指令图标由 SDK 异步填充, 采集重试 6x0.5s。
    """
    js = """() => {
        const sels = ['.cx_tips__answer', '.cx_tips__answer_div',
                      '.cx_tips__answer_img', '.cx_click-tip'];
        const urls = [];
        for (const s of sels) {
            const wrap = document.querySelector(s);
            if (!wrap) continue;
            for (const i of wrap.querySelectorAll('img')) {
                const src = (i.getAttribute('src') || '').trim();
                if (src && urls.indexOf(src) < 0) urls.push(src);
            }
            if (urls.length >= 3) return {urls: urls};
        }
        for (const s of sels) {
            const wrap = document.querySelector(s);
            if (!wrap || !wrap.querySelector('img')) continue;
            const i = wrap.querySelector('img');
            const ir = i.getBoundingClientRect();
            const br = wrap.getBoundingClientRect();
            const src = (i.getAttribute('src') || '').trim();
            if (!src) continue;
            return {urls: urls, geom: {
                src: src,
                ir: [ir.left, ir.top, ir.width, ir.height],
                br: [br.left, br.top, br.width, br.height],
                nw: i.naturalWidth || 0, nh: i.naturalHeight || 0}};
        }
        return {urls: urls};
    }"""
    res = None
    for _ in range(6):
        try:
            res = page.evaluate(js)
        except Exception:
            res = None
        if isinstance(res, dict) and isinstance(res.get('urls'), list):
            break
        time.sleep(0.5)
    if isinstance(res, list):  # 历史调用方/旧脚本兼容
        res = {'urls': res}
    if not isinstance(res, dict) or not isinstance(res.get('urls'), list):
        return None
    urls = res['urls']
    if len(urls) >= 3:  # 3-img 模板: 每个目标图标一张独立图
        try:
            from PIL import Image
            out = []
            for u in urls[-3:]:
                raw = _fetch_img(u)
                if not raw:
                    continue
                img = Image.open(io.BytesIO(raw)).convert('RGB')
                img = img.resize((img.width * 2, img.height * 2),
                                 Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                out.append(buf.getvalue())
            return out if len(out) == 3 else None
        except Exception:
            return None
    # 窗口雪碧图模板: 单 img + 渲染几何 → 窗口映射裁切
    icons = _tip_icons_from_geom(res.get('geom'), log=log)
    if icons is None and log:
        log('[captcha] 页面指令条为窗口雪碧图模板但未能切图, '
            '顺序源退化为 sprite 行1 裁剪')
    return icons


def compose_tip_strip(icons, gap=12, scale=2):
    """逐图标 PNG 左→右拼成单条指令图 (供 VLM 两图模式作第二图)"""
    try:
        from PIL import Image
        imgs = [Image.open(io.BytesIO(b)).convert('RGB') for b in icons]
        h = max(i.height for i in imgs)
        w = sum(i.width for i in imgs) + gap * (len(imgs) - 1)
        strip = Image.new('RGB', (w, h), (255, 255, 255))
        x = 0
        for i in imgs:
            strip.paste(i, (x, (h - i.height) // 2))
            x += i.width + gap
        strip = strip.resize((strip.width * scale, strip.height * scale),
                             Image.LANCZOS)
        buf = io.BytesIO()
        strip.save(buf, format='PNG')
        return buf.getvalue()
    except Exception:
        return None


def _popup_visible(page):
    """#eject 存在、display!=none 且有真实尺寸才算可见。

    站点验证码 SDK 会预插 0x0 的隐藏 #eject 容器 (display=block),
    仅判 display 会把未弹出误判为弹出; 尺寸门槛与内嵌 shim vis() 一致。
    注意: 通过后站点可能切到下一步页面 (无 #eject), 元素缺失必须算
    "已消失", 否则会把已通过误判为弹窗仍在。
    """
    try:
        d = page.eval_on_selector(
            '#eject', "el => { const r = el.getBoundingClientRect();"
                      " return getComputedStyle(el).display !== 'none'"
                      " && r.width > 1 && r.height > 1; }")
        return bool(d)
    except Exception:
        return False


def _sms_sent(page):
    """图标验证码通过后站点才发短信:
    以"获取验证码"按钮进入倒计时/变为重新获取为发送信号 (权威通过信号)"""
    try:
        return bool(page.evaluate(r"""() => {
            const re = /\d+\s*[sS秒]|重新获取|重新发送|获取中|已发送|发送中/;
            for (const el of document.querySelectorAll('button, a')) {
                const t = (el.textContent || '').trim();
                if (t && re.test(t)) return true;
            }
            return false;
        }"""))
    except Exception:
        return False


def _wait_result(page, log, tag, cancel=None):
    """等待图标验证码结果, 返回 'pass' / 'closed' / 'open'

    pass  = 检测到短信发送迹象 (倒计时/重新获取) —— 权威通过信号;
            站点仅在验证通过后发短信。注意: 通过后弹窗可能切到短信
            步骤而仍然存在, 故"弹窗仍在"不再作为失败依据 (旧版在此
            误判: 第一次已实际通过却判失败, 触发无谓刷新重试)。
    closed= 弹窗关闭但未匹配到倒计时文案 —— 视为通过 (兼容文案差异)
    open  = 弹窗仍在且无短信迹象 —— 未通过, 点击位置不对
    """
    for _ in range(10):
        if cancel and cancel():
            return 'cancelled'
        if _sms_sent(page):
            log(f'[captcha] {tag}通过, 站点已发送短信')
            return 'pass'
        if not _popup_visible(page):
            for _ in range(5):
                if cancel and cancel():
                    return 'cancelled'
                if _sms_sent(page):
                    log(f'[captcha] {tag}通过, 站点已发送短信')
                    return 'pass'
                time.sleep(0.6)
            log('[captcha] 弹窗已关闭, 未匹配到倒计时文案 '
                '(按通过处理; 若短信超时本次流程会自动重试)')
            return 'closed'
        time.sleep(1)
    return 'open'


def _click_sprite(page, x, y, mark=None):
    # #cx_imgBg 实测渲染尺寸 320x160, 与 sprite 主图区 1:1, 坐标直用;
    # mark 是内嵌 PageShim 的扩展参数；原生 Playwright 自测页不支持。
    position = {'x': x, 'y': y}
    if page.__class__.__module__.startswith('playwright.'):
        page.click('#cx_imgBg', position=position)
    else:
        page.click('#cx_imgBg', position=position, mark=mark)
    time.sleep(0.4)


def _click_retry_if_blocked(page, log=None):
    """连败后站点把弹窗底部换成「失败过多，点此重试」横幅, 此时
    普通刷新 (.cx_refresh) 无效, 必须点横幅重置出新图。返回是否点击。"""
    try:
        b = page.query_selector('text=点此重试')
        if b is not None and b.is_visible():
            b.click()
            if log:
                log('[captcha] 命中「失败过多，点此重试」状态, 点击重置')
            time.sleep(1.2)
            return True
    except Exception:
        pass
    return False


def _refresh(page, log=None):
    if not _click_retry_if_blocked(page, log):
        try:
            page.click('.cx_refresh')
        except Exception:
            pass
    time.sleep(1.2)


def _beep():
    try:
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        '[console]::beep(880,400);[console]::beep(660,400)'],
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        print('\a')


def vlm_configured(cfg):
    """仅当视觉模型三项配置完整时才允许进入 AI 自动识别。"""
    vlm = cfg.get('vlm') if isinstance(cfg, dict) else None
    return isinstance(vlm, dict) and all(
        str(vlm.get(key) or '').strip() for key in ('base', 'key', 'model'))


def handle_captcha(page, cfg, log=print, manual_timeout=300,
                   manual_provider=None, cancel=None):
    """处理当前已弹出的验证码, 成功返回 True

    流程: 候选编号双视图一致性识别 -> 端到端 VLM 坐标识别兜底
    -> 失败刷新重试 -> 人工兜底。
    manual_provider: callable(sprite_bytes, timeout, tip_bytes) -> [(x,y)*3]
    或 None, 由工具 GUI 内人工点击提供坐标 (无头浏览器下的唯一人工通道)。
    """
    vlm_cfg = cfg.get('vlm') if vlm_configured(cfg) else None
    max_attempts = cfg.get('captcha_max_attempts', 10)
    ai_unavailable = False
    if vlm_cfg:
        for attempt in range(1, max_attempts + 1):
            if cancel and cancel():
                return False
            # 连败后弹窗可能处于「失败过多」态: 先复位再取图,
            # 避免解旧图浪费尝试次数
            _click_retry_if_blocked(page, log)
            sprite = get_sprite_bytes(page)
            tip_icons = get_tip_icons(page, log=log)
            if tip_icons is None:
                log('[captcha] 未提取到页面指令条 (容器/时机), '
                    '顺序源退化为 sprite 行1 裁剪')
            tip = compose_tip_strip(tip_icons) if tip_icons else None
            if not sprite:
                log('[captcha] 获取验证码图片失败')
                clicks = None
            else:
                try:
                    log(f'[captcha] 第{attempt}次: 验证码图已保存 '
                        f'{_save_sprite(sprite)}')
                except Exception:
                    pass
                log(f'[captcha] 第{attempt}次 调用候选编号混合识别')
                try:
                    clicks = hybrid_solve(
                        page, sprite, tip_icons, vlm_cfg, log=log, tries=1)
                    if clicks is None:
                        log(f'[captcha] 第{attempt}次 混合识别无一致结果, '
                            '回退端到端 AI 视觉识别')
                        clicks = vlm_solve(
                            sprite, vlm_cfg, log=log, tip_bytes=tip,
                            page=page)
                except VlmUnavailableError as error:
                    log(f'[captcha] AI 接口不可用，立即转人工点选: {error}')
                    ai_unavailable = True
                    break
            if ai_unavailable:
                break
            if cancel and cancel():
                return False
            if clicks:
                log(f'[captcha] 第{attempt}次 识别结果, 点击坐标: {clicks}')
                try:
                    for i, (x, y) in enumerate(clicks, 1):
                        _click_sprite(page, x, y, i)
                except Exception:
                    if not _popup_visible(page):
                        log('[captcha] 弹窗已过期关闭, 本次验证无效')
                        return False
                    log('[captcha] 点击失败, 刷新重试')
                    _refresh(page, log)
                    continue
                time.sleep(1.0)
                if _wait_result(page, log, 'AI 识别', cancel) in ('pass', 'closed'):
                    return True
                log('[captcha] 图标点选未通过: 弹窗仍在 (点击位置不对), '
                    '刷新重试')
            else:
                log(f'[captcha] 第{attempt}次 AI 识别无结果')
            _refresh(page, log)
    else:
        log('[captcha] 未配置完整 AI 模型，跳过自动识别并直接转人工')
    # 人工兜底: 工具内点击
    if manual_provider is not None:
        log('[captcha] 转人工: 请在工具界面内点击验证码')
        deadline = time.time() + manual_timeout
        while time.time() < deadline:
            if cancel and cancel():
                return False
            _click_retry_if_blocked(page, log)
            sprite = get_sprite_bytes(page)
            tip_icons = get_tip_icons(page, log=log)
            tip = compose_tip_strip(tip_icons) if tip_icons else None
            clicks = manual_provider(sprite, min(120, deadline - time.time()),
                                     tip) if sprite else None
            if cancel and cancel():
                return False
            if not clicks:
                return False
            try:
                for i, (x, y) in enumerate(clicks, 1):
                    _click_sprite(page, x, y, i)
            except Exception:
                if not _popup_visible(page):
                    log('[captcha] 弹窗已过期关闭, 本次验证无效')
                    return False
                log('[captcha] 点击失败, 重试')
                _refresh(page, log)
                continue
            time.sleep(1.0)
            if _wait_result(page, log, '人工点击', cancel) in ('pass', 'closed'):
                return True
            log('[captcha] 图标点选未通过: 弹窗仍在 (点击位置不对), 重试')
            _refresh(page, log)
        return False
    # 无 provider (有头调试模式): 提示音等待用户在浏览器点击
    log('[captcha] 需要人工点击验证码 (完成后自动继续)')
    try:
        page.bring_to_front()
    except Exception:
        pass
    _beep()
    deadline = time.time() + manual_timeout
    while time.time() < deadline:
        if cancel and cancel():
            return False
        if not _popup_visible(page):
            log('[captcha] 人工完成')
            return True
        time.sleep(1)
    return False
