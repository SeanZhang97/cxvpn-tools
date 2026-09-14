# -*- coding: utf-8 -*-
"""core/proxy_guard.py - Windows 手动系统代理残留清扫。

背景：第三方代理工具（Clash 系等）退出不干净时，系统手动代理
(HKCU\\...\\Internet Settings) 仍指向 127.0.0.1 本地端口；重启后该端口
无人监听，走 WinINET/WinHTTP 的流量全部失效。本模块由 CXVPNTools 托管
两条清扫路径：

- 启动自检：上一轮管理器异常结束（脏标记存在）、代理已开启、全部条目
  指向回环地址、且这些端口无人监听 -> 关闭手动代理；
- 系统关机/注销：管理器收到关闭消息时，只要代理已开启且全部条目指向
  回环地址就先关闭再放行退出。此处不检查端口监听状态——关机时其它工具
  的进程同样即将结束，端口是否存活不能作为依据。

只清扫"全部条目指向回环地址"的代理；注册表中出现任何非回环条目
（公司/运营商代理）时一律不动，避免误伤用户原有配置。清扫前把现场
写入快照文件供人工恢复与审计；不做自动回滚——管理器只负责清扫，
无法知道被残留覆盖之前的原始设置。
"""
import json
import os
import socket
import tempfile
import time
import urllib.parse

from core import config as cfgmod

REG_PATH = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
GUARD_FILE_NAME = 'proxy_guard.json'           # 脏标记：running=存活, 缺失=已优雅退出
SNAPSHOT_FILE_NAME = 'proxy_guard_snapshot.json'

_LOOPBACK_EXACT = {'localhost', '127.0.0.1', '::1'}

_INTERNET_OPTION_SETTINGS_CHANGED = 39
_INTERNET_OPTION_REFRESH = 37
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_BLOCK = 0x0001
_SMTO_ABORTIFHUNG = 0x0002


def _file_path(name, base=None):
    return os.path.join(base or cfgmod.BASE, name)


def _atomic_json(path, payload):
    """原子写 JSON（先临时文件后替换），与 config.save 同思路。"""
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.proxy_guard.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# ---------- 注册表读写 ----------
def read_proxy_state(registry=None):
    """读取 HKCU 手动系统代理：enable/server/override/auto_config。"""
    if registry is None:
        import winreg
        registry = winreg
    state = {'enable': False, 'server': '', 'override': '', 'auto_config': ''}
    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER, REG_PATH, 0, registry.KEY_READ)
    except OSError:
        return state
    try:
        try:
            state['enable'] = bool(
                int(registry.QueryValueEx(key, 'ProxyEnable')[0] or 0))
        except (OSError, TypeError, ValueError):
            pass
        for field, name in (('server', 'ProxyServer'),
                            ('override', 'ProxyOverride'),
                            ('auto_config', 'AutoConfigURL')):
            try:
                state[field] = str(
                    registry.QueryValueEx(key, name)[0] or '').strip()
            except OSError:
                pass
    finally:
        registry.CloseKey(key)
    return state


def set_proxy_enabled(enabled, registry=None):
    """仅切换 ProxyEnable，不动服务器地址等其它字段。"""
    if registry is None:
        import winreg
        registry = winreg
    key = registry.OpenKey(
        registry.HKEY_CURRENT_USER, REG_PATH, 0, registry.KEY_SET_VALUE)
    try:
        registry.SetValueEx(key, 'ProxyEnable', 0,
                            registry.REG_DWORD, 1 if enabled else 0)
    finally:
        registry.CloseKey(key)


def refresh_user_proxy_settings(timeout_ms=1000, wininet=None, user32=None):
    """在当前登录用户会话刷新 WinINet，并通知已打开的设置窗口。"""
    if os.name != 'nt' and (wininet is None or user32 is None):
        return {
            'ok': False,
            'settings_changed': False,
            'refreshed': False,
            'broadcast': False,
        }

    import ctypes
    from ctypes import wintypes

    wininet = wininet or ctypes.WinDLL('wininet', use_last_error=True)
    user32 = user32 or ctypes.WinDLL('user32', use_last_error=True)
    set_option = wininet.InternetSetOptionW
    set_option.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    set_option.restype = wintypes.BOOL
    send_message = user32.SendMessageTimeoutW
    send_message.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send_message.restype = wintypes.LPARAM

    settings_changed = bool(set_option(
        None, _INTERNET_OPTION_SETTINGS_CHANGED, None, 0))
    refreshed = bool(set_option(
        None, _INTERNET_OPTION_REFRESH, None, 0))
    section = ctypes.create_unicode_buffer('Internet Settings')
    message_result = ctypes.c_size_t()
    broadcast = bool(send_message(
        _HWND_BROADCAST, _WM_SETTINGCHANGE, 0,
        ctypes.cast(section, ctypes.c_void_p).value,
        _SMTO_BLOCK | _SMTO_ABORTIFHUNG,
        max(100, min(int(timeout_ms), 5000)),
        ctypes.byref(message_result)))
    return {
        'ok': settings_changed and refreshed and broadcast,
        'settings_changed': settings_changed,
        'refreshed': refreshed,
        'broadcast': broadcast,
    }


