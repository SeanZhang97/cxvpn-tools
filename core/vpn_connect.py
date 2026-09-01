# -*- coding: utf-8 -*-
"""
vpn_connect.py - 通过 rasdial 管理 Windows RAS/PPTP/IKEv2 VPN 连接

实测要点 (Win11 25H2):
- rasdial 没有任何保存凭据的开关 (/savecredentials、/SAVECRED 均非法);
  非法开关时 rasdial 打印 USAGE 且退出码为 0, 必须检测输出防止误判成功。
- 连接/认证失败时退出码 = RAS 错误码 (如 691)。
- 无参 rasdial 列出已连接条目: "Connected to <名称>" / "已连接 <名称>"。
"""
import ctypes
import json
import os
import subprocess
import threading
import time

from . import eap_connect, vpn_os

_CONN_CACHE = {}  # name -> (expire_ts, connected)
ERROR_SUCCESS = 0
ERROR_BUFFER_TOO_SMALL = 603
ERROR_CANCELLED = 1223
ERROR_TIMEOUT = 1460
RASCN_CONNECTION = 0x00000001
RASCN_DISCONNECTION = 0x00000002
RASCS_CONNECTED = 0x00002000
RASCS_DISCONNECTED = 0x00002001
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

_RAS_STATE_NAMES = {
    0: '打开端口',
    1: '端口已打开',
    2: '连接设备',
    3: '设备已连接',
    4: '链路已建立',
    5: '正在认证',
    6: '等待认证结果',
    7: '重试认证',
    10: '协商网络协议',
    14: '认证完成',
    18: '网络参数已就绪',
    19: '启动认证',
    21: '登录远程网络',
    RASCS_CONNECTED: '已连接',
    RASCS_DISCONNECTED: '已断开',
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', ctypes.c_uint32),
        ('Data2', ctypes.c_uint16),
        ('Data3', ctypes.c_uint16),
        ('Data4', ctypes.c_ubyte * 8),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [
        ('LowPart', ctypes.c_uint32),
        ('HighPart', ctypes.c_int32),
    ]


class _RASCONN(ctypes.Structure):
    """当前 Windows 使用的 RASCONNW 布局。"""
    _pack_ = 4
    _fields_ = [
        ('dwSize', ctypes.c_uint32),
        ('hrasconn', ctypes.c_void_p),
        ('szEntryName', ctypes.c_wchar * 257),
        ('szDeviceType', ctypes.c_wchar * 17),
        ('szDeviceName', ctypes.c_wchar * 129),
        ('szPhonebook', ctypes.c_wchar * 260),
        ('dwSubEntry', ctypes.c_uint32),
        ('guidEntry', _GUID),
        ('dwFlags', ctypes.c_uint32),
        ('luid', _LUID),
        ('guidCorrelationId', _GUID),
    ]


class _RAS_STATS(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('dwSize', ctypes.c_uint32),
        ('dwBytesXmited', ctypes.c_uint32),
        ('dwBytesRcved', ctypes.c_uint32),
        ('dwFramesXmited', ctypes.c_uint32),
        ('dwFramesRcved', ctypes.c_uint32),
        ('dwCrcErr', ctypes.c_uint32),
        ('dwTimeoutErr', ctypes.c_uint32),
        ('dwAlignmentErr', ctypes.c_uint32),
        ('dwHardwareOverrunErr', ctypes.c_uint32),
        ('dwFramingErr', ctypes.c_uint32),
        ('dwBufferOverrunErr', ctypes.c_uint32),
        ('dwCompressionRatioIn', ctypes.c_uint32),
        ('dwCompressionRatioOut', ctypes.c_uint32),
        ('dwBps', ctypes.c_uint32),
        ('dwConnectDuration', ctypes.c_uint32),
    ]


class _RASDIALPARAMS(ctypes.Structure):
    _fields_ = [
        ('dwSize', ctypes.c_uint32),
        ('szEntryName', ctypes.c_wchar * 257),
        ('szPhoneNumber', ctypes.c_wchar * 129),
        ('szCallbackNumber', ctypes.c_wchar * 129),
        ('szUserName', ctypes.c_wchar * 257),
        ('szPassword', ctypes.c_wchar * 257),
        ('szDomain', ctypes.c_wchar * 16),
        ('dwSubEntry', ctypes.c_uint32),
        ('dwCallbackId', ctypes.c_size_t),
        ('dwIfIndex', ctypes.c_uint32),
    ]


_RASDIALFUNC1 = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)(
    None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_uint32, ctypes.c_uint32)


