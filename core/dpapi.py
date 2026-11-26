# -*- coding: utf-8 -*-
"""当前 Windows 用户范围的 DPAPI 加解密；记录级熵由调用方提供，防止密文在记录间互换。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class DpapiError(OSError):
    """只携带失败阶段，不包含明文、密文或熵。"""


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, entropy, decrypt):
    raw = ctypes.create_string_buffer(data)
    entropy_raw = ctypes.create_string_buffer(entropy.encode('utf-8'))
    source = _Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    salt = _Blob(len(entropy_raw) - 1, ctypes.cast(entropy_raw, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    try:
        if not function(ctypes.byref(source), None, ctypes.byref(salt), None,
                        None, 1, ctypes.byref(target)):
            raise DpapiError('unprotect' if decrypt else 'protect')
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(raw, 0, len(raw))
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def protect(data, entropy):
    return _crypt(data, entropy, False)


def unprotect(data, entropy):
    return _crypt(data, entropy, True)