# ---------- 条目解析 ----------
def _parse_entry(item):
    """解析 ProxyServer 的一条：'host:port'/'proto=host:port'，允许 scheme。

    返回 (host, port)；无法解析时 host 为空。
    """
    raw = str(item or '').strip()
    if not raw:
        return '', 0
    if '=' in raw:
        raw = raw.split('=', 1)[1].strip()
    if '://' in raw:
        raw = raw.split('://', 1)[1]
    parsed = urllib.parse.urlsplit('//' + raw)
    try:
        port = parsed.port
    except ValueError:
        return '', 0
    return (parsed.hostname or '').lower(), port or 0


def _is_loopback(host):
    return host in _LOOPBACK_EXACT or host.startswith('127.')


def loopback_entries(server):
    """拆分全部条目，返回 (回环条目 [(host, port)], 是否存在非回环条目)。"""
    loopback = []
    foreign = False
    for item in str(server or '').split(';'):
        host, port = _parse_entry(item)
        if not host or not port:
            continue
        if _is_loopback(host):
            loopback.append((host, port))
        else:
            foreign = True
    return loopback, foreign


def port_listening(host, port, timeout=0.3):
    """TCP 探测本地端口。::1 失败时回退 127.0.0.1（可能未启用 IPv6）。"""
    targets = [(host, port)]
    if host == '::1':
        targets.append(('127.0.0.1', port))
    for target_host, target_port in targets:
        try:
            with socket.create_connection(
                    (target_host, target_port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


# ---------- 脏标记 ----------
def mark_running(base=None):
    _atomic_json(_file_path(GUARD_FILE_NAME, base), {
        'state': 'running',
        'pid': os.getpid(),
        'started_at': time.time(),
    })


def mark_clean(base=None):
    try:
        os.unlink(_file_path(GUARD_FILE_NAME, base))
    except OSError:
        pass


def is_last_run_dirty(base=None):
    try:
        with open(_file_path(GUARD_FILE_NAME, base), encoding='utf-8') as f:
            return json.load(f).get('state') == 'running'
    except (OSError, ValueError):
        return False


# ---------- 快照 ----------
def _snapshot(base, state, reason):
    _atomic_json(_file_path(SNAPSHOT_FILE_NAME, base), {
        'saved_at': time.time(),
        'reason': reason,
        'proxy_enable': state['enable'],
        'proxy_server': state['server'],
        'proxy_override': state['override'],
        'auto_config_url': state['auto_config'],
    })


# ---------- 两条清扫路径 ----------
def startup_check(log=print, registry=None, base=None, port_check=None):
    """启动自检：脏标记 + 仅回环条目 + 端口无人监听 -> 关闭手动代理。

    返回被关闭的代理服务器串（供 UI 提示），未修复返回 None。
    """
    repaired = None
    if is_last_run_dirty(base):
        state = read_proxy_state(registry)
        loopback, foreign = loopback_entries(state['server'])
        checker = port_check or port_listening
        alive = [entry for entry in loopback if checker(*entry)]
        if state['enable'] and loopback and not foreign and not alive:
            _snapshot(base, state, 'startup_repair')
            set_proxy_enabled(False, registry)
            repaired = state['server']
            log(f'[proxy] 检测到上次异常退出残留的系统代理 '
                f'{state["server"] or "（空）"}，已自动关闭')
    mark_running(base)
    return repaired


def shutdown_cleanup(log=print, registry=None, base=None):
    """系统关机/注销前清扫：已开启且仅回环条目 -> 关闭；成功后清脏标记。

    返回被关闭的代理服务器串，未处理返回 None。
    """
    cleared = None
    state = read_proxy_state(registry)
    loopback, foreign = loopback_entries(state['server'])
    if state['enable'] and loopback and not foreign:
        _snapshot(base, state, 'shutdown_cleanup')
        set_proxy_enabled(False, registry)
        cleared = state['server']
        log(f'[proxy] 关机前检测到本地系统代理 '
            f'{state["server"] or "（空）"}，已关闭')
    mark_clean(base)
    return cleared