class RasConnectionWatcher:
    """基于 RasConnectionNotificationW 的低开销连接变化监听器。"""

    def __init__(self):
        self._event = None
        self._kernel32 = None
        if os.name != 'nt':
            return
        try:
            kernel32 = ctypes.WinDLL('kernel32.dll', use_last_error=True)
            create_event = kernel32.CreateEventW
            create_event.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_wchar_p]
            create_event.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.WaitForSingleObject.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            event = create_event(None, False, False, None)
            if not event:
                return
            rasapi = ctypes.WinDLL('rasapi32.dll')
            notify = rasapi.RasConnectionNotificationW
            notify.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_uint32]
            notify.restype = ctypes.c_uint32
            code = int(notify(
                ctypes.c_void_p(-1), event,
                RASCN_CONNECTION | RASCN_DISCONNECTION))
            if code != ERROR_SUCCESS:
                kernel32.CloseHandle(event)
                return
            self._kernel32 = kernel32
            self._event = event
        except (AttributeError, OSError, TypeError, ValueError):
            self.close()

    @property
    def available(self):
        return bool(self._event)

    def wait(self, timeout_ms=0):
        if not self._event or not self._kernel32:
            return False
        wait = self._kernel32.WaitForSingleObject
        result = int(wait(self._event, max(0, int(timeout_ms))))
        return result == WAIT_OBJECT_0

    def close(self):
        event, self._event = self._event, None
        if event and self._kernel32:
            try:
                self._kernel32.CloseHandle(event)
            except (AttributeError, OSError, TypeError, ValueError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True,
                          errors='replace',
                          timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def _is_usage(out):
    head = out.splitlines()[0].strip() if out else ''
    return head.upper().startswith('USAGE') or head.startswith('用法')


_USER_PBK = os.path.join(os.environ.get('APPDATA', ''),
                         'Microsoft', 'Network', 'Connections',
                         'Pbk', 'rasphone.pbk')


def _silence_pbk(name):
    """拨号前静默 rasphone.pbk 预览开关, 避免弹预览/二次拨号窗口。

    PreviewUserPw/PreviewDomain/ShowDialingProgress=0、SkipDoubleDialDialog=1;
    保持电话簿原编码；兼容 Windows 当前使用的 UTF-8、UTF-16 与旧 ANSI。
    失败静默不阻断拨号。
    """
    if not name:
        return
    try:
        if not os.path.isfile(_USER_PBK):
            return
        with open(_USER_PBK, 'rb') as f:
            raw = f.read()
        bom = b''
        if raw.startswith(b'\xff\xfe'):
            bom, enc = b'\xff\xfe', 'utf-16-le'
        elif raw.startswith(b'\xfe\xff'):
            bom, enc = b'\xfe\xff', 'utf-16-be'
        elif raw.startswith(b'\xef\xbb\xbf'):
            bom, enc = b'\xef\xbb\xbf', 'utf-8'
        else:
            # 新版 Windows 可能把 rasphone.pbk 保存为无 BOM UTF-8。
            # 先严格按 UTF-8 解码，避免中文条目名被 mbcs 解成乱码。
            try:
                raw.decode('utf-8')
                enc = 'utf-8'
            except UnicodeDecodeError:
                enc = 'mbcs'
        text = raw[len(bom):].decode(enc, errors='replace')
        nl = '\r\n' if '\r\n' in text else '\n'
        sep = '\r\n' if '\r\n' in text else '\n'
        lines = text.split(sep)
        header = '[' + name + ']'
        in_sec = False
        changed = False
        for i, line in enumerate(lines):
            if line.startswith('['):
                in_sec = (line.strip() == header)
                continue
            if not in_sec:
                continue
            if line == 'PreviewUserPw=1':
                lines[i] = 'PreviewUserPw=0'; changed = True
            elif line == 'PreviewDomain=1':
                lines[i] = 'PreviewDomain=0'; changed = True
            elif line == 'ShowDialingProgress=1':
                lines[i] = 'ShowDialingProgress=0'; changed = True
            elif line == 'SkipDoubleDialDialog=0':
                lines[i] = 'SkipDoubleDialDialog=1'; changed = True
        if not changed:
            return
        out = nl.join(lines)
        data = bom + out.encode(enc)
        with open(_USER_PBK, 'wb') as f:
            f.write(data)
    except OSError:
        pass


def prepare_profile(name):
    """把 Windows 电话簿条目调整为使用已保存凭据的静默连接模式。"""
    _silence_pbk(name)


def prepare_credentials(name, username, password):
    """把软件凭据同步为 EAP 用户数据；非 EAP 条目无需额外处理。"""
    handled, ok, message = eap_connect.prepare(name, username, password)
    return ok, message if handled else ''


def _notify_progress(progress, message):
    if not progress:
        return
    try:
        progress(message)
    except Exception:
        pass


def _hang_up_pending(rasapi, connection):
    """结束未完成的拨号并给 RAS 状态机固定的释放时间。"""
    value = connection.value if isinstance(connection, ctypes.c_void_p) \
        else connection
    if not value:
        return False
    hang_up = rasapi.RasHangUpW
    hang_up.argtypes = [ctypes.c_void_p]
    hang_up.restype = ctypes.c_uint32
    if int(hang_up(ctypes.c_void_p(value))) != ERROR_SUCCESS:
        return False
    # Microsoft 要求挂断后等待状态机释放端口，否则下一次拨号可能报 756。
    time.sleep(3)
    return True


def _ras_dial_with_credentials(name, username, password, timeout=90,
                               progress=None, cancel_event=None):
    """通过异步 RasDialW 拨号，并在超时/取消时强制挂断连接句柄。"""
    params = _RASDIALPARAMS()
    params.dwSize = ctypes.sizeof(params)
    params.szEntryName = str(name or '')[:256]
    params.szUserName = str(username or '')[:256]
    params.szPassword = str(password or '')[:256]
    connection = ctypes.c_void_p()
    rasapi = ctypes.WinDLL('rasapi32.dll')
    dial = rasapi.RasDialW
    dial.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                     ctypes.POINTER(_RASDIALPARAMS), ctypes.c_uint32,
                     _RASDIALFUNC1, ctypes.POINTER(ctypes.c_void_p)]
    dial.restype = ctypes.c_uint32
    finished = threading.Event()
    outcome = {'code': None, 'connection': 0, 'state': None}
    last_state = {'value': None}

    @_RASDIALFUNC1
    def on_state(hrasconn, _message, state, error, extended_error):
        if hrasconn:
            outcome['connection'] = int(hrasconn)
        state = int(state)
        error = int(error)
        if state != last_state['value']:
            last_state['value'] = state
            label = _RAS_STATE_NAMES.get(state, f'状态 {state}')
            suffix = f'，错误 {error}' if error else ''
            if extended_error:
                suffix += f'，扩展错误 {int(extended_error)}'
            _notify_progress(progress, f'RAS {label}{suffix}')
        outcome['state'] = state
        if error:
            outcome['code'] = error
            finished.set()
        elif state == RASCS_CONNECTED:
            outcome['code'] = ERROR_SUCCESS
            finished.set()
        elif state == RASCS_DISCONNECTED:
            outcome['code'] = 668
            finished.set()

    # 非空回调使 RasDialW 立即返回；最终结果由回调通知，worker 不再被
    # Windows 内部 PPP 协商无限卡住。
    code = int(dial(None, None, ctypes.byref(params), 1, on_state,
                    ctypes.byref(connection)))
    if code != ERROR_SUCCESS:
        if connection.value:
            _hang_up_pending(rasapi, connection)
        return code

    deadline = time.monotonic() + max(0.01, float(timeout or 90))
    cancelled = False
    while not finished.is_set():
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        finished.wait(min(0.25, remaining))

    if not finished.is_set():
        handle = connection.value or outcome['connection']
        _notify_progress(
            progress, 'RAS 拨号已取消，正在释放连接' if cancelled
            else f'RAS 拨号超过 {timeout:g} 秒，正在强制释放连接')
        _hang_up_pending(rasapi, handle)
        return ERROR_CANCELLED if cancelled else ERROR_TIMEOUT

    result = int(outcome['code'] if outcome['code'] is not None else code)
    if result != ERROR_SUCCESS:
        handle = connection.value or outcome['connection']
        if handle:
            _hang_up_pending(rasapi, handle)
    return result


