# -*- coding: utf-8 -*-
"""
core/ras_cred.py - Windows 已保存 VPN 凭据的读写 (单一数据源)

- 读: RasGetCredentialsW -> 用户名明文; 密码返回星号掩码 (系统安全设计,
  无法取回明文, 但连接时系统自动使用)。
- 写: RasSetCredentialsW, dwMask 只能含 UserName|Password (0x3)。
  实测: mask 带 RASCM_DefaultCreds(0x8) 会返回错误 5 (拒绝访问)。
"""
import ctypes
from ctypes import wintypes

_U, _P, _D = 256, 256, 15
_ENTRY, _PHONE = 256, 128


class _RASCREDENTIALS(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('dwMask', wintypes.DWORD),
        ('szUserName', ctypes.c_wchar * (_U + 1)),
        ('szPassword', ctypes.c_wchar * (_P + 1)),
        ('szDomain', ctypes.c_wchar * (_D + 1)),
    ]


class _RASDIALPARAMS(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('szEntryName', ctypes.c_wchar * (_ENTRY + 1)),
        ('szPhoneNumber', ctypes.c_wchar * (_PHONE + 1)),
        ('szCallbackNumber', ctypes.c_wchar * (_PHONE + 1)),
        ('szUserName', ctypes.c_wchar * (_U + 1)),
        ('szPassword', ctypes.c_wchar * (_P + 1)),
        ('szDomain', ctypes.c_wchar * (_D + 1)),
        ('dwSubEntry', wintypes.DWORD),
        ('dwCallbackId', ctypes.c_size_t),
        ('dwIfIndex', wintypes.DWORD),
    ]


def _get_credentials(name):
    rc = _RASCREDENTIALS()
    rc.dwSize = ctypes.sizeof(rc)
    rc.dwMask = 0x7
    result = ctypes.windll.rasapi32.RasGetCredentialsW(
        None, name, ctypes.byref(rc))
    return result, rc.szUserName, rc.szPassword


def _get_entry_dial_params(name):
    params = _RASDIALPARAMS()
    params.dwSize = ctypes.sizeof(params)
    params.szEntryName = (name or '')[:_ENTRY]
    has_password = wintypes.BOOL(False)
    result = ctypes.windll.rasapi32.RasGetEntryDialParamsW(
        None, ctypes.byref(params), ctypes.byref(has_password))
    return result, params.szUserName, params.szPassword, bool(has_password)


def _set_entry_dial_params(name, user, password, remove_password=False):
    params = _RASDIALPARAMS()
    params.dwSize = ctypes.sizeof(params)
    params.szEntryName = (name or '')[:_ENTRY]
    params.szUserName = (user or '')[:_U]
    params.szPassword = (password or '')[:_P]
    return ctypes.windll.rasapi32.RasSetEntryDialParamsW(
        None, ctypes.byref(params), bool(remove_password))


def _matches(user, password_handle, expected_user):
    return bool(password_handle) and user == (expected_user or '')[:_U]


def _set_credentials(name, user, password, remove=False):
    rc = _RASCREDENTIALS()
    rc.dwSize = ctypes.sizeof(rc)
    rc.dwMask = 0x3
    rc.szUserName = (user or '')[:_U]
    rc.szPassword = (password or '')[:_P]
    return ctypes.windll.rasapi32.RasSetCredentialsW(
        None, name, ctypes.byref(rc), bool(remove))


def get(name):
    """返回 (用户名, 密码); 未保存时 ('', ''); 密码可能是星号掩码"""
    try:
        result, user, password = _get_credentials(name)
        if result == 0 and password:
            return user, password
        result, user, password, has_password = _get_entry_dial_params(name)
        if result == 0 and has_password:
            return user, password
    except Exception:
        pass
    return '', ''


def set(name, user, password):
    """写入并回读确认用户名/密码；必要时兼容写入拨号参数。"""
    try:
        result = _set_credentials(name, user, password)
        if result != 0:
            return False, result
        get_result, saved_user, saved_password = _get_credentials(name)
        if get_result == 0 and _matches(saved_user, saved_password, user):
            return True, 0

        # 部分 EAP/系统版本把“记住的登录信息”放在拨号参数槽；使用官方
        # RasSetEntryDialParamsW 补写后再回读，不能只相信写 API 的返回码。
        fallback_result = _set_entry_dial_params(name, user, password)
        if fallback_result != 0:
            return False, f'回读未通过，兼容写入错误 {fallback_result}'
        get_result, saved_user, saved_password, has_password = \
            _get_entry_dial_params(name)
        if get_result == 0 and has_password and \
                _matches(saved_user, saved_password, user):
            return True, 0
        return False, f'Windows 凭据回读校验失败 ({get_result})'
    except Exception as e:
        return False, str(e)


def remove(name):
    """删除当前用户电话簿条目的 RAS 凭据，返回 (ok, err)。"""
    try:
        result = _set_credentials(name, '', '', True)
        fallback_result = _set_entry_dial_params(name, '', '', True)
        if result != 0:
            return False, result
        return fallback_result == 0, fallback_result
    except Exception as e:
        return False, str(e)
