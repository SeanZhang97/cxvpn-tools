# -*- coding: utf-8 -*-
"""二维码只在有界独立进程中解码，图片及密钥不写临时文件。"""
import io
import json
import os
import subprocess
import sys

from core.two_factor import TwoFactorError

MAX_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 8_000_000


def decode_image(content):
    from PIL import Image, UnidentifiedImageError
    import zxingcpp

    if not content or len(content) > MAX_BYTES:
        raise TwoFactorError('图片为空或超过 6 MB')
    try:
        with Image.open(io.BytesIO(content)) as picture:
            if picture.format not in ('PNG', 'JPEG', 'WEBP'):
                raise TwoFactorError('仅支持 PNG、JPEG、WebP 二维码图片')
            if picture.width * picture.height > MAX_PIXELS:
                raise TwoFactorError('图片尺寸过大，请裁剪二维码后重试')
            picture.load()
            codes = zxingcpp.read_barcodes(picture.convert('RGB'), formats=zxingcpp.BarcodeFormat.QRCode)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise TwoFactorError('图片损坏或格式无效') from None
    values = list(dict.fromkeys(code.text for code in codes if code.valid))
    if not values:
        raise TwoFactorError('未识别到二维码，请使用清晰完整的图片')
    if len(values) != 1:
        raise TwoFactorError('图片包含多个二维码，请裁剪至单个账号后重试')
    if not values[0].lower().startswith('otpauth://totp/'):
        raise TwoFactorError('此二维码不是标准 TOTP 账号二维码')
    return values[0]


def _pipe_stream(kind):
    stream = getattr(sys, kind)
    if stream is not None:
        return stream.buffer
    # PyInstaller --noconsole 将 sys.std* 置空，但 subprocess 的继承管道仍有效。
    import ctypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
    kernel.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel.GetStdHandle(-10 if kind == 'stdin' else -11)
    mode = os.O_BINARY | (os.O_RDONLY if kind == 'stdin' else os.O_WRONLY)
    return os.fdopen(msvcrt.open_osfhandle(handle, mode), 'rb' if kind == 'stdin' else 'wb')


def run_child():
    try:
        result = {'ok': True, 'uri': decode_image(_pipe_stream('stdin').read(MAX_BYTES + 1))}
    except Exception as exc:
        result = {'ok': False, 'msg': str(exc) if isinstance(exc, TwoFactorError) else '二维码识别失败'}
    stream = _pipe_stream('stdout')
    stream.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    stream.flush()


def decode_isolated(content):
    if not isinstance(content, bytes) or not content or len(content) > MAX_BYTES:
        raise TwoFactorError('图片为空或超过 6 MB')
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command.append(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'main.py'))
    command.append('--otp-decode')
    try:
        completed = subprocess.run(command, input=content, capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        if completed.returncode != 0 or len(completed.stdout) > 32768:
            raise TwoFactorError('二维码识别进程异常退出')
        result = json.loads(completed.stdout.decode('utf-8'))
        if not result.get('ok'):
            raise TwoFactorError(result.get('msg') or '二维码识别失败')
        return result['uri']
    except subprocess.TimeoutExpired:
        raise TwoFactorError('二维码识别超过 10 秒，已终止；请裁剪图片后重试') from None
    except (UnicodeError, json.JSONDecodeError, KeyError):
        raise TwoFactorError('二维码识别返回无效结果') from None