def _recent_eap_error(username, started_at):
    """读取本次拨号后的 EAP 失败事件，补足 RasDialW 丢失的内层错误码。"""
    start_ms = max(0, int((started_at - 1.0) * 1000))
    script = rf'''
Import-Module "$env:windir\System32\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.Diagnostics\Microsoft.PowerShell.Diagnostics.psd1"
$start = [DateTimeOffset]::FromUnixTimeMilliseconds({start_ms}).LocalDateTime
$rows = foreach ($event in @(Get-WinEvent -FilterHashtable @{{
  LogName='Microsoft-Windows-EapMethods-RasChap/Operational'
  Id=101
  StartTime=$start
}} -ErrorAction SilentlyContinue)) {{
  [xml]$xml = $event.ToXml()
  $values = @{{}}
  foreach ($data in @($xml.Event.EventData.Data)) {{
    $values[[string]$data.Name] = [string]$data.'#text'
  }}
  [pscustomobject]@{{
    Code = [string]$values['int1']
    Domain = [string]$values['Domain']
    Username = [string]$values['Username']
  }}
}}
if ($rows) {{ @($rows) | ConvertTo-Json -Compress }}
'''
    try:
        ok, out, _ = vpn_os._ps(script, timeout=10)
        if not ok or not out:
            return 0
        rows = json.loads(out)
        rows = rows if isinstance(rows, list) else [rows]
        expected = str(username or '').strip().casefold()
        for row in rows:
            identities = {
                str(row.get('Domain') or '').strip().casefold(),
                str(row.get('Username') or '').strip().casefold(),
            }
            if str(row.get('Code') or '').strip() == '691' and \
                    (not expected or expected in identities):
                return 691
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return 0


