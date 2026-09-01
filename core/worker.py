# -*- coding: utf-8 -*-
"""core/worker.py - 后台调度线程: 自动续期 + 断线重连 + 原生浏览器管理 + 抓包落盘

内置浏览器为 pywebview/WebView2 原生窗口 (core.browser_win), 用户可直接
操作; 自动化经 JS 桥接驱动。抓包记录 (原生网络事件 + 页内 xhr body)
定时排空: 内存保留供 UI 展示, 全量追加 netlog.jsonl 供流程分析。
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta
from contextlib import contextmanager

NETLOG_MAX_UI = 500
NETLOG_FILE_LIMIT = 2 * 1024 * 1024


def _log_text(v):
    """保留抓包记录中的完整文本内容。"""
    return '' if v is None else str(v)


class Worker(threading.Thread):
    def __init__(self, cfg_get, log, manual=None, sms_poll=None, sms_show=None,
                 authorization_save=None):
        super().__init__(daemon=True)
        self.cfg_get = cfg_get
        self.log = log
        self.manual = manual
        self.sms_poll = sms_poll
        self.sms_show = sms_show
        self.authorization_save = authorization_save
        self.state = {'running': False, 'connected': False,
                      'connections': [], 'route_conflicts': [],
                      'connection_checked_at': 0.0,
                      'connection_error': None,
                      'connection_action': {
                          'active': False, 'status': 'idle',
                          'source': '', 'name': '', 'message': ''},
                      'last_renew': 0.0,
                      'authorization': {
                          'last_success_at': 0.0, 'expires_at': 0.0,
                          'next_renew_at': 0.0, 'source': '',
                          'status': 'unknown'},
                      'browser_up': False,
                      'browser_progress': {
                          'active': False, 'status': 'idle',
                          'step': 0, 'total': 5,
                          'title': '等待自动化任务',
                          'detail': '浏览器可随时手动操作'}}
        self.renew_requested = threading.Event()
        self.renew_cancel_requested = threading.Event()
        self._renew_lock = threading.Lock()
        self._renew_request_source = 'manual'
        self._renew_retry_after = 0.0
        self.state['renewing'] = False
        self.state['renew_cancel_pending'] = False
        self.browser_requested = threading.Event()
        self.main_window = None      # 主 pywebview 窗口 (内嵌面板宿主)
        self.storage_path = None     # WebView2 UserDataFolder (cookie 共享)
        self.browser_visible = False
        self.browser = None
        self.page = None
        self.netlog = []
        self._net_lock = threading.Lock()
        self._last_drain = 0.0
        self._stop_event = threading.Event()
        self._last_conn = 0.0
        self._vpn_operation_lock = threading.RLock()
        self._auto_connect_thread_lock = threading.Lock()
        self._auto_connect_thread = None
        self._auto_connect_name = ''
        self._auto_connect_source = ''
        self._auto_connect_dial_active = False
        self._connection_state_lock = threading.Lock()
        self._profile_refresh_lock = threading.Lock()
        self._profiles = []
        self._profiles_checked_at = 0.0
        self._connected_since = {}
        self._conn_fail = 0       # 连续失败次数 (退避用)
        self._retry_after = 0.0   # 失败退避截止时间戳
        self._conn_blocked = False  # 配置/服务类错误暂停自动重试
        self._connect_confirm_until = 0.0  # RasDial 成功后的系统状态确认窗口
        self._renew_trigger_after = 0.0  # 691 触发续期的冷却截止时间
        self._desired_vpn = ''
        self._auto_connect_suspended = False
        self._observed_connected_names = set()
        self._ras_refresh_due = 0.0
        self._ras_watcher = None
        self._disconnect_history = []
        self._reconnect_delays = (5, 15, 30, 60)
        self._reconnect_limit = 5
        self._event_reconnect_pending = False

    def stop(self):
        self._stop_event.set()
        self.renew_cancel_requested.set()

    def request_renew(self, source='manual'):
        """请求续期；同一时间只保留一个待执行或正在执行的任务。"""
        with self._renew_lock:
            if self.state['renewing'] or self.renew_requested.is_set():
                return False
            self._renew_request_source = source
            self._renew_retry_after = 0.0
            self.renew_cancel_requested.clear()
            self.renew_requested.set()
            return True

    def cancel_renew(self):
        """取消待执行任务，或向正在执行的流程发送协作式中断信号。"""
        with self._renew_lock:
            queued = self.renew_requested.is_set()
            active = bool(self.state['renewing'])
            if not (queued or active):
                return False
            self.renew_requested.clear()
            self._renew_retry_after = time.time() + 600
            self.state['renew_cancel_pending'] = active
            if active:
                self.renew_cancel_requested.set()
        if queued and not active:
            self._set_browser_progress(
                0, '授权处理已中断', '任务已在执行前取消', 'cancelled')
        return True

    @staticmethod
    def _timestamp(value):
        """兼容配置时间戳和超星接口的本地时间字符串。"""
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
        text = str(value or '').strip()
        if not text:
            return 0.0
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                return datetime.strptime(text, pattern).timestamp()
            except ValueError:
                continue
        return 0.0

    def _authorization_schedule(self, cfg, now=None):
        """根据已持久化的真实授权结果计算下一次续期，不凭启动时间猜测。"""
        now = time.time() if now is None else float(now)
        authorization = cfg.get('authorization') or {}
        last_success = self._timestamp(
            authorization.get('last_success_at'))
        expires_at = self._timestamp(authorization.get('expires_at'))
        hours = max(1.0, min(float(cfg.get('renew_hours', 7) or 7), 8.0))
        due_candidates = []
        if last_success:
            due_candidates.append(last_success + hours * 3600)
        if expires_at:
            # 到期前预留 15 分钟；若服务端受次日 02:00 截断，以返回值为准。
            due_candidates.append(expires_at - 15 * 60)
        next_renew = min(due_candidates) if due_candidates else 0.0
        status = ('unknown' if not expires_at else
                  'expired' if now >= expires_at else 'valid')
        state = {
            'last_success_at': last_success,
            'expires_at': expires_at,
            'next_renew_at': next_renew,
            'source': str(authorization.get('source') or ''),
            'status': status,
        }
        self.state['authorization'] = state
        self.state['last_renew'] = last_success
        return state

    def _save_authorization_result(self, expiries, source):
        """持久化授权成功时间和服务端到期时间；无返回时按业务上限兜底。"""
        now = time.time()
        normalized = {}
        for vpn_id, value in (expiries or {}).items():
            expires_at = self._timestamp(value)
            if expires_at:
                normalized[str(vpn_id)] = expires_at
        if normalized:
            expires_at = min(normalized.values())
        else:
            local_now = datetime.fromtimestamp(now)
            next_two = (local_now + timedelta(days=1)).replace(
                hour=2, minute=0, second=0, microsecond=0).timestamp()
            expires_at = min(now + 8 * 3600, next_two)
            self.log('[worker] 授权接口未返回到期时间，已按 8 小时/次日 '
                     '02:00 上限生成保守记录')
        authorization = {
            'last_success_at': now,
            'expires_at': expires_at,
            'source': source,
            'vpn_expiries': normalized,
        }
        if self.authorization_save:
            try:
                self.authorization_save(authorization)
            except Exception as exc:
                self.log(f'[worker] 保存授权到期时间失败: {exc}')
        cfg = self.cfg_get()
        cfg['authorization'] = authorization
        self._authorization_schedule(cfg, now=now)
        return authorization

    def _claim_renew(self, cfg):
        """原子领取一个手动/自动续期任务，避免请求与取消之间的竞态。"""
        with self._renew_lock:
            if self.state['renewing']:
                return None
            now = time.time()
            authorization = self._authorization_schedule(cfg, now=now)
            manual_due = self.renew_requested.is_set()
            next_renew = authorization.get('next_renew_at', 0.0)
            auto_due = bool(cfg.get('auto_renew', True) and next_renew and
                            now >= next_renew and
                            now >= self._renew_retry_after)
            if not (manual_due or auto_due):
                return None
            source = self._renew_request_source if manual_due else 'automatic'
            self.renew_requested.clear()
            self.renew_cancel_requested.clear()
            self.state['renewing'] = True
            self.state['renew_cancel_pending'] = False
            return source

    def _finish_renew(self):
        with self._renew_lock:
            self.state['renewing'] = False
            self.state['renew_cancel_pending'] = False
            self.renew_cancel_requested.clear()

    def reset_backoff(self, trigger_check=True, clear_confirmation=True):
        """解除失败状态；按调用场景决定是否立即检查及取消在途确认。"""
        self._clear_connection_failure()
        if clear_confirmation:
            self._connect_confirm_until = 0.0
        if trigger_check:
            self._last_conn = 0.0

    def connection_pending(self, name):
        """指定 VPN 是否已有拨号请求处于 Windows 状态确认期。"""
        expected = str(name or '').strip()
        with self._auto_connect_thread_lock:
            dialing = bool(
                self._auto_connect_thread and
                self._auto_connect_thread.is_alive() and
                self._auto_connect_dial_active and
                self._auto_connect_name == expected)
        return bool(dialing or (
            expected == self._desired_vpn and
            time.time() < self._connect_confirm_until))

    def dial_in_progress(self):
        """返回后台拨号快照，供 API 拒绝并发的手动拨号。"""
        with self._auto_connect_thread_lock:
            if not (self._auto_connect_thread and
                    self._auto_connect_thread.is_alive() and
                    self._auto_connect_dial_active):
                return None
            return {
                'name': self._auto_connect_name,
                'source': self._auto_connect_source,
            }

    def begin_connection_confirmation(self, name, source='manual'):
        """统一登记成功提交的拨号请求，所有入口共享同一重入门控。"""
        name = str(name or '').strip()
        self._desired_vpn = name
        self._auto_connect_suspended = False
        self._event_reconnect_pending = False
        now = time.time()
        self._last_conn = now
        self._connect_confirm_until = now + 30
        self._set_connection_action(
            'verifying', source, name,
            f'Windows 正在完成连接 {name}，等待状态确认')

    def _check_connection_confirmation(self, cfg):
        """确认窗口到期后只登记一次失败，再交给统一退避策略。"""
        deadline = self._connect_confirm_until
        if not deadline or time.time() < deadline:
            return None
        name = self._desired_vpn or cfg.get('vpn_name', '')
        action_source = (self.state.get('connection_action') or {}).get(
            'source', 'automatic')
        self._connect_confirm_until = 0.0
        try:
            profiles = self.refresh_connections()
        except Exception as exc:
            profiles = []
            self.log(f'[worker] 连接确认刷新失败: {exc}')
        connected_names = {
            row.get('name') for row in profiles
            if row.get('name') and row.get('status') == 'Connected'
        }
        if name in connected_names:
            self._clear_connection_failure()
            self._event_reconnect_pending = False
            self._set_connection_action(
                'connected', action_source, name, f'已连接 {name}')
            return True
        message = '连接请求已提交，但 Windows 未在 30 秒内确认连接建立'
        wait = None
        if cfg.get('auto_connect', False) and \
                not self._auto_connect_suspended:
            _policy, wait = self.record_connection_failure(message, name)
        else:
            self.state['connection_error'] = {
                'message': message, 'name': name, 'code': '',
                'category': 'unconfirmed', 'retryable': False,
                'suggest_repair': False, 'trigger_renew': False}
        self._event_reconnect_pending = wait is not None
        self._set_connection_action('failed', action_source, name, message)
        self.log(f'[worker] {name} {message}')
        return False

    def prepare_connection_target(self, name, expected_disconnects=None):
        """记录用户即将连接的目标，切换期间旧连接断开不得被自动拉回。"""
        next_name = str(name or '').strip()
        self._desired_vpn = next_name
        self._auto_connect_suspended = False
        self._event_reconnect_pending = False
        self._connect_confirm_until = 0.0
        self._clear_connection_failure()
        self._set_connection_action(
            'connecting', 'manual', next_name, f'正在连接 {next_name}')
        if expected_disconnects:
            self.log('[worker] 已记录 VPN 切换目标，旧连接断开不会触发重连')

    def note_manual_disconnect(self, names):
        """软件内主动断开后暂停本会话自动连接，直到用户再次连接。"""
        disconnected = {str(name or '').strip() for name in (names or [])}
        if self._desired_vpn in disconnected or not self._desired_vpn:
            self._auto_connect_suspended = True
            self._event_reconnect_pending = False
            self._connect_confirm_until = 0.0
            self._set_connection_action(
                'suspended', 'manual', self._desired_vpn,
                '已按手动断开暂停自动连接，手动连接后恢复')

    def update_default_target(self, name):
        """默认 VPN 改变时更新后台目标；不打断当前已连接的其它 VPN。"""
        with self._vpn_operation_lock:
            self._desired_vpn = str(name or '').strip()
            self._auto_connect_suspended = False
            self._event_reconnect_pending = False
            self._connect_confirm_until = 0.0
            self._clear_connection_failure()
            self._last_conn = 0.0

    def enable_auto_connect(self):
        """重新开启自动连接；保留已有确认窗口，避免开关动作造成重复拨号。"""
        with self._vpn_operation_lock:
            self._auto_connect_suspended = False
            self._clear_connection_failure()
            if not self._connect_confirm_until:
                self._last_conn = 0.0

    @contextmanager
    def manual_connection_action(self):
        """串行化手动与后台 VPN 操作，并避免手动操作后立即自动拨号。"""
        with self._vpn_operation_lock:
            try:
                yield
            finally:
                self._last_conn = time.time()

    def _clear_connection_failure(self):
        """清除失败策略状态，但不改变下一次状态检查时间。"""
        self._conn_fail = 0
        self._retry_after = 0.0
        self._conn_blocked = False
        self.state['connection_error'] = None

    def record_connection_failure(self, msg, name=None):
        """记录失败并应用分类策略，返回 (policy, retry_wait_seconds)。"""
        from . import vpn_service

        policy = vpn_service.failure_policy(msg)
        self.state['connection_error'] = {
            'message': msg, 'name': name or '', **policy}
        if policy['retryable']:
            self._conn_blocked = False
            self._conn_fail += 1
            if self._conn_fail >= self._reconnect_limit:
                self._conn_blocked = True
                self._retry_after = 0.0
                self.state['connection_error']['message'] = (
                    f'{msg}；连续失败 {self._conn_fail} 次，已暂停自动重连')
                return policy, None
            wait = policy.get('retry_delay') or self._reconnect_delays[
                min(self._conn_fail - 1, len(self._reconnect_delays) - 1)]
            self._retry_after = time.time() + wait
            return policy, wait
        self._conn_blocked = True
        self._retry_after = 0.0
        return policy, None

    def _set_connection_action(self, status, source='', name='', message=''):
        """公开后台连接动作，供总览展示自动连接/授权后重连状态。"""
        self.state['connection_action'] = {
            'active': status in ('queued', 'connecting', 'verifying'),
            'status': status,
            'source': source,
            'name': name,
            'message': message,
            'updated_at': time.time(),
        }

    def _attempt_auto_connect(self, cfg, source='automatic'):
        """刷新默认 VPN 状态，并按来源执行一次可观测的自动连接。"""
        with self._vpn_operation_lock:
            return self._attempt_auto_connect_locked(cfg, source)

    def _queue_auto_connect(self, cfg, source='automatic'):
        """把可能长耗时的拨号移出 worker 主循环，且同一时刻只保留一个任务。"""
        name = self._desired_vpn or cfg.get('vpn_name', '')
        with self._auto_connect_thread_lock:
            if self._auto_connect_thread and \
                    self._auto_connect_thread.is_alive():
                return False

            def run_dial():
                started = time.monotonic()
                try:
                    self._attempt_auto_connect(cfg, source)
                except Exception as exc:
                    self._set_connection_action(
                        'failed', source, name, f'后台拨号异常: {exc}')
                    self.log(f'[worker] 后台拨号任务异常: {name}: {exc}')
                finally:
                    elapsed = time.monotonic() - started
                    if elapsed >= 2:
                        self.log(f'[worker] 后台连接检查任务结束: {name}'
                                 f'（{elapsed:.1f} 秒）')

            self._auto_connect_name = name
            self._auto_connect_source = source
            self._auto_connect_thread = threading.Thread(
                target=run_dial, name='cxvpn-auto-dial', daemon=True)
            self._auto_connect_thread.start()
        return True

    def _try_vpn_operation(self, operation):
        """worker 主循环只尝试获取 VPN 锁，后台拨号期间绝不等待。"""
        if not self._vpn_operation_lock.acquire(blocking=False):
            return False
        try:
            operation()
            return True
        finally:
            self._vpn_operation_lock.release()

    def _attempt_auto_connect_locked(self, cfg, source='automatic'):
        """在 VPN 操作锁内执行自动连接判定与拨号。"""
        from . import vpn_service

        name = self._desired_vpn or cfg.get('vpn_name', '')
        try:
            profiles = self.refresh_connections()
        except Exception as exc:
            profiles = []
            self.log(f'[worker] 读取 VPN 连接状态失败: {exc}')
        connected_names = {
            row.get('name') for row in profiles
            if row.get('name') and row.get('status') == 'Connected'
        }
        target_status = next((
            str(row.get('status') or '').casefold()
            for row in profiles if row.get('name') == name), '')
        if name in connected_names:
            self._clear_connection_failure()
            self._connect_confirm_until = 0.0
            self._event_reconnect_pending = False
            action = self.state.get('connection_action') or {}
            if action.get('name') == name and action.get('status') in (
                    'queued', 'connecting', 'verifying'):
                self._set_connection_action(
                    'connected', action.get('source', source), name,
                    f'已连接 {name}')
            return True
        if not name or not cfg.get('auto_connect', False) or \
                self._auto_connect_suspended or self._conn_blocked or \
                time.time() < self._retry_after:
            return None
        if target_status == 'connecting':
            if not self._connect_confirm_until:
                self.begin_connection_confirmation(name, source)
            return None
        if target_status == 'disconnecting':
            self._retry_after = max(self._retry_after, time.time() + 5)
            return None
        if self._connect_confirm_until:
            return None
        # 用户切换到其它 VPN 后，以当前活动连接为准。自动连接只补齐
        # “当前没有任何 VPN”的状态，避免默认 VPN 抢回连接或造成路由冲突。
        if connected_names:
            if source == 'authorization':
                active = '、'.join(sorted(connected_names))
                self._set_connection_action(
                    'skipped', source, name,
                    f'已保留当前连接 {active}，未自动连接 {name}')
            return None

        label = ('授权完成，正在重新连接' if source == 'authorization'
                 else '正在自动连接')
        self._set_connection_action(
            'connecting', source, name, f'{label} {name}')
        creds = cfg.get('creds', {}).get(name)
        with self._auto_connect_thread_lock:
            self._auto_connect_dial_active = True
        self.log(f'[worker] 开始后台拨号: {name}（来源 {source}，独立任务）')
        try:
            ok, msg = vpn_service.connect(
                name, creds, log=self.log, cancel_event=self._stop_event)
        finally:
            with self._auto_connect_thread_lock:
                self._auto_connect_dial_active = False
        if ok:
            self._desired_vpn = name
            confirmed_profiles = self.refresh_connections()
            confirmed_names = {
                row.get('name') for row in confirmed_profiles
                if row.get('name') and row.get('status') == 'Connected'
            }
            self._last_conn = time.time()
            self._event_reconnect_pending = False
            if name in confirmed_names:
                self._clear_connection_failure()
                self._connect_confirm_until = 0.0
                self._set_connection_action(
                    'connected', source, name, f'已连接 {name}')
                self.log(f'[worker] 已连接 {name}')
            else:
                self.begin_connection_confirmation(name, source)
                self.log(f'[worker] 已提交 {name} 连接请求，等待 Windows 确认')
            return True

        self._connect_confirm_until = 0.0
        policy, wait = self.record_connection_failure(msg, name)
        self._set_connection_action('failed', source, name, msg)
        if wait is None:
            self.log(f'[worker] 连接失败: {msg} '
                     '(已暂停自动重试，等待修正配置或服务)')
        else:
            delay = f'{wait} 秒' if wait < 60 else f'{wait // 60} 分钟'
            self.log(f'[worker] 连接失败: {msg} ({delay}后重试)')
        if policy['trigger_renew'] and \
                time.time() >= self._renew_trigger_after:
            self._renew_trigger_after = time.time() + 600
            self.request_renew('recovery')
            self.log('[worker] 认证被拒 (691), '
                     '疑似授权到期, 已触发续期流程')
        return False

    def _reconnect_after_authorization(self, cfg):
        """授权成功后显式重连，不依赖下一轮 30 秒状态检查。"""
        name = self._desired_vpn or cfg.get('vpn_name', '')
        if not (name and cfg.get('auto_connect', False)):
            return None
        if self.connection_pending(name):
            self.log(f'[worker] 授权完成，{name} 已有连接请求等待确认，未重复拨号')
            return None
        self._renew_trigger_after = time.time() + 600
        self._last_conn = time.time()
        self._set_connection_action(
            'queued', 'authorization', name,
            f'授权完成，准备重新连接 {name}')
        self.log(f'[worker] 授权完成，立即重新连接 {name}')
        return self._queue_auto_connect(cfg, source='authorization')

    def refresh_connections(self, profiles=None):
        """刷新全部 VPN 连接态，并维护每条连接的会话时长。"""
        with self._profile_refresh_lock:
            return self._refresh_connections(profiles)

    def _handle_ras_event(self, cfg):
        """处理 Windows RAS 连接变化；事件仅触发刷新，不把普通断线当授权失败。"""
        before = set(self._observed_connected_names)
        try:
            profiles = self.refresh_connections()
        except Exception as exc:
            self.log(f'[worker] RAS 事件后刷新连接状态失败: {exc}')
            return
        after = {
            row.get('name') for row in profiles
            if row.get('name') and row.get('status') == 'Connected'
        }
        connected = after - before
        disconnected = before - after
        if connected:
            self.log('[worker] RAS 连接事件: ' + '、'.join(sorted(connected)))
        if disconnected:
            self.log('[worker] RAS 断开事件: ' + '、'.join(sorted(disconnected)))
        desired = self._desired_vpn or cfg.get('vpn_name', '')
        if desired in connected:
            self._clear_connection_failure()
            self._connect_confirm_until = 0.0
            self._event_reconnect_pending = False
            action = self.state.get('connection_action') or {}
            if action.get('name') == desired and action.get('status') in (
                    'queued', 'connecting', 'verifying'):
                self._set_connection_action(
                    'connected', action.get('source', 'ras_event'), desired,
                    f'已连接 {desired}')
        if desired not in disconnected or self._auto_connect_suspended or \
                not cfg.get('auto_connect', False):
            return
        # 另一个 VPN 已在线时保留用户当前出口，不抢回期望连接。
        if after:
            self._set_connection_action(
                'skipped', 'ras_event', desired,
                '检测到其它 VPN 已连接，未自动抢回原连接')
            return
        now = time.time()
        self._disconnect_history = [
            timestamp for timestamp in self._disconnect_history
            if now - timestamp <= 600]
        self._disconnect_history.append(now)
        if len(self._disconnect_history) >= self._reconnect_limit:
            self._conn_blocked = True
            self._event_reconnect_pending = False
            message = ('VPN 在 10 分钟内连续断开 '
                       f'{len(self._disconnect_history)} 次，已暂停自动重连')
            self.state['connection_error'] = {
                'message': message, 'name': desired, 'code': '',
                'category': 'flapping', 'retryable': False,
                'suggest_repair': False, 'trigger_renew': False}
            self._set_connection_action(
                'suspended', 'ras_event', desired, message)
            self.log(f'[worker] {message}')
            return
        delay = self._reconnect_delays[min(
            len(self._disconnect_history) - 1,
            len(self._reconnect_delays) - 1)]
        self._retry_after = max(self._retry_after, now + delay)
        self._event_reconnect_pending = True
        self._set_connection_action(
            'queued', 'ras_event', desired,
            f'检测到连接中断，{delay} 秒后尝试重连 {desired}')

    def _schedule_ras_refresh(self, now=None):
        """合并 RAS 事件风暴；首个事件确定刷新期限，后续事件不延后。"""
        if not self._ras_refresh_due:
            self._ras_refresh_due = (time.time() if now is None else now) + 1.0

    def list_profiles(self, force_refresh=False):
        """优先返回后台快照；无快照或明确刷新时再查询 Windows。"""
        with self._profile_refresh_lock:
            if not force_refresh:
                profiles, checked_at = self.profiles_snapshot()
                if checked_at:
                    return profiles
            return self._refresh_connections()

    def _refresh_connections(self, profiles=None):
        from . import vpn_connect, vpn_os

        rows = profiles if profiles is not None else vpn_os.list_vpns()
        rows = [dict(row) for row in (rows or [])]
        if profiles is None:
            # Get-VpnConnection 在 RAS 刚完成拨号时可能仍短暂返回 Disconnected。
            # rasdial 列出的当前 RAS 会话更接近实时状态，按完整名称合并，避免
            # Windows 已连接而页面仍显示未连接。
            active_names = {
                str(name).strip().casefold()
                for name in vpn_connect.status()
                if str(name).strip()
            }
            for row in rows:
                row_name = str(row.get('name') or '').strip().casefold()
                if row_name and row_name in active_names:
                    row['status'] = 'Connected'
        now = time.time()
        connected_names = {
            row.get('name') for row in rows
            if row.get('name') and row.get('status') == 'Connected'
        }
        self._observed_connected_names = set(connected_names)
        system_durations = vpn_connect.connection_durations() \
            if profiles is None else {}
        duration_by_name = {
            str(name).strip().casefold(): duration
            for name, duration in system_durations.items()
        }
        with self._connection_state_lock:
            self._profiles = [dict(row) for row in rows]
            self._profiles_checked_at = now
            for name in connected_names:
                duration = duration_by_name.get(str(name).strip().casefold())
                if duration is None:
                    self._connected_since.setdefault(name, now)
                else:
                    self._connected_since[name] = now - max(0.0, duration)
            for name in list(self._connected_since):
                if name not in connected_names:
                    del self._connected_since[name]
            connections = []
            for row in rows:
                if row.get('status') != 'Connected':
                    continue
                item = dict(row)
                item['connected_at'] = self._connected_since[item['name']]
                item['connected_seconds'] = max(
                    0, int(now - item['connected_at']))
                key = str(item['name']).strip().casefold()
                item['connection_time_source'] = \
                    'system' if key in duration_by_name else 'observed'
                connections.append(item)
            conflicts = vpn_os.analyze_route_conflicts(rows)
            default_name = self.cfg_get().get('vpn_name', '')
            self.state['connections'] = connections
            self.state['route_conflicts'] = conflicts
            self.state['connection_checked_at'] = now
            self.state['connected'] = default_name in connected_names
        by_name = {row.get('name'): row for row in connections}
        for row in rows:
            session = by_name.get(row.get('name'))
            if session:
                row['connected_at'] = session['connected_at']
                row['connected_seconds'] = session['connected_seconds']
                row['connection_time_source'] = \
                    session['connection_time_source']
        return rows

    def profiles_snapshot(self):
        """返回最近一次后台系统查询得到的完整 VPN 配置快照。"""
        with self._connection_state_lock:
            profiles = [dict(row) for row in self._profiles]
            checked_at = self._profiles_checked_at
        return profiles, checked_at

    def connection_snapshot(self, default_name=None):
        """返回可序列化的多连接快照；时长在读取时持续增长。"""
        now = time.time()
        with self._connection_state_lock:
            connections = []
            for row in self.state.get('connections', []):
                item = dict(row)
                started = float(item.get('connected_at') or now)
                item['connected_seconds'] = max(0, int(now - started))
                connections.append(item)
            conflicts = [dict(row) for row in
                         self.state.get('route_conflicts', [])]
            profiles = [dict(row) for row in self._profiles]
            checked_at = self.state.get('connection_checked_at', 0.0)
        default_name = default_name if default_name is not None \
            else self.cfg_get().get('vpn_name', '')
        names = [row.get('name') for row in connections]
        return {
            'connected': default_name in names,
            'default_connected': default_name in names,
            'default_name': default_name,
            'connected_names': names,
            'connected_count': len(connections),
            'connections': connections,
            'profiles': profiles,
            'route_conflicts': conflicts,
            'checked_at': checked_at,
        }

    def _set_browser_progress(self, step, title, detail='', status='running'):
        """更新真实续期阶段，并同步到原生浏览器内的浮动进度面板。"""
        progress = {
            'active': status == 'running',
            'status': status,
            'step': max(0, min(int(step), 5)),
            'total': 5,
            'title': title,
            'detail': detail,
        }
        self.state['browser_progress'] = progress
        self._push_browser_overlay()

    def _push_browser_overlay(self):
        browser = self.browser
        if not (browser and browser.alive):
            return
        with self._net_lock:
            rows = list(self.netlog[-40:])
            count = len(self.netlog)
        try:
            browser.update_overlay(
                self.state['browser_progress'], rows, count)
        except Exception:
            pass

    # ---------- 浏览器管理 ----------
    def _start_browser(self, automated=False):
        from . import web_flow
        from .browser_win import NativeBrowser
        if automated:
            self._set_browser_progress(
                1, '正在启动浏览器', '准备打开 VPN 授权页面')
        self.log('[worker] 启动内嵌浏览器面板')
        self.browser = NativeBrowser(
            self.main_window, self.storage_path, log=self.log,
            visible=self.browser_visible)
        self.page = self.browser.start(web_flow.URL)
        self.browser.set_visible(self.browser_visible)
        if not automated:
            self._set_browser_progress(
                0, '等待自动化任务', '浏览器已就绪，可手动操作', 'idle')

    def _netlog_path(self):
        from . import config as _cfg
        return os.path.join(_cfg.BASE, 'netlog.jsonl')

    def _drain_net(self):
        """排空抓包: UI 内存 + jsonl 落盘; xhr 记录摘要进运行日志"""
        if not (self.browser and self.browser.alive):
            return
        try:
            recs = self.browser.drain_net()
        except Exception:
            return
        if not recs:
            return
        with self._net_lock:
            self.netlog.extend(recs)
            if len(self.netlog) > NETLOG_MAX_UI:
                self.netlog = self.netlog[-NETLOG_MAX_UI:]
        try:
            p = self._netlog_path()
            with open(p, 'a', encoding='utf-8') as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + '\n')
            if os.path.getsize(p) > NETLOG_FILE_LIMIT:
                with open(p, 'r', encoding='utf-8') as f:
                    lines = f.readlines()[-800:]
                with open(p, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
        except OSError:
            pass
        for r in recs:
            if r.get('src') == 'xhr':
                url = str(r.get('url', ''))
                req = r.get('req')
                res = r.get('res')
                extra = ''
                if req:
                    extra += ' 参数=' + _log_text(req)
                if res:
                    extra += ' 返回=' + _log_text(res)
                self.log(f"[net] {r.get('method')} {r.get('status')} "
                         f'{url}{extra}')

    # ---------- 主循环 ----------
    def run(self):
        from . import vpn_connect, vpn_service, web_flow
        self.state['running'] = True
        self.log('[worker] 后台调度已启动')
        # 首次连接检查推迟 5s: 让资源给 GUI/WebView2 冷启动, 避免开局假死
        self._last_conn = time.time() - 25
        initial_cfg = self.cfg_get()
        self._desired_vpn = initial_cfg.get('vpn_name', '')
        self._authorization_schedule(initial_cfg)
        self._ras_watcher = vpn_connect.RasConnectionWatcher()
        if self._ras_watcher.available:
            self.log('[worker] 已启用 Windows RAS 连接事件监听')
        else:
            self.log('[worker] RAS 事件监听不可用，保留 30 秒轮询')
        try:
            while not self._stop_event.is_set():
                cfg = self.cfg_get()
                self.state['browser_up'] = \
                    bool(self.browser and self.browser.alive)
                # 原子领取手动/自动续期任务；取消后自动任务延迟 10 分钟再评估。
                renew_source = self._claim_renew(cfg)
                need_renew = renew_source is not None
                manual_open = self.browser_requested.is_set()
                self.browser_requested.clear()
                if manual_open:
                    self.log('[worker] 已领取手动打开浏览器请求')
                if need_renew:
                    self.log(f'[worker] 已领取授权/续期任务 ({renew_source})')
                if (need_renew or manual_open) and \
                        (self.browser is None or not self.browser.alive):
                    try:
                        self._start_browser(automated=need_renew)
                    except Exception as e:
                        self.log(f'[worker] 浏览器启动失败: {e}')
                        self._set_browser_progress(
                            1, '浏览器启动失败', str(e), 'failed')
                # 抓包排空 (1s 节拍)
                if time.time() - self._last_drain > 1:
                    self._last_drain = time.time()
                    self._drain_net()
                    # 浮层心跳: 页面脚本或站点 DOM 重建移除浮层时，最迟 1s 自愈。
                    self._push_browser_overlay()
                # 续期流程
                if need_renew:
                    self.log(f'[worker] 执行授权/续期流程 ({renew_source})')
                    self._set_browser_progress(
                        1, '浏览器已就绪', '准备打开授权页面')
                    cancelled = False
                    ok = False
                    try:
                        if self.renew_cancel_requested.is_set():
                            raise web_flow.RenewCancelled()
                        if not (self.page is not None and self.browser
                                and self.browser.alive):
                            raise RuntimeError('内嵌浏览器未能启动')
                        result = web_flow.ensure_authorized(
                            self.page, cfg, log=self.log,
                            manual=self.manual,
                            sms_poll=self.sms_poll,
                            sms_show=self.sms_show,
                            progress=self._set_browser_progress,
                            cancel=self.renew_cancel_requested.is_set)
                        ok = bool(result.get('ok')) \
                            if isinstance(result, dict) else bool(result)
                        expiries = result.get('expiries', {}) \
                            if isinstance(result, dict) else {}
                    except web_flow.RenewCancelled:
                        cancelled = True
                        self._renew_retry_after = time.time() + 600
                        current = self.state['browser_progress'].get('step', 1)
                        self._set_browser_progress(
                            current, '授权处理已中断',
                            '本次操作已停止，可随时重新发起', 'cancelled')
                        self.log('[worker] 用户已中断本次授权/续期')
                    except Exception as e:
                        self.log(f'[worker] 续期异常: {e}')
                        current = self.state['browser_progress'].get('step', 1)
                        self._set_browser_progress(
                            current, '自动化执行异常', str(e), 'failed')
                    finally:
                        self._finish_renew()
                    if ok:
                        self._save_authorization_result(
                            expiries, renew_source)
                        self._renew_retry_after = 0.0
                        # 仅解除失败熔断；若已有拨号请求仍在确认期，不得因续期完成
                        # 清除门控并重复拨号。
                        self.reset_backoff(
                            trigger_check=False,
                            clear_confirmation=False)
                        self._set_browser_progress(
                            5, '授权/续期已完成',
                            '远端列表已同步成功，无需刷新页面',
                            'completed')
                        self.log('[worker] 授权/续期完成')
                        self._reconnect_after_authorization(cfg)
                    elif not cancelled:
                        self._renew_retry_after = time.time() + 600
                        if self.state['browser_progress'].get('status') \
                                == 'running':
                            current = self.state['browser_progress'].get(
                                'step', 1)
                            self._set_browser_progress(
                                current, '授权/续期未完成',
                                '将在 10 分钟后自动重试', 'failed')
                        self.log('[worker] 授权/续期失败, 10分钟后重试')
                # 断线重连 (30s 检查一次; 失败后退避, 防止密码错误时反复撞服务器)
                # 状态常更新供 UI 展示; 自动拨号/重连受 auto_connect 开关
                # 控制 (默认关: 本工具是 VPN 管理器, 连接由用户手动发起)
                now = time.time()
                if self._ras_watcher.available and self._ras_watcher.wait(0):
                    # RAS 建连/断开期间会连续产生状态变化，短暂合并后统一刷新。
                    self._schedule_ras_refresh(now)
                if self._ras_refresh_due and now >= self._ras_refresh_due:
                    handled = self._try_vpn_operation(
                        lambda: self._handle_ras_event(cfg))
                    self._ras_refresh_due = 0.0 if handled else now + 0.5
                self._try_vpn_operation(
                    lambda: self._check_connection_confirmation(cfg))
                if self._event_reconnect_pending and \
                        now >= self._retry_after:
                    self._event_reconnect_pending = False
                    self._queue_auto_connect(cfg, source='ras_event')
                if now - self._last_conn > 30:
                    self._last_conn = time.time()
                    self._queue_auto_connect(cfg)
                if self._stop_event.wait(0.2):
                    break
        except Exception as e:
            self.log(f'[worker] 调度线程退出: {e}')
        finally:
            try:
                if self.browser:
                    self.browser.destroy()
            except Exception:
                pass
            try:
                if self._ras_watcher:
                    self._ras_watcher.close()
            except Exception:
                pass
            self.state['running'] = False
            self.state['browser_up'] = False
            self.log('[worker] 后台调度已停止')
