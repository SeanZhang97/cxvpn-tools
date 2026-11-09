# -*- coding: utf-8 -*-
"""超星拼图滑块：本地图像匹配、原生拖动及有界结果等待。"""
import io
import math
import time
import traceback
import urllib.parse
import urllib.request

from PIL import Image, ImageFilter


READ_SLIDER_JS = r"""() => {
  const visible = e => {
    if (!e) return false;
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && s.display !== 'none' &&
      s.visibility !== 'hidden' && !!e.getClientRects().length;
  };
  const root = document.querySelector('#eject');
  const knob = root?.querySelector('.cx_rightBtn');
  const piece = root?.querySelector('.cx_imgBtn img');
  const bg = root?.querySelector('#cx_imgBg');
  if (![root, knob, bg].every(visible) || !piece) return null;
  const k = knob.getBoundingClientRect(), p = piece.getBoundingClientRect();
  const b = bg.getBoundingClientRect();
  const match = getComputedStyle(bg).backgroundImage.match(/url\(["']?(.*?)["']?\)/);
  return {background: match ? new URL(match[1], location.href).href : '',
    piece: piece.currentSrc || piece.src,
    ready: piece.complete && piece.naturalWidth > 0 && p.width > 1,
    width: b.width, height: b.height, pieceWidth: p.width,
    pieceLeft: p.left - b.left, naturalWidth: piece.naturalWidth,
    knob: {x:k.left,y:k.top,w:k.width,h:k.height}};
}"""


def read_slider(page):
    if page.__class__.__module__ == 'core.browser_win':
        return page.b.eval_js(f'({READ_SLIDER_JS})()', timeout=2)
    return page.evaluate(READ_SLIDER_JS)


def is_slider(page):
    # 以实际可见的控件为准；旧缓存脚本可能无视 window.captchaType。
    try:
        return bool(read_slider(page))
    except (AttributeError, RuntimeError, TypeError):
        return False


def is_icon(page):
    try:
        return page.evaluate(r"""() => {
          const e = document.querySelector('#eject .cx_click-tip');
          return !!e && e.getBoundingClientRect().width > 1 &&
            getComputedStyle(e).display !== 'none';
        }""") is True
    except (AttributeError, RuntimeError, TypeError):
        return False


def same_challenge(before, after):
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    return (before.get('background') == after.get('background') and
            before.get('piece') == after.get('piece') and
            all(abs(before.get(k, 0) - after.get(k, 0)) < 0.5
                for k in ('width', 'height', 'pieceWidth')))


def solve_offset(background_bytes, piece_bytes):
    """返回原图横向位移及置信分数；不确定时拒绝，不把装饰缺口当目标。"""
    with Image.open(io.BytesIO(background_bytes)) as raw_bg, \
            Image.open(io.BytesIO(piece_bytes)) as raw_piece:
        bw, bh = raw_bg.size
        pw, ph = raw_piece.size
        if not (64 <= bw <= 1024 and 32 <= bh <= 512 and
                12 <= pw <= 128 and pw < bw and ph == bh):
            raise ValueError('滑块图像尺寸不受支持')
        bg = raw_bg.convert('L')
        piece_rgb = raw_piece.convert('RGB')
        piece = piece_rgb.convert('L')
    # 小图为黑底 JPEG，不是透明 PNG；去掉黑底、JPEG 边缘和黄色描边。
    mask = Image.new('L', (pw, ph))
    rgb = piece_rgb.load()
    mask.putdata([255 if max(rgb[x, y]) > 45 else 0
                  for y in range(ph) for x in range(pw)])
    mask = mask.filter(ImageFilter.MinFilter(7))
    points = [(x, y) for y in range(3, ph - 3, 2)
              for x in range(3, pw - 3, 2) if mask.getpixel((x, y))]
    if len(points) < 50:
        raise ValueError('拼图有效区域不足')
    samples = [piece.getpixel(point) for point in points]
    mean = sum(samples) / len(samples)
    centered = [value - mean for value in samples]
    energy = sum(value * value for value in centered)
    if energy / len(points) < 25:
        raise ValueError('拼图纹理不足，不能可靠定位')
    pixels = bg.load()
    scores = []
    for offset in range(1, bw - pw):
        values = [pixels[x + offset, y] for x, y in points]
        total = sum(values)
        variance = sum(v * v for v in values) - total * total / len(values)
        score = (sum(a * b for a, b in zip(centered, values)) /
                 math.sqrt(energy * variance)) if variance > 1 else -1
        scores.append((score, offset))
    scores.sort(reverse=True)
    best, offset = scores[0]
    alternate = max((score for score, x in scores if abs(x - offset) > 8),
                    default=-1)
    if best < 0.75 or best - alternate < 0.06:
        raise ValueError('拼图匹配置信度不足或存在多个相似位置')
    return {'offset': offset, 'width': bw, 'score': best,
            'margin': best - alternate}