def status():
    """返回当前已连接的 RAS 条目名列表 (兼容中英文输出)"""
    try:
        r = _run(['rasdial'], timeout=15)
    except Exception:
        return []
    names = []
    listing = False
    for line in (r.stdout or '').splitlines():
        s = line.strip().rstrip('。.')
        if not s:
            continue
        low = s.lower()
        if low == 'connected to' or s in ('已连接到', '已连接'):
            # rasdial 常见格式是标题单独一行，后续每行一个连接名。
            listing = True
        elif low.startswith('connected to '):
            names.append(s[len('connected to '):].strip())
        elif s.startswith('已连接到'):
            names.append(s[len('已连接到'):].strip())
        elif s.startswith('已连接'):
            names.append(s[len('已连接'):].strip())
        elif (low.startswith('command completed') or
              low.startswith('no connections') or
              s.startswith('命令已') or s.startswith('没有连接')):
            listing = False
        elif listing:
            names.append(s)
        # 其余 ("No connections"/"Command completed"/"没有连接"/"命令已完成") 忽略
    return names


def connection_durations(rasapi=None):
    """返回活动 RAS 条目到系统会话持续秒数的映射。

    `dwConnectDuration` 由 Windows 维护，软件重启不会重置；任何 API/结构版本
    异常都返回空映射，由 Worker 回退到首次观察时间。
    """
    if rasapi is None:
        if os.name != 'nt':
            return {}
        try:
            rasapi = ctypes.WinDLL('rasapi32.dll')
        except OSError:
            return {}
    try:
        enum_connections = rasapi.RasEnumConnectionsW
        enum_connections.argtypes = [
            ctypes.POINTER(_RASCONN), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32)]
        enum_connections.restype = ctypes.c_uint32
        get_statistics = rasapi.RasGetConnectionStatistics
        get_statistics.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_RAS_STATS)]
        get_statistics.restype = ctypes.c_uint32

        structure_size = ctypes.sizeof(_RASCONN)
        connections = (_RASCONN * 1)()
        connections[0].dwSize = structure_size
        buffer_size = ctypes.c_uint32(ctypes.sizeof(connections))
        count = ctypes.c_uint32()
        code = int(enum_connections(
            connections, ctypes.byref(buffer_size), ctypes.byref(count)))
        if code == ERROR_BUFFER_TOO_SMALL:
            capacity = max(
                1, (int(buffer_size.value) + structure_size - 1) //
                structure_size)
            connections = (_RASCONN * capacity)()
            connections[0].dwSize = structure_size
            buffer_size.value = ctypes.sizeof(connections)
            code = int(enum_connections(
                connections, ctypes.byref(buffer_size), ctypes.byref(count)))
        if code != ERROR_SUCCESS:
            return {}

        durations = {}
        for index in range(min(int(count.value), len(connections))):
            connection = connections[index]
            name = str(connection.szEntryName or '').strip()
            if not (name and connection.hrasconn):
                continue
            stats = _RAS_STATS()
            stats.dwSize = ctypes.sizeof(stats)
            if int(get_statistics(
                    connection.hrasconn, ctypes.byref(stats))) == ERROR_SUCCESS:
                durations[name] = max(0.0, stats.dwConnectDuration / 1000.0)
        return durations
    except (AttributeError, OSError, TypeError, ValueError):
        return {}


