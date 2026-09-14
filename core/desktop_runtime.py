# -*- coding: utf-8 -*-
"""在访问用户数据前确认实际路径；由桌面 Shell 启动时不继承启动器的重定向。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


_JOB_FLAG = '--cxvpn-desktop-job'
_ENV_NAMES = ('CODEX_HOME', 'PYTHONUTF8', 'PYTHONIOENCODING', 'UV_CACHE_DIR',
              'UV_PYTHON_INSTALL_DIR', 'CARGO_HOME', 'RUSTUP_HOME', 'VIRTUAL_ENV', 'PYTHONPATH')


def user_data_is_redirected():
    """路径字符串相同不代表文件相同；原子写入新文件也必须落到真实用户目录。"""
    if os.name != 'nt':
        return False
    from core import app_paths
    root = Path(app_paths.user_data_root())
    root.mkdir(parents=True, exist_ok=True)
    expected = root.resolve()
    descriptor, name = tempfile.mkstemp(prefix='.cxvpn-path-', suffix='.tmp', dir=root)
    try:
        os.close(descriptor)
        if Path(name).resolve().parent != expected:
            return True
        return any(path.exists() and path.resolve().parent != expected
                   for path in (root / 'state.sqlite3', root / 'config.json'))
    finally:
        os.unlink(name)


def _write_json(path, value):
    temporary = str(path) + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _process_running(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE；只观察自己的启动任务
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def _broker_job(request):
    path = Path(request).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if (temporary not in path.parents or not path.parent.name.startswith('cxvpn-desktop-')
            or path.name != 'request.json'):
        raise ValueError('桌面启动请求必须位于专用临时目录')
    response = path.with_name('response.json')
    log_path = path.with_name('output.log')
    try:
        job = json.loads(path.read_text(encoding='utf-8'))
        if user_data_is_redirected():
            raise RuntimeError('桌面进程仍存在用户数据重定向，已停止启动')
        _write_json(response, {'phase': 'claimed', 'pid': os.getpid()})
        env = os.environ.copy()
        env.update({name: value for name, value in job.get('environment', {}).items()
                    if name in _ENV_NAMES and isinstance(value, str)})
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        env.setdefault('PYTHONUTF8', '1')
        env.setdefault('PYTHONIOENCODING', 'utf-8')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        if job['wait']:
            with open(log_path, 'wb') as log:
                process = subprocess.run(job['command'], cwd=job['cwd'], env=env,
                                         stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, creationflags=flags)
            result = {'phase': 'done', 'exit_code': process.returncode}
        else:
            process = subprocess.Popen(job['command'], cwd=job['cwd'], env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, close_fds=True,
                                       creationflags=flags)
            result = {'phase': 'done', 'exit_code': 0, 'pid': process.pid}
        _write_json(response, result)
    except Exception as exc:
        _write_json(response, {'phase': 'failed', 'error_type': type(exc).__name__})


def _shell_launch(command):
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    # 必须使用桌面窗口的 Application。新建 Shell.Application 后直接调用
    # ShellExecute 仍可能沿用调用者环境，不能保证实际文件位置。
    script = f"""
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$cxShell = New-Object -ComObject Shell.Application
try {{
    $cxHwnd = 0
    $cxDesktop = $cxShell.Windows().FindWindowSW(0, 0, 8, [ref]$cxHwnd, 1)
    $cxDesktop.Document.Application.ShellExecute(
        {quote(command[0])}, {quote(subprocess.list2cmdline(command[1:]))},
        {quote(os.getcwd())}, 'open', 0)
}} finally {{
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($cxShell) | Out-Null
}}
"""
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                   timeout=10, check=True, capture_output=True, text=True,
                   encoding='utf-8', errors='backslashreplace',
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def ensure_desktop_runtime(*, wait=False):
    """在配置、日志和迁移前调用；构建等待任务结束，GUI 只等待桌面接收启动。"""
    if _JOB_FLAG in sys.argv:
        _broker_job(sys.argv[sys.argv.index(_JOB_FLAG) + 1])
        raise SystemExit(0)
    if not user_data_is_redirected():
        return
    command = ([sys.executable, *sys.argv[1:]] if getattr(sys, 'frozen', False)
               else [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    with tempfile.TemporaryDirectory(prefix='cxvpn-desktop-') as folder:
        request = Path(folder, 'request.json')
        _write_json(request, {'command': command, 'cwd': os.getcwd(), 'wait': wait,
                              'environment': {key: os.environ[key] for key in _ENV_NAMES
                                              if key in os.environ}})
        helper = ([sys.executable] if getattr(sys, 'frozen', False) else
                  [sys.executable, os.path.abspath(__file__)])
        _shell_launch(helper + [_JOB_FLAG, str(request.resolve())])
        response = Path(folder, 'response.json')
        log_path = Path(folder, 'output.log')
        started = time.monotonic()
        claimed = False
        while True:
            if response.is_file():
                result = json.loads(response.read_text(encoding='utf-8'))
                claimed = True
                if result['phase'] == 'failed':
                    raise RuntimeError('桌面启动失败：' + result['error_type'])
                if result['phase'] == 'done':
                    if log_path.is_file():
                        output = log_path.read_text(encoding='utf-8', errors='backslashreplace')
                        try:
                            sys.stdout.write(output)
                        except (OSError, UnicodeError):
                            pass
                    raise SystemExit(result['exit_code'])
                if not _process_running(result['pid']):
                    latest = json.loads(response.read_text(encoding='utf-8'))
                    if latest['phase'] != 'claimed':
                        continue
                    raise RuntimeError('桌面启动任务提前退出，未报告完成结果')
            if (not claimed or not wait) and time.monotonic() - started >= 15:
                raise TimeoutError('桌面未在 15 秒内领取启动请求，未访问用户配置')
            time.sleep(0.1)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    ensure_desktop_runtime()