def download_image(url, log, timeout=10):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.hostname not in ('captcha-b.cxdmp.com', 'captcha-b.chaoxing.com') or
            not parsed.path.startswith(('/slide/big/', '/slide/small/'))):
        raise ValueError('滑块图像来源不受支持')
    started = time.monotonic()
    label = '背景' if '/big/' in parsed.path else '拼图'
    log(f'[slider] 开始下载{label}，来源={parsed.hostname}，超时={timeout}s')
    request = urllib.request.Request(url, headers={
        'Referer': 'https://remote.chaoxing.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('滑块图像超过大小限制')
        log(f'[slider] {label}下载完成，耗时={time.monotonic() - started:.2f}s')
        return data
    except Exception as exc:
        # URL 中可能有挑战标识，不把网络异常的原始 URL 写入日志。
        log(f'[slider] {label}下载失败: {type(exc).__name__}，'
            f'耗时={time.monotonic() - started:.2f}s')
        raise RuntimeError(f'{label}下载失败 ({type(exc).__name__})') from None


def _pause(seconds, cancel):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel and cancel():
            raise InterruptedError('滑块验证已取消')
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def wait_result(page, sms_sent, popup_visible, timeout, cancel):
    """关闭/过期不等于通过；短信按钮进入倒计时才继续后续登录。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel and cancel():
            return 'cancelled'
        if sms_sent(page):
            return 'pass'
        if is_icon(page):
            return 'icon'
        if not popup_visible(page):
            # SDK 先收起验证框，成功回调/短信请求可能稍晚完成。
            for _ in range(15):
                if cancel and cancel():
                    return 'cancelled'
                if sms_sent(page):
                    return 'pass'
                _pause(0.1, cancel)
            return 'closed'
        _pause(0.2, cancel)
    return 'open'


def handle_slider(page, cfg, log, *, sms_sent, popup_visible,
                  refresh, cancel=None, manual_timeout=300):
    """True/False 为终态；站点换成点选题时返回 None，交还既有点选流程。"""
    started = time.monotonic()
    attempts = max(1, min(10, int(cfg.get('captcha_max_attempts', 10))))
    log(f'[slider] 自动验证任务已领取，开始执行，上限={attempts}次，超时=90s')
    try:
        for attempt in range(1, attempts + 1):
            if cancel and cancel():
                raise InterruptedError('滑块验证已取消')
            if time.monotonic() - started >= 90:
                log('[slider] 自动验证超时，停止重试')
                break
            if not popup_visible(page):
                return bool(sms_sent(page))
            if is_icon(page):
                log('[slider] 站点已切换为图标点选，移交既有验证流程')
                return None
            try:
                prepare = getattr(page, 'prepare_slider', None)
                if callable(prepare):
                    prepare()
                if cancel and cancel():
                    raise InterruptedError('滑块验证已取消')
                snapshot = read_slider(page)
                ready_deadline = min(started + 90, time.monotonic() + 5)
                while ((not snapshot or not snapshot.get('ready')) and
                       time.monotonic() < ready_deadline):
                    _pause(0.2, cancel)
                    snapshot = read_slider(page)
                if not snapshot or not snapshot.get('ready'):
                    raise ValueError('滑块图片尚未加载完成')
                background = download_image(snapshot['background'], log)
                if cancel and cancel():
                    raise InterruptedError('滑块验证已取消')
                piece = download_image(snapshot['piece'], log)
                if cancel and cancel():
                    raise InterruptedError('滑块验证已取消')
                solution = solve_offset(background, piece)
                if time.monotonic() - started >= 90:
                    log('[slider] 图像处理已达到自动验证期限，不再提交拖动')
                    break
                if not same_challenge(snapshot, read_slider(page)):
                    raise ValueError('图像识别期间验证码已刷新或窗口已缩放')
                offset = solution['offset'] * snapshot['width'] / solution['width']
                log(f'[slider] 第{attempt}次本地匹配通过，已提交拖动任务')
                if not page.drag_slider(snapshot, offset, cancel=cancel):
                    raise RuntimeError('拖动未执行')
                result = wait_result(page, sms_sent, popup_visible, 9, cancel)
                if result == 'pass':
                    log(f'[slider] 站点校验通过，耗时={time.monotonic() - started:.2f}s')
                    return True
                if result == 'cancelled':
                    raise InterruptedError('滑块验证已取消')
                if result == 'icon':
                    log('[slider] 站点已切换为图标点选，移交既有验证流程')
                    return None
                if result == 'closed':
                    log('[slider] 验证框已关闭但未确认通过，结束本轮')
                    return False
                log(f'[slider] 第{attempt}次站点未确认通过')
            except InterruptedError:
                raise
            except Exception as exc:
                log(f'[slider] 第{attempt}次失败: {type(exc).__name__}: {exc}\n'
                    f'{traceback.format_exc(limit=4)}')
            if attempt < attempts:
                if cancel and cancel():
                    raise InterruptedError('滑块验证已取消')
                refresh(page, log)
        log('[slider] 自动验证未完成，等待用户在内置浏览器拖动滑块')
        page.bring_to_front()
        result = wait_result(page, sms_sent, popup_visible, manual_timeout, cancel)
        log(f'[slider] 人工等待结束，结果={result}')
        if result == 'icon':
            return None
        return result == 'pass'
    except InterruptedError:
        log('[slider] 验证已取消')
        return False