def _set_cache(name, val):
    _CONN_CACHE[name] = (time.time() + 5.0, val)


def is_connected(name, refresh=False, max_age=5.0):
    """name 是否已连接 (rasdial 快路径 + 短缓存 + Get-VpnConnection 兜底)"""
    if not name:
        return False
    now = time.time()
    if not refresh:
        hit = _CONN_CACHE.get(name)
        if hit and now < hit[0]:
            return hit[1]
    val = False
    try:
        expected = name.strip().casefold()
        val = any(expected == n.strip().casefold() for n in status())
    except Exception:
        val = False
    if not val:
        # 权威兜底: PowerShell ConnectionStatus 枚举值与系统语言无关
        ok, out, _ = vpn_os._ps(
            f"(Get-VpnConnection -Name {vpn_os._quote(name)} "
            f"-ErrorAction SilentlyContinue).ConnectionStatus")
        if ok:
            val = (out or '').strip() == 'Connected'
    _CONN_CACHE[name] = (now + max_age, val)
    return val


def connect(name, username=None, password=None, timeout=90, log=None,
            cancel_event=None):
    """连接指定 VPN。

    软件保存的明文凭据直接交给 RasDialW；EAP 返回 703 时把凭据转换为
    EAP-MSCHAPv2 用户数据，再通过 RASEAPINFO 无界面拨号。
    """
    if not name:
        return False, '未指定 VPN 名称'
    _silence_pbk(name)
    if not (username and password):
        _set_cache(name, False)
        return False, ('NO_TOOL_CREDENTIALS 软件尚未保存此 VPN 的账号密码，'
                       '请在「VPN 配置」中点击该行「凭据」补录')
    started_at = time.time()
    try:
        code = _ras_dial_with_credentials(
            name, username, password, timeout=timeout,
            progress=(lambda message: log(f'[vpn] {name} {message}'))
            if log else None,
            cancel_event=cancel_event)
    except Exception as exc:
        _set_cache(name, False)
        return False, f'RAS 系统调用失败: {exc}'
    if code == 703:
        if log:
            log(f'[vpn] {name} 普通凭据路径返回 703，切换 EAP 无界面拨号')
        handled, ok, out = eap_connect.connect(
            name, username, password, timeout=timeout)
        if not handled:
            ok, out = False, 'RAS 错误 703'
    elif code == ERROR_TIMEOUT:
        ok, out = False, f'RAS 拨号超时 ({timeout:g} 秒)'
    elif code == ERROR_CANCELLED:
        ok, out = False, 'RAS 拨号已取消 (1223)'
    else:
        ok, out = code == 0, ('' if code == 0 else f'RAS 错误 {code}')
    if not ok and (code == 628 or '628' in (out or '')):
        nested_code = _recent_eap_error(username, started_at)
        if nested_code:
            out = f'RAS 错误 {nested_code} (EAP 事件确认认证失败)'
    connected = is_connected(name, refresh=True)
    if not ok and connected:
        ok = True
    _set_cache(name, ok or connected)
    return ok, out or '已使用软件保存的凭据连接'


def disconnect(name=None, timeout=60):
    args = ['rasdial'] + ([name] if name else []) + ['/disconnect']
    r = _run(args, timeout=timeout)
    out = ((r.stdout or '') + (r.stderr or '')).strip()
    if _is_usage(out):
        return False, out
    if name:
        _set_cache(name, False)
    return r.returncode == 0, out


if __name__ == '__main__':
    print('当前连接:', status())
