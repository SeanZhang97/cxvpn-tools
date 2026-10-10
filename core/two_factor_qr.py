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


def _read_values(content):
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
    if len(values) > 32:
        raise TwoFactorError('图片中的二维码过多，请分张导入')
    return values


def decode_image(content):
    values = _read_values(content)
    return _totp_value(values)


def _totp_value(values):
    if len(values) != 1:
        raise TwoFactorError('图片包含多个二维码，请裁剪至单个账号后重试')
    if values[0].lower().startswith('otpauth-migration://'):
        raise TwoFactorError('这是 Google 导出二维码，请在“备份与恢复 → Google 导入”中批量导入')
    if not values[0].lower().startswith('otpauth://totp/'):
        raise TwoFactorError('此二维码不是标准 TOTP 账号二维码')
    return values[0]


def _automatic_result(values):
    if all(value.lower().startswith('otpauth-migration://offline?') for value in values):
        return {'kind': 'google', 'uris': values}
    return {'kind': 'totp', 'uri': _totp_value(values)}


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
        content = _pipe_stream('stdin').read(MAX_BYTES + 1)
        if '--otp-auto' in sys.argv:
            result = {'ok': True, **_automatic_result(_read_values(content))}
        elif '--otp-migration' in sys.argv:
            values = _read_values(content)
            if any(not value.lower().startswith('otpauth-migration://offline?') for value in values):
                raise TwoFactorError('请选择 Google Authenticator 导出账号的二维码')
            result = {'ok': True, 'uris': values}
        else:
            result = {'ok': True, 'uri': decode_image(content)}
    except Exception as exc:
        result = {'ok': False, 'msg': str(exc) if isinstance(exc, TwoFactorError) else '二维码识别失败'}
    stream = _pipe_stream('stdout')
    stream.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    stream.flush()


def decode_isolated(content, migration=False, timeout=10, auto=False):
    if not isinstance(content, bytes) or not content or len(content) > MAX_BYTES:
        raise TwoFactorError('图片为空或超过 6 MB')
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command.append(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'main.py'))
    command.append('--otp-decode')
    if auto:
        command.append('--otp-auto')
    elif migration:
        command.append('--otp-migration')
    try:
        completed = subprocess.run(command, input=content, capture_output=True, timeout=timeout,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        if completed.returncode != 0 or len(completed.stdout) > (1024 * 1024 if migration or auto else 32768):
            raise TwoFactorError('二维码识别进程异常退出')
        result = json.loads(completed.stdout.decode('utf-8'))
        if not result.get('ok'):
            raise TwoFactorError(result.get('msg') or '二维码识别失败')
        if auto and result.get('kind') == 'totp' and isinstance(result.get('uri'), str):
            return {'kind': 'totp', 'uri': result['uri']}
        if auto and result.get('kind') != 'google':
            raise TwoFactorError('二维码识别返回无效结果')
        if migration or auto:
            values = result['uris']
            if not isinstance(values, list) or not 1 <= len(values) <= 32 or any(not isinstance(v, str) for v in values):
                raise TwoFactorError('二维码识别返回无效结果')
            return {'kind': 'google', 'uris': values} if auto else values
        return result['uri']
    except subprocess.TimeoutExpired:
        raise TwoFactorError('二维码识别超时，已终止；请裁剪图片后重试') from None
    except (UnicodeError, json.JSONDecodeError, KeyError):
        raise TwoFactorError('二维码识别返回无效结果') from None
