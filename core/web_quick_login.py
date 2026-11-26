# -*- coding: utf-8 -*-
"""自动登录任务：一次打开对应一个任务，登录信息只在 Edge 扩展领取时取出且只发放一次。"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
import webbrowser

from core import edge_window
from core.two_factor import AccountStore, TwoFactorError
from core.web_quick import WebQuickError

PENDING_SECONDS = 120
CLAIM_SECONDS = 90
EXTENSION_SECONDS = 150
REPORT_STATES = ('success', 'failed')
ACTIVE_STATES = ('pending', 'claimed')
STATE_TEXT = {'pending': '任务已提交', 'claimed': '任务已被 Edge 扩展领取', 'success': '成功',
              'failed': '失败', 'expired': '任务已失效', 'timeout': '未收到结果，任务超时',
              'cancelled': '任务已取消'}


def _line(value, limit=120):
    return ' '.join(str(value or '').split())[:limit]


def edge_path():
    import winreg

    relative = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'
    for view in (0, winreg.KEY_WOW64_32KEY):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, relative, 0, winreg.KEY_READ | view) as key:
                    path = str(winreg.QueryValueEx(key, None)[0] or '').strip('"')
            except OSError:
                continue
            if os.path.isfile(path):
                return path
    bases = [os.environ.get(name) for name in
             ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA')]
    for base in filter(None, bases):
        path = os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe')
        if os.path.isfile(path):
            return path
    return None


def open_url(url, edge=False, *, new_window=False, maximized=False):
    """交给系统默认浏览器或 Edge 后立即返回，不等待页面结果。"""
    if not edge:
        return webbrowser.open(url)
    path = edge_path()
    if not path:
        raise FileNotFoundError('msedge.exe')
    arguments = [path]
    previous = edge_window.browser_windows(path) if new_window else set()
    if new_window:
        arguments.append('--new-window')
    if maximized:
        arguments.append('--start-maximized')
    if not new_window:
        arguments.append(url)
    subprocess.Popen(arguments, close_fds=True, creationflags=0x00000008 | 0x00000200)
    if new_window:
        edge_window.navigate_new_window(previous, path, url, maximized=maximized)


class LoginManager:
    def __init__(self, store, log, launch=None, otp=None, clock=time.time):
        self._store = store
        self._log = log
        self._launch = launch or open_url
        self._otp = otp or self._totp
        self._clock = clock
        self._lock = threading.Lock()
        self._runs = {}
        self._changes = 0
        self._extension_seen = 0.0
        self._extension_connected = False

    @property
    def changes(self):
        """读取时顺带推进超时任务；界面状态快照据此发现变化。"""
        with self._lock:
            self._sweep()
            return self._changes

    def status(self):
        with self._lock:
            self._sweep()
            return {'runs': {run['item_id']: self._public(run) for run in self._runs.values()},
                    'extension_seen': self._extension_seen if self._extension_connected else 0}

    def hello(self):
        with self._lock:
            self._sweep()
            self._extension_seen = self._clock()
            connected = not self._extension_connected
            self._extension_connected = True
            if connected:
                self._changes += 1
        if connected:
            self._log('[web-quick] Edge 扩展已连接')
        return {'ok': True, 'version': 1}

    def open(self, item):
        platform = item.get('platform')
        name = _line(item.get('name'))
        if not platform:
            try:
                opened = self._launch(item['url'])
            except OSError:
                opened = False
            if opened is False:
                return {'ok': False, 'msg': '无法调用系统默认浏览器，请检查默认浏览器设置'}
            self._log(f'[web-quick] 已交给系统默认浏览器打开：{name}')
            return {'ok': True}
        now = self._clock()
        with self._lock:
            self._sweep()
            blocker = next((run for run in self._runs.values() if run['platform'] == platform
                            and run['state'] in ACTIVE_STATES), None)
            if blocker:
                refusal = self._blocked(blocker, item['id'], now)
                if refusal:
                    return refusal
            run = {'run_id': uuid.uuid4().hex, 'item_id': item['id'], 'name': name,
                   'platform': platform, 'state': 'pending', 'detail': '',
                   'created_at': now, 'updated_at': now, 'expires_at': now + PENDING_SECONDS}
            self._runs[run['run_id']] = run
            self._changes += 1
        self._log(f'[web-quick] 「{name}」自动登录任务已提交；{PENDING_SECONDS} 秒内等待 Edge 扩展领取')
        try:
            self._launch(item['url'], edge=True)
        except FileNotFoundError:
            error = '未找到 Microsoft Edge，自动登录需要 Edge'
        except OSError:
            error = '未能启动 Microsoft Edge'
        else:
            self._log(f'[web-quick] 已启动 Edge 打开「{name}」')
            return {'ok': True, 'run': self._public(run)}
        self._log(f'[web-quick] 「{name}」自动登录任务提交后启动 Edge 失败：{error}')
        with self._lock:
            self._set(run, 'failed', error)
        return {'ok': False, 'msg': error, 'run': self._public(run)}

    def _resume(self, platform, login, run_id):
        with self._lock:
            self._sweep()
            run = self._runs.get(run_id)
            if run is None or run['platform'] != platform:
                return {'ok': False, 'error': 'invalid_resume'}
            if run['state'] == 'success':
                return {'ok': True, 'skipped': True, 'run_id': run_id, 'resumed': True}
            if run['state'] != 'claimed':
                return {'ok': False, 'error': 'invalid_resume'}
            if not login:
                self._set(run, 'success', '打开时已是登录状态')
                return {'ok': True, 'skipped': True, 'run_id': run_id, 'resumed': True}
            run['expires_at'] = self._clock() + CLAIM_SECONDS
        try:
            account = self._store.otp_account(run['item_id'])
            if not account or account['platform'] != platform:
                raise WebQuickError('登录信息已失效，请重新保存条目')
            one_time = self._otp(account['otp_account_id'])
        except (WebQuickError, TwoFactorError) as exc:
            self._fail(run, str(exc))
            return {'ok': False, 'error': 'credentials_unavailable'}
        except Exception as exc:
            self._log(f'[web-quick] 整页续接读取动态口令失败：{type(exc).__name__}')
            self._fail(run, '读取动态口令失败')
            return {'ok': False, 'error': 'credentials_unavailable'}
        self._log(f'[web-quick] Edge 扩展续接「{run["name"]}」的整页登录；仅续发动态口令，不重复发放账号密码')
        return {'ok': True, 'run_id': run_id, 'resumed': True,
                'credentials': {'otp': one_time}}

    def claim(self, platform, login=True, run_id=None):
        if run_id:
            return self._resume(platform, login, run_id)
        with self._lock:
            self._sweep()
            run = next((r for r in self._runs.values()
                        if r['platform'] == platform and r['state'] == 'pending'), None)
            if run is None:
                self._log(f'[web-quick] Edge 扩展请求领取自动登录任务，但站点 {platform} 没有待领取任务')
                return {'ok': False, 'error': 'no_pending_run'}
            run_id, item_id = run['run_id'], run['item_id']
            if not login:
                self._set(run, 'success', '打开时已是登录状态')
                return {'ok': True, 'skipped': True, 'run_id': run_id}
            self._set(run, 'claimed', '')
            run['expires_at'] = self._clock() + CLAIM_SECONDS
        self._log(f'[web-quick] Edge 扩展已领取「{run["name"]}」的自动登录任务；开始读取登录信息，数据库锁等待上限 5 秒')
        try:
            credentials = self._store.credentials(item_id)
            if not credentials or credentials['platform'] != platform:
                raise WebQuickError('登录信息已失效，请重新保存条目')
            one_time = self._otp(credentials['otp_account_id'])
        except (WebQuickError, TwoFactorError) as exc:
            self._fail(run, str(exc))
            return {'ok': False, 'error': 'credentials_unavailable'}
        except Exception as exc:
            self._log(f'[web-quick] 读取登录信息失败：{type(exc).__name__}')
            self._fail(run, '读取登录信息失败')
            return {'ok': False, 'error': 'credentials_unavailable'}
        self._log(f'[web-quick] 登录信息已发放给 Edge 扩展；{CLAIM_SECONDS} 秒内等待登录结果')
        return {'ok': True, 'run_id': run_id, 'credentials': {
            'username': credentials['username'], 'password': credentials['password'],
            'otp': one_time}}

    def report(self, run_id, state, detail='', platform=None):
        with self._lock:
            self._sweep()
            run = self._runs.get(run_id)
            if run is None:
                return {'ok': False, 'error': 'unknown_run'}
            if platform is not None and run['platform'] != platform:
                return {'ok': False, 'error': 'forbidden'}
            if run['state'] == 'claimed':
                self._set(run, state, detail)
            return {'ok': True, 'state': run['state']}

    def _blocked(self, blocker, item_id, now):
        if blocker['state'] == 'claimed':
            return {'ok': False, 'msg': '上一次自动登录仍在进行，请等待登录结果后再试'}
        if blocker['item_id'] != item_id:
            seconds = max(1, int(blocker['expires_at'] - now))
            return {'ok': False, 'msg': f'同一站点的另一个自动登录任务还在等待 Edge 扩展领取'
                                        f'（{seconds} 秒后失效）'}
        self._set(blocker, 'cancelled', '已被重新打开的操作替代')
        return None

    def _fail(self, run, detail):
        with self._lock:
            if run['state'] == 'claimed':
                self._set(run, 'failed', detail)

    def _set(self, run, state, detail=''):
        run.update(state=state, detail=_line(detail), updated_at=self._clock())
        self._changes += 1
        suffix = f'：{run["detail"]}' if run['detail'] else ''
        self._log(f'[web-quick] 「{run["name"]}」自动登录{STATE_TEXT[state]}{suffix}')

    def _sweep(self):
        now = self._clock()
        if self._extension_connected and now - self._extension_seen > EXTENSION_SECONDS:
            self._extension_connected = False
            self._changes += 1
            self._log('[web-quick] Edge 扩展连接已超时')
        latest = {}
        for run in sorted(self._runs.values(), key=lambda value: value['created_at']):
            if run['state'] == 'pending' and now >= run['expires_at']:
                self._set(run, 'expired', 'Edge 扩展未在 2 分钟内领取')
            elif run['state'] == 'claimed' and now >= run['expires_at']:
                self._set(run, 'timeout', '未在限定时间内收到登录结果')
            latest[run['item_id']] = run['run_id']
        # 每个条目只保留最近一次任务。
        self._runs = {run_id: self._runs[run_id] for run_id in latest.values()}

    def _totp(self, account_id):
        if not account_id:
            return ''
        accounts = AccountStore(self._store.root)
        code = accounts.code(account_id)
        # 剩余不足 4 秒时改用下一个周期，避免口令在提交前失效
        if code['expires_at'] - code['now'] < 4:
            time.sleep(max(0.0, code['expires_at'] - time.time()) + 0.5)
            code = accounts.code(account_id)
        return code['code']

    @staticmethod
    def _public(run):
        return {key: run[key] for key in
                ('run_id', 'item_id', 'name', 'platform', 'state', 'detail',
                 'created_at', 'updated_at', 'expires_at')}
