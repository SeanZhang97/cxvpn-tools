# -*- coding: utf-8 -*-
"""api.py - pywebview JS 桥接: UI 调用的全部后端能力"""
import datetime
import json
import os
import queue
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import nullcontext

from core import config as cfgmod
from core import (config_maintenance, ip_info, proxy_guard, ras_cred, routing,
                  sms_receiver, vpn_connect, vpn_os, windows_desktop)
from core import codex_proxy
from core import vpn_service
from core import app_update
from core.version import APP_NAME, APP_VERSION
from core.mihomo_activity import MihomoActivityRelay
from core.mihomo_telemetry import MihomoTelemetryRelay
from core.routing_speedtest import RoutingTestJobs
from core.routing_api_tasks import RoutingTaskApi
from core.routing_aggregate import AggregateSelectionApi
from core.routing_tasks import operation_scope
from core.routing_updates import RoutingUpdateWorker
from core.routing_network import RoutingNetworkWorker
from core.ui_state_stream import UiStateStream
from core.worker import Worker

LOG_MAX = 500
LOG_FILE_MAX = 2 * 1024 * 1024  # 运行日志文件上限 2MB, 超限截断保留尾部


def _log_single_line(value):
    """将单条事件规范化为一行，避免多行模型回复破坏日志结构。"""
    return str(value).replace('\r', r'\r').replace('\n', r'\n')


class _LogWriter:
    """有界异步运行日志写入器，避免磁盘 I/O 阻塞业务线程。"""

    def __init__(self, path, limit):
        self.path = path
        self.limit = limit
        self._queue = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name='run-log-writer', daemon=True)
        self._thread.start()

    def submit(self, line):
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            # 日志不能反向阻塞业务；丢弃最旧的一条后保留最新证据。
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(line)
            except queue.Empty:
                pass

    def _rotate_if_needed(self, stream):
        if stream.tell() <= self.limit:
            return stream
        stream.close()
        backup = self.path + '.1'
        try:
            if os.path.exists(backup):
                os.replace(backup, self.path + '.2')
            os.replace(self.path, backup)
        except OSError:
            pass
        return open(self.path, 'a', encoding='utf-8')

    def _run(self):
        stream = None
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    line = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    if stream is None:
                        stream = open(self.path, 'a', encoding='utf-8')
                    stream.write(line + '\n')
                    stream.flush()
                    stream = self._rotate_if_needed(stream)
                except (OSError, ValueError):
                    # 日志是旁路能力，磁盘错误不能影响业务。
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
                    stream = None
                finally:
                    self._queue.task_done()
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self, timeout=2):
        self._stop.set()
        self._thread.join(timeout=max(0, timeout))


def _safe_console_write(line):
    """控制台编码不可影响业务；不支持的字符按当前代码页转义。"""
    stream = getattr(sys, 'stdout', None)
    if stream is None:
        return
    try:
        print(line, file=stream, flush=True)
        return
    except (OSError, UnicodeError, ValueError):
        pass
    try:
        encoding = getattr(stream, 'encoding', None) or 'utf-8'
        safe_line = str(line).encode(
            encoding, errors='backslashreplace').decode(
                encoding, errors='replace')
        stream.write(safe_line + '\n')
        stream.flush()
    except Exception:
        # 日志是旁路能力，绝不能反向中断业务事务或异常回滚。
        pass


class Api(RoutingTaskApi, AggregateSelectionApi):
    def __init__(self):
        self.cfg = cfgmod.load()
        self._lock = threading.Lock()
        self._vpn_action_lock = threading.Lock()
        self._repair_lock = threading.Lock()
        self._routing_lock = threading.Lock()
        self._routing_revision = 0
        self._repairing = False
        self._repair_result = None
        self._repair_run_id = 0
        self.logs = []
        self._manual = None
        self._manual_clicks = None
        self._manual_event = threading.Event()
        self._manual_id = 0
        self._sms_ui = False          # 是否展示手动短信输入弹窗
        self._sms_ui_id = 0
        self._manual_sms = None       # UI 手动提交的短信验证码
        self._manual_sms_ev = threading.Event()
        self._desktop = None
        self._pending_notice = None
        self._ui_ready_callback = None
        self._routing_stream_state = ''
        self._routing_observability_available = None
        self._log_version = 0
        self._ui_state_stream = None
        self._app_download = app_update.UpdateDownloadState()
        self._app_download_cancel = threading.Event()
        self._app_update_lock = threading.Lock()
        self._ip_info_lock = threading.Lock()
        self._ip_info_thread = None
        self._ip_info_refresh_queued = False
        self._ip_info_failures = 0
        self._ip_info_retry_after = 0.0
        self._ip_info = {
            'loading': False,
            'checked_at': 0,
            'updated_at_text': '',
            'local': ip_info._empty_entry(),
            'domestic': ip_info._empty_entry(),
            'overseas': ip_info._empty_entry(),
        }
        self.worker = Worker(self._cfg_get, self.log,
                             manual=self.request_manual_captcha,
                             sms_poll=self.poll_manual_sms,
                             sms_show=self.sms_show,
                             authorization_save=self._save_authorization_state)
        # pywebview 会遍历 js_api 的全部公开属性生成 JS 桥: worker 内持有
        # main_window (WinForms 原生对象), 遍历会陷入 .NET 结构体自引用
        # (Bounds.Empty.Empty…) 无限递归 + 长时间反射开销 = 启动假死 20s。
        # 标记不可序列化, 桥接遍历跳过 (UI 也不需要直接调 worker)。
        self.worker._serializable = False
        self.routing = routing.RoutingManager(self.log)
        self.routing._serializable = False
        self.routing_history = config_maintenance.ConfigHistory()
        self.routing_history._serializable = False
        self.routing_test_jobs = RoutingTestJobs(self.log)
        self.routing_test_jobs._serializable = False
        self.routing_updates = RoutingUpdateWorker(
            self._cfg_get, self.routing, self._routing_lock, self.log,
            task_runner=self._routing_provider_task)
        self.routing_updates._serializable = False
        self.routing_network = RoutingNetworkWorker(
            self._cfg_get, self.routing, self._routing_lock,
            lambda value, target: self._apply_routing_locked(
                value, '自动物理出口切换', expected_interface=target), self.log)
        self.routing_network._serializable = False
        self.routing_telemetry = MihomoTelemetryRelay(
            self._cfg_get, self.routing, self.log, self._poke_ui_state)
        self.routing_telemetry._serializable = False
        self.routing_activity = MihomoActivityRelay(
            self._cfg_get, self.routing, self.log)
        self.routing_activity._serializable = False
        self._ui_state_stream = UiStateStream(
            self._build_ui_snapshot, self.log, interval=0.5)
        self._ui_state_stream._serializable = False
        self._log_writer = _LogWriter(
            os.path.join(cfgmod.BASE, 'run.log'), LOG_FILE_MAX)
        # 存储可能在 _lock 内调用，不能重入 log() 再获取同一个非重入锁。
        cfgmod.state_store.configure_logger(lambda message: self._log_writer.submit(
            _log_single_line(f'{datetime.datetime.now():%H:%M:%S} {message}')))

    # ---------- 基础设施 ----------
    def _cfg_get(self):
        with self._lock:
            return json.loads(json.dumps(self.cfg))

    def _save_authorization_state(self, authorization):
        """Worker 回写服务端授权结果；该字段不接受前端旧快照覆盖。"""
        with self._lock:
            self.cfg['authorization'] = json.loads(
                json.dumps(authorization))
            cfgmod.save(self.cfg)

    def log(self, msg):
        line = _log_single_line(
            f'{datetime.datetime.now():%H:%M:%S} {msg}')
        with self._lock:
            self.logs.append(line)
            self._log_version = getattr(self, '_log_version', 0) + 1
            if len(self.logs) > LOG_MAX:
                self.logs = self.logs[-LOG_MAX:]
        writer = getattr(self, '_log_writer', None)
        if writer is not None:
            writer.submit(line)
        else:
            # 兼容极简测试替身；正式 Api 在 __init__ 中使用异步写入器。
            try:
                p = os.path.join(cfgmod.BASE, 'run.log')
                with open(p, 'a', encoding='utf-8') as stream:
                    stream.write(line + '\n')
            except OSError:
                pass
        _safe_console_write(line)
        self._poke_ui_state()

    def get_logs(self):
        with self._lock:
            return list(self.logs)

    def start(self):
        # 启动自检：上一轮异常退出可能残留指向本机死端口的系统代理，
        # 先修复再加载业务，避免残留代理影响内置浏览器与网络请求。
        try:
            repaired = proxy_guard.startup_check(log=self.log)
            if repaired:
                codex_proxy.restore(logger=self.log)
                self._pending_notice = {
                    'title': '系统代理已修复',
                    'message': (f'检测到上次异常退出残留的本地代理 '
                                f'{repaired}，已自动关闭'),
                }
        except Exception as exc:
            self.log(f'[proxy] 系统代理残留自检失败: {exc}')
        cfg = self._cfg_get()
        credentials = cfg.get('creds', {})
        if isinstance(credentials, dict):
            for name, credential in credentials.items():
                if not isinstance(credential, dict):
                    continue
                user = credential.get('user', '')
                password = credential.get('pass', '')
                if not (name and user and password):
                    continue
                saved, error = vpn_service.sync_credentials(
                    name, user, password)
                self.log(
                    f'[vpn] {name} 启动时同步 Windows 凭据 -> '
                    f'{"成功" if saved else f"失败 (错误 {error})"}')
        statuses = cfg.get('credential_status', {})
        if isinstance(statuses, dict):
            obsolete = {'pending_authorization', 'validated',
                        'validation_failed'}
            if any(status in obsolete for status in statuses.values()):
                with self._lock:
                    self.cfg['credential_status'] = {
                        name: status for name, status in statuses.items()
                        if status not in obsolete
                    }
                    cfgmod.save(self.cfg)
        if not self.worker.is_alive():
            self.worker.start()
        self.routing_updates.start()
        self.routing_network.start()
        state_stream = getattr(self, '_ui_state_stream', None)
        if state_stream is not None:
            state_stream.start()
        telemetry = getattr(self, 'routing_telemetry', None)
        if telemetry is not None:
            telemetry.start()
        activity = getattr(self, 'routing_activity', None)
        if activity is not None:
            activity.start()
        if hasattr(self, '_routing_lock'):
            self._start_routing_standby_reconcile()

    def _start_routing_standby_reconcile(self, source='启动'):
        """启动时对账系统代理并在后台恢复待机核心，不阻塞窗口首屏。"""
        thread = getattr(self, '_routing_standby_thread', None)
        if thread is not None and thread.is_alive():
            self.log(f'[routing] {source}待机核心复核已在后台排队，无需重复提交')
            return
        self.log(f'[routing] {source}待机核心复核任务已提交')

        def reconcile():
            self.log(f'[routing] 后台已领取{source}待机核心复核任务，等待路由互斥锁')
            with self._routing_lock:
                self.log(f'[routing] {source}待机核心复核开始执行')
                current = routing.normalize_config(
                    (self._cfg_get().get('routing') or {}))
                state = self.routing._service_state()

                # 异常重启可能留下仍指向 CXVPN mixed-port 的 Windows 手动代理，
                # 但持久化配置已经是关闭状态；反过来，配置仍是开启状态时也要
                # 在服务已运行但系统代理未恢复的情况下补做一次接管。只处理精确
                # 匹配当前 mixed-port 的端点，不触碰第三方代理配置。
                expected_proxy = (
                    f'http://127.0.0.1:{current["mixed_port"]}'
                    if current['capture_mode'] == 'system-proxy' else '')
                actual_proxy = routing.windows_system_proxy()
                if expected_proxy and current['enabled']:
                    runtime_ok = bool(
                        state.get('installed') and
                        state.get('backend') == 'native' and
                        state.get('runtime_running') and
                        state.get('runtime_mode') == 'active')
                    if actual_proxy != expected_proxy or not runtime_ok:
                        self.log(
                            f'[routing] {source}发现代理接管未完整恢复，'
                            f'系统代理={actual_proxy or "关闭"}，'
                            f'核心运行={runtime_ok}，开始恢复代理接管')
                        if (state.get('installed') and
                                state.get('backend') == 'native' and
                                self.routing._can_fast_toggle_system_proxy(
                                    current, state)):
                            result = self.routing._fast_toggle_system_proxy(
                                current, True)
                        else:
                            result = self.routing.apply(current)
                        if not result.get('ok', True):
                            raise routing.RoutingError(
                                result.get('msg') or '启动时恢复系统代理失败')
                        self.log('[routing] 启动时系统代理恢复成功')
                    else:
                        self.log('[routing] 启动时系统代理已与配置一致')
                        self.routing._sync_codex_proxy(current, f'{source}代理有效校正')
                    return

                if current['enabled'] and state.get('runtime_running') \
                        and state.get('runtime_mode') == 'active':
                    self.log(f'[routing] {source}确认 TUN 核心已运行，校正 Codex 配置')
                    self.routing._sync_codex_proxy(current, f'{source}代理有效校正')
                    return

                if expected_proxy and not current['enabled'] \
                        and actual_proxy == expected_proxy:
                    self.log(
                        '[routing] 启动时发现已关闭配置仍残留 CXVPN 系统代理，'
                        '开始关闭残留接管')
                    if (state.get('installed') and
                            state.get('backend') == 'native' and
                            self.routing._can_fast_toggle_system_proxy(
                                current, state)):
                        result = self.routing._fast_toggle_system_proxy(
                            current, False)
                        if not result.get('ok', True):
                            raise routing.RoutingError(
                                result.get('msg') or '启动时关闭系统代理失败')
                    else:
                        proxy_guard.set_proxy_enabled(False)
                    self.log('[routing] 启动时残留系统代理已关闭')

                if not any(item.get('enabled')
                           for item in current['proxy_providers']):
                    self.log('[routing] 待机核心复核跳过：当前无需待机节点核心')
                    return
                if (not state.get('installed') or
                        state.get('backend') != 'native'):
                    self.log('[routing] 待机核心复核跳过：原生服务尚未安装')
                    return
                if (state.get('runtime_mode') == 'standby' and
                        state.get('runtime_running') and
                        state.get('service_version') ==
                        routing._routing_service.SERVICE_VERSION):
                    self.log('[routing] 待机核心复核完成：当前版本已在运行')
                    return
                result = self.routing.apply(current)
                if result.get('warnings'):
                    self.log('[routing] 待机核心复核完成：代理保持关闭，临时核心降级可用')
                else:
                    self.log('[routing] 待机核心复核成功')
                self._poke_ui_state()

        def guarded_reconcile():
            try:
                reconcile()
            except Exception as exc:
                self.log(
                    f'[routing] 待机核心复核失败：{type(exc).__name__}；'
                    '代理保持关闭，订阅操作将使用临时核心')

        thread = threading.Thread(
            target=guarded_reconcile, daemon=True,
            name='routing-standby-reconcile')
        self._routing_standby_thread = thread
        thread.start()

    def _attach_desktop(self, desktop):
        self._desktop = desktop

    def _os_shutdown_cleanup(self):
        """系统关机/注销前同步清扫本地代理残留；异常不影响退出。"""
        try:
            cleared = proxy_guard.shutdown_cleanup(log=self.log)
            if cleared:
                self.log(f'[proxy] 关机前已关闭本地系统代理 {cleared}')
        except Exception as exc:
            self.log(f'[proxy] 关机代理清扫失败: {exc}')

    def flush_pending_desktop_notice(self):
        """托盘就绪后投递启动自检结果的气泡提示（不弹主窗口）。"""
        desktop = getattr(self, '_desktop', None)
        pending = self._pending_notice
        if desktop is None or pending is None:
            return
        try:
            desktop.notify(pending['title'], pending['message'])
        except Exception:
            self.log('[proxy] 托盘提示投递失败')
        self._pending_notice = None

    def _attach_ui_ready(self, callback):
        self._ui_ready_callback = callback

    def ui_ready(self):
        """由前端在本地配置完成首屏预展示后通知入口显示窗口。"""
        callback = self._ui_ready_callback
        if callback:
            callback()
        return True

    def _manual_connection_action(self):
        """兼容测试替身，并在真实 Worker 上串行化手动与自动拨号。"""
        factory = getattr(self.worker, 'manual_connection_action', None)
        guard = factory() if callable(factory) else None
        return guard if hasattr(guard, '__enter__') else nullcontext()

    def _poke_ui_state(self):
        stream = getattr(self, '_ui_state_stream', None)
        if stream is not None:
            stream.poke()

    @staticmethod
    def _config_for_ui(value):
        """移除只应由后端持有的 Mihomo Controller 连接信息。"""
        result = json.loads(json.dumps(value or {}))
        routing_config = result.get('routing')
        if isinstance(routing_config, dict):
            routing_config.pop('controller_port', None)
            routing_config.pop('controller_secret', None)
        return result

    @staticmethod
    def _routing_result_for_ui(value):
        result = json.loads(json.dumps(value or {}))

        def scrub(item):
            if isinstance(item, dict):
                item.pop('controller_port', None)
                item.pop('controller_secret', None)
                for child in item.values():
                    scrub(child)
            elif isinstance(item, list):
                for child in item:
                    scrub(child)

        scrub(result)
        return result

    def _routing_with_private_fields(self, value):
        """把 UI 草稿与当前后端私有字段合并，避免预检/应用时轮换 Secret。"""
        incoming = json.loads(json.dumps(value or {}))
        current = routing.normalize_config(
            self._cfg_get().get('routing') or routing.default_config())
        incoming['controller_port'] = current['controller_port']
        incoming['controller_secret'] = current['controller_secret']
        return incoming

    def _build_ui_snapshot(self):
        """聚合跨桥状态；快照不包含配置、凭据或 Controller Secret。"""
        with self._lock:
            logs_version = self._log_version
        with self._ip_info_lock:
            current_ip_info = json.loads(json.dumps(self._ip_info))
        manual = json.loads(json.dumps(self._manual)) if self._manual else None
        sms = {'id': self._sms_ui_id} if self._sms_ui else None
        app_download = getattr(self, '_app_download', None)
        return {
            'state': self.get_state(),
            'vpn_status': self.vpn_status(),
            'captcha': manual,
            'sms': sms,
            'browser': self.get_browser(),
            'logs_version': logs_version,
            'ip_info': current_ip_info,
            'app_update': app_download.snapshot() if app_download is not None
                          else {'phase': 'idle'},
            'routing_telemetry': self.routing_telemetry.snapshot(),
            'routing_revision': getattr(self, '_routing_revision', 0),
        }

    def get_ui_state_snapshot(self):
        """订阅初始化或恢复时获取当前版本化快照。"""
        return self._ui_state_stream.current()

    def wait_ui_state(self, after_version=0, timeout=25):
        """阻塞等待版本变化；超时返回心跳，不制造固定周期跨桥轮询。"""
        return self._ui_state_stream.wait(after_version, timeout)

    def set_routing_telemetry_active(self, active):
        """页面生命周期只传布尔意图，Controller 细节始终留在后端。"""
        return self.routing_telemetry.set_active(active)

    def set_routing_activity_view(self, view):
        """按当前可见页面启停连接或核心日志流。"""
        return self.routing_activity.set_view(view)

    def get_routing_activity_snapshot(self, view):
        return self.routing_activity.snapshot(view)

    def wait_routing_activity(self, after_version=0, view='connections',
                              timeout=25):
        return self.routing_activity.wait(after_version, view, timeout)

    def clear_routing_activity(self, view):
        cleared = self.routing_activity.clear(view)
        if cleared:
            self.log(f'[routing-activity] UI 已清空 {view} 本地历史')
        return cleared

    def close_routing_connection(self, connection_id=''):
        target = '单个连接' if str(connection_id or '').strip() else '全部连接'
        self.log(f'[routing-activity] {target}关闭请求已提交')
        with self._routing_lock:
            self.log(f'[routing-activity] 后台已领取{target}关闭请求，开始执行')
            try:
                result = self.routing_activity.close_connection(connection_id)
                self.log(f'[routing-activity] {target}关闭成功')
                return result
            except Exception as exc:
                self.log(
                    f'[routing-activity] {target}关闭失败: '
                    f'{type(exc).__name__}')
                return {'ok': False, 'msg': '关闭连接失败，请确认代理核心正在运行'}

    def shutdown(self):
        """解除可能的人工等待并停止后台线程，供托盘退出和窗口关闭使用。"""
        self._manual_event.set()
        self._manual_sms_ev.set()
        self._sms_ui = False
        self.routing_telemetry.stop()
        self.routing_activity.stop()
        self._ui_state_stream.stop()
        self.routing_updates.stop()
        self.routing_network.stop()
        self.worker.stop()
        # 正常退出即视为优雅结束，清除脏标记；仅系统关机路径会在此前
        # 由 _os_shutdown_cleanup 先清扫代理（用户点击退出的场景不碰代理）。
        try:
            proxy_guard.mark_clean()
        except Exception as exc:
            self.log(f'[proxy] 清除代理脏标记失败: {exc}')
        writer = getattr(self, '_log_writer', None)
        if writer is not None:
            writer.close()

    # ---------- 配置 ----------
    def get_config(self):
        return self._config_for_ui(self._cfg_get())

    def get_app_version(self):
        return {'name': APP_NAME, 'version': APP_VERSION}

    def check_app_update(self, manual=False):
        result = app_update.check_latest()
        if result.get('ok'):
            with self._lock:
                self.cfg.setdefault('app_update', {})['last_check'] = time.time()
                cfgmod.save(self.cfg)
        self.log('[app-update] %s check completed: ok=%s newer=%s manual=%s' %
                 (APP_VERSION, result.get('ok'), result.get('newer'), manual))
        return result

    def open_app_release(self, url):
        result = app_update.open_release(url)
        self.log('[app-update] release page open: ok=%s' % result.get('ok'))
        return result

    # ---------- 软件升级（下载 + 静默安装） ----------

    def start_app_update_download(self, installer_url='', latest_version='',
                                  expected_sha256=''):
        """UI 确认升级后启动后台下载；进度经 UI 状态流推送。"""
        with self._app_update_lock:
            phase = self._app_download.snapshot().get('phase')
            if phase == 'downloading':
                return {'ok': False, 'msg': '升级包正在下载中，请稍候'}
            if phase == 'installing':
                return {'ok': False, 'msg': '升级正在进行，请稍候'}
            if phase == 'downloaded':
                return {'ok': True, 'msg': '升级包已就绪，可直接开始升级'}
            version = str(latest_version or '').strip()
            self._app_download.begin(version)
            self._app_download_cancel = threading.Event()
            url = str(installer_url or '').strip()
            sha = str(expected_sha256 or '').strip().lower()
        self.log('[app-update] 升级包下载任务已提交: version=%s read_timeout=%ss' % (
            version, app_update.DOWNLOAD_READ_TIMEOUT))
        threading.Thread(
            target=self._run_app_download, name='app-update-download',
            args=(url, version, sha), daemon=True).start()
        return {'ok': True, 'msg': f'开始下载新版本 {version}'.strip()}

    def _run_app_download(self, url, version, expected_sha256):
        """下载工作线程：消费下载任务并回写终态，避免阻塞其它后台任务。"""
        self.log('[app-update] 后台已领取升级包下载任务，开始执行')
        started = time.monotonic()
        result = app_update.download_installer(
            url, expected_sha256=expected_sha256,
            state=self._app_download,
            cancel_event=self._app_download_cancel, log=self.log)
        elapsed = time.monotonic() - started
        if result.get('ok'):
            app_update.prune_old_installers(result.get('path'))
            self.log('[app-update] 升级包下载任务成功: elapsed=%.1fs' % elapsed)
            self._poke_ui_state()
            return
        if result.get('cancelled'):
            self._app_download.update(
                phase='cancelled', progress=0, speed_bps=0,
                msg='已取消下载新版本')
            self.log('[app-update] 升级包下载任务已取消: elapsed=%.1fs' % elapsed)
        else:
            self._app_download.update(
                phase='failed', progress=0, speed_bps=0,
                msg=result.get('msg') or '下载升级包失败')
            self.log('[app-update] 升级包下载任务失败: elapsed=%.1fs' % elapsed)
        self._poke_ui_state()

    def cancel_app_update_download(self):
        with self._app_update_lock:
            phase = self._app_download.snapshot().get('phase')
            if phase != 'downloading':
                return {'ok': False, 'msg': '当前没有正在进行的下载'}
        self._app_download_cancel.set()
        self.log('[app-update] 已请求取消升级包下载')
        return {'ok': True, 'msg': '已请求取消下载'}

    def apply_app_update(self):
        """下载完成后启动静默安装器；安装器负责退出旧版并部署新版。"""
        with self._app_update_lock:
            snapshot = self._app_download.snapshot()
            phase = snapshot.get('phase')
            if phase == 'installing':
                return {'ok': False, 'msg': '升级已在进行中'}
            if phase != 'downloaded':
                return {'ok': False, 'msg': '升级包尚未下载完成'}
            installer_path = snapshot.get('path')
            version = snapshot.get('version')
            self._app_download.update(
                phase='installing', speed_bps=0, msg='正在启动升级…')
        fallback_dir = os.path.dirname(os.path.abspath(sys.executable))
        result = app_update.launch_installer(
            installer_path, version, fallback_dir=fallback_dir, log=self.log)
        if not result.get('ok'):
            self._app_download.update(
                phase='failed', progress=0,
                msg=result.get('msg') or '启动升级失败')
            self.log('[app-update] 启动升级失败: %s' % result.get('msg'))
            self._poke_ui_state()
            return result
        threading.Thread(
            target=self._monitor_app_update_install, args=(version,),
            name='app-update-install-monitor', daemon=True).start()
        self._poke_ui_state()
        return result

    def _monitor_app_update_install(self, version,
                                    timeout=app_update.INSTALL_MONITOR_TIMEOUT):
        """跟踪静默安装；超时未完成则把 UI 恢复为可重试状态。"""
        deadline = time.monotonic() + timeout
        self.log('[app-update] 升级监控任务已提交: version=%s timeout=%ss' % (
            version, timeout))
        while time.monotonic() < deadline:
            time.sleep(3)
            if app_update.installed_display_version() == str(version or ''):
                self.log(
                    '[app-update] 新版本 %s 已安装就绪，等待守护进程重启' % version)
                return
        with self._app_update_lock:
            if self._app_download.snapshot().get('phase') == 'installing':
                self._app_download.update(
                    phase='failed', progress=0,
                    msg='升级未完成：安装可能被取消或失败，可稍后重试')
                self._poke_ui_state()
        self.log('[app-update] 升级监控超时: version=%s timeout=%ss' % (
            version, timeout))

    def save_config(self, cfg):
        if not isinstance(cfg, dict) or ('proxy_providers' in cfg and 'routing' not in cfg):
            raise ValueError('分流配置必须通过专用配置入口提交')
        with self._lock:
            old_default = self.cfg.get('vpn_name', '')
            old_auto_connect = bool(self.cfg.get('auto_connect', False))
            authorization = self.cfg.get('authorization') or {}
            routing_config = self.cfg.get('routing') or routing.default_config()
            committed = json.loads(json.dumps(self.cfg))
            committed.update(json.loads(json.dumps(cfg)))
            committed['authorization'] = authorization
            # 分流状态只能通过 apply_routing 原子变更，避免普通设置保存绕过预检。
            committed['routing'] = routing_config
            cfgmod.save(committed)
            self.cfg = committed
            new_default = self.cfg.get('vpn_name', '')
            new_auto_connect = bool(self.cfg.get('auto_connect', False))
        self.log('[config] 已保存')
        if new_default != old_default:
            self.worker.update_default_target(new_default)
        elif new_auto_connect and not old_auto_connect:
            self.worker.enable_auto_connect()
        state_stream = getattr(self, '_ui_state_stream', None)
        if state_stream is not None:
            state_stream.set_interval(0.5)
        self._poke_ui_state()
        return True

    # ---------- 统一域名分流 ----------
    def get_routing_bootstrap(self):
        started_at = time.monotonic()
        self.log('[routing] UI 首屏快照读取开始，不执行 VPN/网卡/TUN 慢扫描')
        try:
            config, revision = self._routing_snapshot()
            result = self.routing.bootstrap(config)
            result['routing_revision'] = revision
            self.log(
                f'[routing] UI 首屏快照就绪，耗时 {time.monotonic() - started_at:.3f} 秒')
            return self._routing_result_for_ui(result)
        except Exception as exc:
            self.log(
                f'[routing] UI 首屏快照读取失败，耗时 '
                f'{time.monotonic() - started_at:.3f} 秒: {exc}')
            return {'ok': False, 'msg': str(exc)}

    def get_routing_setup(self):
        try:
            config, revision = self._routing_snapshot()
            with operation_scope(lambda: self._routing_commit(revision)):
                result = self.routing.setup(config)
            return self._routing_response(result, revision)
        except Exception as exc:
            self.log(f'[routing] 读取配置失败: {exc}')
            return {'ok': False, 'msg': str(exc)}

    def get_routing_status(self):
        try:
            return self.routing.status(self._cfg_get().get('routing') or {})
        except Exception as exc:
            return {'ok': False, 'running': False, 'msg': str(exc)}

    def get_routing_proxies(self):
        try:
            config, revision = self._routing_snapshot()
            groups = self.routing.proxy_overview(config)
            return self._routing_response({'ok': True, 'groups': groups}, revision)
        except Exception as exc:
            return {'ok': False, 'msg': str(exc), 'groups': []}

    def get_routing_observability(self):
        """低频返回脱敏连接摘要；实时速率由 Mihomo WebSocket 提供。"""
        started_at = time.monotonic()
        try:
            result = self.routing.connection_observability(
                self._cfg_get().get('routing') or {})
            available = bool(result.get('available'))
            if available != self._routing_observability_available:
                self.log(
                    f'[routing-stream] 脱敏连接摘要'
                    f'{"恢复" if available else "不可用"}，耗时 '
                    f'{time.monotonic() - started_at:.3f} 秒')
                self._routing_observability_available = available
            return {'ok': True, 'observability': result}
        except Exception as exc:
            self.log(
                f'[routing-stream] 脱敏连接摘要读取失败，耗时 '
                f'{time.monotonic() - started_at:.3f} 秒: {type(exc).__name__}')
            return {'ok': False, 'msg': str(exc)}

    def get_codex_status(self):
        """返回 Codex 配置同步状态摘要，不读取或返回配置正文。"""
        try:
            current = routing.normalize_config(
                self._cfg_get().get('routing') or routing.default_config())
            summary = codex_proxy.status(logger=self.log)
        except (OSError, ValueError, TypeError, routing.RoutingError) as exc:
            self.log(f'[codex] 读取状态失败：{type(exc).__name__}')
            return {
                'ok': False,
                'config_exists': False,
                'config_path': '',
                'parse_error': str(exc),
                'snapshot_exists': False,
                'snapshot_fields': 0,
                'last_mixed_port': 0,
                'synced_fields': 0,
                'deviated_fields': [],
                'mixed_port': '',
                'system_proxy_mode': False,
                'routing_enabled': False,
                'residual_fields': [],
                'restart_required_after_change': True,
            }
        return {
            'ok': True,
            'config_exists': summary['config_exists'],
            'config_path': summary['path'],
            'parse_error': summary['parse_error'],
            'snapshot_exists': summary['snapshot_exists'],
            'snapshot_fields': summary['snapshot_fields'],
            'last_mixed_port': summary['last_mixed_port'],
            'synced_fields': summary['synced_fields'],
            'deviated_fields': summary['deviated_fields'],
            'residual_fields': summary.get('residual_fields', []),
            'mixed_port': current['mixed_port'],
            'system_proxy_mode': current['capture_mode'] == 'system-proxy',
            'routing_enabled': bool(current['enabled']),
            'restart_required_after_change': True,
        }

    def sync_codex_proxy(self):
        """手动把当前 mixed_port 同步到 Codex 配置；端口只从本地配置读取。"""
        started = time.monotonic()
        self.log('[codex] 手动同步请求已提交（UI）')
        try:
            current = routing.normalize_config(
                self._cfg_get().get('routing') or routing.default_config())
            self.log(f'[codex] 手动同步已领取：mixed_port={current["mixed_port"]}')
            result = codex_proxy.sync(current['mixed_port'], logger=self.log)
            self.log(f'[codex] 手动同步执行完成，耗时={time.monotonic() - started:.2f}秒'
                     if result.get('ok')
                     else f'[codex] 手动同步执行失败，耗时={time.monotonic() - started:.2f}秒')
            if result.get('ok'):
                result['restart_required_after_change'] = True
            return result
        except (OSError, ValueError, TypeError, routing.RoutingError) as exc:
            self.log(f'[codex] 手动同步读取配置失败：{type(exc).__name__}')
            return {'ok': False, 'warning': str(exc)}

    def restore_codex_proxy(self):
        """关闭 Codex 代理：删除仍等于本工具最后写入值的托管字段。"""
        started = time.monotonic()
        self.log('[codex] 关闭代理请求已提交（UI）')
        result = codex_proxy.restore(logger=self.log)
        self.log(f'[codex] 关闭代理执行{"完成" if result.get("ok") else "失败"}，耗时={time.monotonic() - started:.2f}秒')
        if result.get('ok') and result.get('changed'):
            result['restart_required_after_change'] = True
        return result

    def read_codex_config(self):
        """只读返回 Codex 配置原文供 UI 查看；日志不记录内容。"""
        started = time.monotonic()
        self.log('[codex] 查看配置请求已提交（UI）')
        result = codex_proxy.read_config(logger=self.log)
        if result.get('ok'):
            self.log(f'[codex] 查看配置已返回，耗时={time.monotonic() - started:.2f}秒')
        return result

    def report_routing_stream_state(self, state, retry_seconds=0):
        """记录实时流量连接状态；不接收 URL、secret 或消息正文。"""
        value = str(state or '').strip().lower()
        if value not in {'idle', 'connecting', 'connected', 'reconnecting'}:
            return False
        try:
            retry = min(15, max(0, int(retry_seconds or 0)))
        except (TypeError, ValueError):
            retry = 0
        signature = f'{value}:{retry}'
        if signature == self._routing_stream_state:
            return True
        self._routing_stream_state = signature
        detail = f'，{retry} 秒后重试' if value == 'reconnecting' and retry else ''
        self.log(f'[routing-stream] 实时流量通道状态={value}{detail}')
        return True

    def select_routing_proxy(self, group_id, node_name):
        with self._routing_lock:
            try:
                return self.routing.select_proxy_node(
                    self._cfg_get().get('routing') or {}, group_id, node_name)
            except Exception as exc:
                self.log(f'[routing] 节点切换失败: {exc}')
                return {'ok': False, 'msg': str(exc)}

    def save_proxy_preference(
            self, provider_id, mode, node_name='', provider_draft=None,
            auto_policy=None):
        """持久化自动/手动节点偏好；运行态可安全热切换时立即生效。"""
        self.log('[routing] 节点偏好保存请求已提交')
        with self._routing_lock:
            try:
                self.log('[routing] 后台已领取节点偏好保存请求，开始校验并写入')
                current_cfg = self._cfg_get()
                current = routing.normalize_config(
                    current_cfg.get('routing') or {})
                target_id = str(provider_id or '').strip().lower()
                target_mode = str(mode or '').strip().lower()
                if target_mode not in routing.SELECTION_MODES:
                    raise routing.RoutingError('节点选择模式无效')
                provider = next((item for item in current['proxy_providers']
                                 if item['id'] == target_id), None)
                if not provider:
                    draft = (dict(provider_draft)
                             if isinstance(provider_draft, dict) else None)
                    if (not draft or
                            str(draft.get('id') or '').strip().lower() != target_id):
                        raise routing.RoutingError('代理订阅不存在，请先保存订阅配置')
                    current['proxy_providers'].append(draft)
                    current = routing.normalize_config(current)
                    provider = next((item for item in current['proxy_providers']
                                     if item['id'] == target_id), None)
                    if not provider:
                        raise routing.RoutingError('代理订阅保存失败，请重试')
                selected = str(node_name or '').strip()
                if target_mode == 'manual':
                    if not provider.get('enabled'):
                        raise routing.RoutingError('请先在订阅管理启用并保存该订阅')
                    snapshot = routing.subscription_store.load_node_snapshot(
                        provider)
                    names = {str(item.get('name') or '')
                             for item in snapshot.get('nodes') or []}
                    if selected not in names:
                        raise routing.RoutingError(
                            '所选节点不在当前持久化节点列表中，请先更新订阅')
                previous_mode = provider.get('selection_mode')
                previous_policy = json.dumps(
                    provider.get('auto_policy') or {}, ensure_ascii=False,
                    sort_keys=True)
                provider['selection_mode'] = target_mode
                provider['selected_node'] = (
                    selected if target_mode == 'manual' else '')
                if target_mode == 'auto' and auto_policy is not None:
                    provider['auto_policy'] = auto_policy
                normalized = routing.normalize_config(current)
                normalized_provider = next(
                    item for item in normalized['proxy_providers']
                    if item['id'] == target_id)
                if (target_mode == 'auto' and
                        (normalized_provider.get('auto_policy') or {}).get(
                            'enabled')):
                    routing.validate_auto_policy_nodes(normalized_provider)
                policy_changed = previous_policy != json.dumps(
                    normalized_provider.get('auto_policy') or {},
                    ensure_ascii=False, sort_keys=True)
                status = self.routing.status(current)
                structural_change = (
                    previous_mode != target_mode or policy_changed)
                if status.get('running') and structural_change:
                    self.log(
                        f'[routing] 节点优选策略开始重新应用: provider={target_id}')
                    try:
                        applied = self._apply_routing_locked(
                            normalized, 'proxy-preference')
                    except routing.RoutingError as apply_error:
                        # 运行中的控制端暂时不可达时，仍先保留用户的下次运行偏好；
                        # 不能因本次热切换失败而丢弃已明确提交的配置。
                        with self._lock:
                            committed = json.loads(json.dumps(self.cfg))
                            committed['routing'] = normalized
                            cfgmod.save(committed)
                            self.cfg = committed
                        self._routing_changed()
                        self.log(f'[routing] 运行时切换未确认，偏好已落盘: {type(apply_error).__name__}')
                        return self._routing_response({
                            'ok': True, 'config': normalized,
                            'status': self.routing.status(normalized, quick=True),
                            'requires_apply': True,
                            'msg': '节点偏好已保存；当前核心未连接，开启代理时将自动应用',
                        })
                    if not applied.get('ok'):
                        return applied
                    self.log(
                        f'[routing] 节点优选策略重新应用成功: provider={target_id}')
                    return self._routing_response({
                        **applied,
                        'groups': self.routing.proxy_overview(normalized),
                        'msg': '节点优选策略已保存并立即应用',
                        'requires_apply': False,
                    })
                runtime_result = None
                core_running = status.get('core_running', status.get('running', False))
                self._routing_changed()
                if core_running and target_mode == 'manual' and previous_mode == 'manual':
                    runtime_result = self.routing.select_proxy_node(
                        normalized, target_id, f'[{provider["name"]}] {selected}')
                try:
                    with self._lock:
                        committed = json.loads(json.dumps(self.cfg))
                        committed['routing'] = normalized
                        cfgmod.save(committed)
                        self.cfg = committed
                except Exception as save_error:
                    if runtime_result is not None:
                        old_provider = next(item for item in current_cfg['routing']['proxy_providers']
                                            if item['id'] == target_id)
                        try:
                            self.routing.select_proxy_node(current_cfg['routing'], target_id,
                                f'[{old_provider["name"]}] {old_provider["selected_node"]}')
                        except Exception as rollback_error:
                            self.log(f'[routing] 节点偏好保存回滚失败: {type(rollback_error).__name__}')
                            raise routing.RoutingError('节点偏好保存失败且无法确认原节点恢复，请刷新运行状态') from save_error
                    raise routing.RoutingError('节点偏好保存失败，已保留原配置') from save_error
                if runtime_result is not None:
                    self.routing.remember_selection(normalized, current_cfg['routing'])
                self._routing_changed()
                requires_apply = bool(core_running and structural_change)
                msg = ('节点偏好已保存，开启代理时将重新应用'
                       if requires_apply else
                       '节点偏好与订阅草稿已保存，请先在订阅管理启用该订阅'
                       if not provider.get('enabled') else
                       '节点偏好已保存并确认生效' if runtime_result is not None else
                       '节点偏好已保存，开启代理时将自动应用')
                self.log(f'[routing] 节点偏好保存成功: provider={target_id}, mode={target_mode}')
                result = {'ok': True, 'msg': msg, 'config': normalized,
                          'status': status, 'requires_apply': requires_apply}
                if runtime_result is not None:
                    result['groups'] = runtime_result.get('groups') or []
                self._poke_ui_state()
                return self._routing_response(result)
            except routing.RoutingError as exc:
                self.log(f'[routing] 节点偏好保存失败: {exc}')
                return {'ok': False, 'msg': str(exc)}
            except Exception as exc:
                self.log(f'[routing] 节点偏好保存失败: {type(exc).__name__}')
                return {'ok': False, 'msg': '节点偏好保存失败，请重试'}

    def refresh_routing_provider(self, provider_id):
        try:
            return self._routing_provider_task(provider_id, lambda config: self.routing.refresh_proxy_provider(config, provider_id))
        except routing.RoutingError as exc:
            return {'ok': False, 'msg': str(exc)}
        except Exception as exc:
            self.log(f'[routing-task] refresh_routing_provider失败: {type(exc).__name__}')
            return {'ok': False, 'msg': '订阅或节点操作失败，请重试或查看运行日志'}

    def preview_routing_provider(self, provider, network=None):
        try:
            return self._routing_provider_task((provider or {}).get('id'), lambda config: self.routing.preview_proxy_provider(provider, network))
        except routing.RoutingError as exc:
            return {'ok': False, 'msg': str(exc)}
        except Exception as exc:
            self.log(f'[routing-task] preview_routing_provider失败: {type(exc).__name__}')
            return {'ok': False, 'msg': '订阅或节点操作失败，请重试或查看运行日志'}

    def test_preview_routing_provider(self, provider, network=None):
        try:
            return self._routing_provider_task((provider or {}).get('id'), lambda config: self.routing.test_preview_proxy_provider(provider, network))
        except routing.RoutingError as exc:
            return {'ok': False, 'msg': str(exc)}
        except Exception as exc:
            self.log(f'[routing-task] test_preview_routing_provider失败: {type(exc).__name__}')
            return {'ok': False, 'msg': '订阅或节点操作失败，请重试或查看运行日志'}

    def start_preview_routing_test(self, provider, network=None):
        provider_copy = json.loads(json.dumps(provider or {}))
        network_copy = json.loads(json.dumps(network or {}))

        def runner(progress, cancel_event):
            return self._routing_provider_task(provider_copy.get('id'),
                lambda config: self.routing.test_preview_proxy_provider(
                    provider_copy, network_copy, progress, cancel_event))

        return self.routing_test_jobs.start(
            runner, '订阅节点测速',
            lambda exc: (str(exc) if isinstance(exc, routing.RoutingError)
                         else '订阅节点测速发生内部错误，请重试'))

    def start_routing_group_test(self, group_id):
        target = str(group_id or '').strip().lower()

        def runner(progress, cancel_event):
            return self._routing_provider_task(target,
                lambda config: self.routing.test_proxy_group_stream(
                    config, target, progress, cancel_event))

        return self.routing_test_jobs.start(
            runner, '代理组测速',
            lambda exc: (str(exc) if isinstance(exc, routing.RoutingError)
                         else '代理组测速发生内部错误，请重试'))

    def get_routing_test_job(self, job_id, after_version=-1):
        return self.routing_test_jobs.get(job_id, after_version)

    def cancel_routing_test_job(self, job_id):
        return self.routing_test_jobs.cancel(job_id)

    def import_routing_provider(self, provider, content, network=None):
        try:
            return self._routing_provider_task((provider or {}).get('id'), lambda config: self.routing.import_proxy_provider(provider, content, network))
        except routing.RoutingError as exc:
            return {'ok': False, 'msg': str(exc)}
        except Exception as exc:
            self.log(f'[routing-task] import_routing_provider失败: {type(exc).__name__}')
            return {'ok': False, 'msg': '订阅或节点操作失败，请重试或查看运行日志'}

    def test_routing_proxy_group(self, group_id):
        try:
            return self._routing_provider_task(group_id, lambda config: self.routing.test_proxy_group(config, group_id))
        except routing.RoutingError as exc:
            return {'ok': False, 'msg': str(exc)}
        except Exception as exc:
            self.log(f'[routing-task] test_routing_proxy_group失败: {type(exc).__name__}')
            return {'ok': False, 'msg': '订阅或节点操作失败，请重试或查看运行日志'}

    def test_routing_proxy_node(self, group_id, node_name):
        try:
            return self._routing_provider_task(group_id, lambda config: self.routing.test_proxy_node(config, group_id, node_name))
        except routing.RoutingError as exc:
            return {'ok': False, 'msg': str(exc)}
        except Exception as exc:
            self.log(f'[routing-task] test_routing_proxy_node失败: {type(exc).__name__}')
            return {'ok': False, 'msg': '订阅或节点操作失败，请重试或查看运行日志'}
    def preview_routing_match(self, value, domain):
        try:
            result = routing.explain_domain(
                self._routing_with_private_fields(value), domain,
                vpn_os.list_vpns())
            return {'ok': True, 'result': result}
        except Exception as exc:
            return {'ok': False, 'msg': str(exc)}

    def validate_routing_dns(self, value):
        """只校验 DNS 草稿，不访问网络、不写配置或重启服务。"""
        try:
            normalized = routing.normalize_config(
                self._routing_with_private_fields(value))
            active = (normalized if normalized['dns_mode'] == 'advanced'
                      else routing.default_config())
            return {
                'ok': True,
                'msg': ('DNS 高级配置校验通过' if normalized['dns_mode'] == 'advanced'
                        else 'DNS 简单模式将使用内置推荐配置'),
                'summary': {
                    'mode': normalized['dns_mode'],
                    'enhanced_mode': active['dns_enhanced_mode'],
                    'nameserver_count': len(active['dns_servers']),
                    'policy_count': len(active.get('nameserver_policy') or []),
                    'fake_ip_range': active['fake_ip_range'],
                },
            }
        except Exception as exc:
            return {'ok': False, 'msg': str(exc)}

    def preview_routing(self, value):
        try:
            result = self.routing.preview(
                self._routing_with_private_fields(value))
            self.log('[routing] 配置预检通过')
            return self._routing_result_for_ui(result)
        except Exception as exc:
            self.log(f'[routing] 配置预检失败: {exc}')
            return {'ok': False, 'msg': str(exc)}

    def apply_routing(self, value):
        with self._routing_lock:
            return self._apply_routing_locked(value, 'manual')

    def set_routing_enabled(self, enabled, traffic_mode=''):
        """首页快开关：只修改启用状态，避免前端旧快照覆盖完整配置。"""
        if not isinstance(enabled, bool):
            return {'ok': False, 'msg': '代理启用状态无效'}
        target = enabled
        mode = str(traffic_mode or '').strip().lower()
        if mode and mode not in routing.TRAFFIC_MODES:
            return {'ok': False, 'msg': '流量模式无效'}
        with self._routing_lock:
            current = routing.normalize_config(
                self._cfg_get().get('routing') or routing.default_config())
            current['enabled'] = target
            if target and mode:
                current['traffic_mode'] = mode
            return self._apply_routing_locked(current, 'proxy_toggle')

    @staticmethod
    def _routing_matches_except_enabled(previous, requested):
        try:
            left = routing.normalize_config(previous)
            right = routing.normalize_config(requested)
        except Exception:
            # 完整应用仍由 RoutingManager 返回原有校验错误；快路径判断不能
            # 提前改变异常边界，也兼容测试或旧调用方提供的最小替身配置。
            return False
        left.pop('enabled', None)
        right.pop('enabled', None)
        return left == right

    def _history_record(self, config, success, source, message):
        try:
            self.routing_history.record(
                config, success, source, message)
        except Exception as exc:
            self.log(
                f'[routing-history] 历史记录写入失败: {type(exc).__name__}')

    def _apply_routing_locked(self, value, source, replacement_cfg=None, expected_interface=None):
        """在持有 _routing_lock 时提交路由和磁盘配置，并维护有效历史。"""
        self._routing_changed()
        previous_cfg = self._cfg_get()
        previous_routing_raw = json.loads(json.dumps(
            previous_cfg.get('routing') or routing.default_config()))
        previous_routing = routing.normalize_config(
            previous_routing_raw)
        requested = self._routing_with_private_fields(value)
        allow_fast_toggle = self._routing_matches_except_enabled(
            previous_routing, requested)
        try:
            try:
                self.routing_history.ensure_baseline(previous_routing)
            except Exception as exc:
                self.log(
                    f'[routing-history] 基线记录失败: {type(exc).__name__}')
            apply_options = {'defer_standby': True, 'allow_fast_toggle': allow_fast_toggle}
            if expected_interface is not None:
                apply_options.update(allow_fast_toggle=False, expected_interface=expected_interface)
            result = self.routing.apply(requested, **apply_options)
            standby_pending = bool(result.pop('standby_pending', False))
            save_exc = None
            with self._lock:
                committed_cfg = json.loads(json.dumps(
                    replacement_cfg if replacement_cfg is not None else self.cfg))
                # 运行时返回值才是通过规范化和预检的最终事实。
                committed_cfg['routing'] = json.loads(
                    json.dumps(result['config']))
                try:
                    cfgmod.save(committed_cfg)
                except Exception as exc:
                    save_exc = exc
                else:
                    self.cfg = committed_cfg
            if save_exc is not None:
                self.log('[routing] 配置提交失败，正在恢复原分流状态')
                try:
                    self.routing.apply(previous_routing_raw)
                except Exception as rollback_exc:
                    self.log(
                        '[routing] 严重错误：配置提交和服务回滚均失败: '
                        f'{type(rollback_exc).__name__}')
                    raise routing.RoutingError(
                        '分流服务已变更，但配置保存失败且自动恢复未完成；'
                        '请保持软件打开并重新应用原配置') from save_exc
                raise routing.RoutingError(
                    '配置保存失败，已自动恢复原分流状态') from save_exc
            routing.subscription_store.prune_cache(
                result['config'].get('proxy_providers') or [])
            self._history_record(
                result['config'], True, source,
                result.get('msg') or '配置已应用')
            if standby_pending:
                self._start_routing_standby_reconcile('代理关闭后')
            self._routing_changed()
            self._poke_ui_state()
            return self._routing_response(result)
        except Exception as exc:
            try:
                failed = routing.normalize_config(requested)
            except Exception:
                failed = previous_routing
            self._history_record(failed, False, source, str(exc))
            self._routing_changed()
            self.log(f'[routing] 应用失败: {exc}')
            return self._routing_response({'ok': False, 'msg': str(exc)})

    def export_config_backup(self, include_subscription_urls=False):
        try:
            result = config_maintenance.create_backup(
                self._cfg_get(), bool(include_subscription_urls))
            self.log('[config] 配置备份已生成（不包含登录或服务凭据）')
            return result
        except Exception as exc:
            self.log(f'[config] 配置备份生成失败: {type(exc).__name__}')
            return {'ok': False, 'msg': str(exc)}

    def preview_config_restore(self, content):
        try:
            prepared = config_maintenance.prepare_restore(
                content, self._cfg_get())
            return {'ok': True, 'summary': prepared['summary']}
        except Exception as exc:
            return {'ok': False, 'msg': str(exc)}

    def restore_config_backup(self, content):
        with self._routing_lock:
            try:
                prepared = config_maintenance.prepare_restore(
                    content, self._cfg_get())
            except Exception as exc:
                return {'ok': False, 'msg': str(exc)}
            result = self._apply_routing_locked(
                prepared['candidate']['routing'], 'backup_restore',
                prepared['candidate'])
            if result.get('ok'):
                result['msg'] = '配置备份已恢复；代理启用状态保持不变'
                result['restore_summary'] = prepared['summary']
                self.log('[config] 配置备份恢复成功')
            return result

    def get_routing_config_history(self):
        try:
            return {'ok': True, 'items': self.routing_history.list()}
        except Exception as exc:
            return {'ok': False, 'msg': str(exc), 'items': []}

    def restore_routing_config_history(self, record_id):
        with self._routing_lock:
            try:
                historical = self.routing_history.load(record_id)
            except Exception as exc:
                return {'ok': False, 'msg': str(exc)}
            return self._apply_routing_locked(historical, 'history_restore')

    def export_routing_diagnostics(self):
        try:
            service = self.routing._service_state(allow_powershell=False)
            try:
                native = self.routing._native_service.diagnostics()
            except Exception as exc:
                native = {'available': False, 'error': type(exc).__name__}
            logs_snapshot = self.routing_activity.snapshot('logs')
            connections_snapshot = self.routing_activity.snapshot('connections')
            core_logs = ((logs_snapshot.get('snapshot') or {}).get('items') or [])
            connection_state = connections_snapshot.get('snapshot') or {}
            activity = {
                'active_connection_count': len(connection_state.get('active') or []),
                'closed_connection_count': len(connection_state.get('closed') or []),
                'upload_total': connection_state.get('upload_total', 0),
                'download_total': connection_state.get('download_total', 0),
            }
            result = config_maintenance.diagnostic_bundle(
                self._cfg_get(), self.get_logs(), core_logs,
                {'state': service, 'native': native}, activity,
                {
                    'system': platform.system(),
                    'release': platform.release(),
                    'machine': platform.machine(),
                    'python_frozen': bool(getattr(sys, 'frozen', False)),
                    'mihomo_version': routing.MIHOMO_VERSION,
                })
            self.log('[diagnostic] 脱敏诊断包已生成')
            return result
        except Exception as exc:
            self.log(f'[diagnostic] 诊断包生成失败: {type(exc).__name__}')
            return {'ok': False, 'msg': str(exc)}

    def test_sms_email(self):
        """测试已保存的 IMAP 配置，只读打开邮箱而不读取正文。"""
        sms_cfg = self._cfg_get().get('sms') or {}
        ok, msg = sms_receiver.test_imap(sms_cfg)
        self.log(f'[sms] 邮箱配置测试 -> {"成功" if ok else "失败"}: {msg}')
        return {'ok': ok, 'msg': msg}

    def get_desktop_settings(self):
        return {
            'startup_enabled': windows_desktop.is_startup_enabled(),
            'close_to_tray': self._cfg_get().get('close_to_tray', False),
        }

    def desktop_quick_snapshot(self):
        """仅从配置和安全节点快照构造托盘菜单，不发起网络请求。"""
        current = routing.normalize_config(
            self._cfg_get().get('routing') or routing.default_config())
        providers = []
        for provider in current['proxy_providers']:
            if not provider.get('enabled'):
                continue
            snapshot = routing.subscription_store.load_node_snapshot(provider)
            rows = list(snapshot.get('nodes') or [])
            selected = str(provider.get('selected_node') or '')
            rows.sort(key=lambda item: (
                str(item.get('name') or '') != selected,
                item.get('alive') is False,
                int(item.get('delay') or 1 << 30),
                str(item.get('name') or '').casefold()))
            nodes = []
            for item in rows[:24]:
                name = str(item.get('name') or '').strip()
                if name:
                    nodes.append({
                        'name': name,
                        'selected': (provider.get('selection_mode') == 'manual'
                                     and name == selected),
                    })
            if nodes:
                providers.append({
                    'id': provider['id'], 'name': provider['name'],
                    'nodes': nodes,
                })
        return {
            'enabled': current['enabled'],
            'traffic_mode': current['traffic_mode'],
            'providers': providers,
        }

    def desktop_toggle_routing(self):
        with self._routing_lock:
            current = routing.normalize_config(
                self._cfg_get().get('routing') or routing.default_config())
            current['enabled'] = not current['enabled']
            return self._apply_routing_locked(current, 'desktop_toggle')

    def desktop_set_traffic_mode(self, mode):
        target = str(mode or '').strip().lower()
        if target not in routing.TRAFFIC_MODES:
            return {'ok': False, 'msg': '流量模式无效'}
        with self._routing_lock:
            current = routing.normalize_config(
                self._cfg_get().get('routing') or routing.default_config())
            if current['traffic_mode'] == target:
                return {'ok': True, 'msg': '当前已经是所选流量模式'}
            current['traffic_mode'] = target
            return self._apply_routing_locked(current, 'desktop_mode')

    def desktop_toggle_traffic_mode(self):
        current = routing.normalize_config(
            self._cfg_get().get('routing') or routing.default_config())
        target = 'global' if current['traffic_mode'] == 'rule' else 'rule'
        return self.desktop_set_traffic_mode(target)

    def desktop_select_node(self, provider_id, node_name):
        """使用现有节点快照切换节点，并通过完整事务立即应用。"""
        with self._routing_lock:
            current = routing.normalize_config(
                self._cfg_get().get('routing') or routing.default_config())
            target_id = str(provider_id or '').strip().lower()
            selected = str(node_name or '').strip()
            provider = next((
                item for item in current['proxy_providers']
                if item['id'] == target_id and item.get('enabled')), None)
            if not provider:
                return {'ok': False, 'msg': '代理订阅不存在或未启用'}
            snapshot = routing.subscription_store.load_node_snapshot(provider)
            names = {
                str(item.get('name') or '')
                for item in snapshot.get('nodes') or []
            }
            if selected not in names:
                return {'ok': False, 'msg': '所选节点不在当前持久化节点列表中'}
            provider['selection_mode'] = 'manual'
            provider['selected_node'] = selected
            return self._apply_routing_locked(current, 'desktop_node')

    def set_startup_enabled(self, enabled):
        try:
            windows_desktop.set_startup_enabled(bool(enabled))
            active = windows_desktop.is_startup_enabled()
            if active != bool(enabled):
                raise RuntimeError('注册表状态校验未通过')
            msg = ('已开启开机自启，登录 Windows 后将静默驻留托盘'
                   if active else '已关闭开机自启')
            self.log(f'[desktop] {msg}')
            return {'ok': True, 'msg': msg}
        except Exception as e:
            self.log(f'[desktop] 开机自启设置失败: {e}')
            return {'ok': False, 'msg': f'开机自启设置失败：{e}'}

    # ---------- 系统 VPN ----------
    def list_vpns(self, force_refresh=False):
        """返回完整 VPN 列表；默认复用 Worker 后台快照。"""
        return self.worker.list_profiles(bool(force_refresh))

    def add_vpn(self, name, server, vtype, l2tp_psk='',
                ipv4_default_gateway=True, ipv6_default_gateway=True):
        ok, msg = vpn_os.add_vpn(name, server, vtype,
                                 l2tp_psk=l2tp_psk,
                                 ipv4_default_gateway=bool(
                                     ipv4_default_gateway),
                                 ipv6_default_gateway=bool(
                                     ipv6_default_gateway))
        default_name = ''
        if ok:
            vpn_connect.prepare_profile(name)
            with self._lock:
                if not self.cfg.get('vpn_name'):
                    self.cfg['vpn_name'] = name
                    cfgmod.save(self.cfg)
                default_name = self.cfg.get('vpn_name', '')
            self.worker.reset_backoff(
                trigger_check=default_name == name,
                clear_confirmation=False)
            self.worker.refresh_connections()
        self.log(f'[vpn] 添加 {name} -> {ok} {msg}')
        if ok and default_name == name:
            msg = (msg + '；' if msg else '') + '已设为默认 VPN'
        return {'ok': ok, 'msg': msg, 'default_name': default_name}

    def update_vpn(self, name, server, vtype, l2tp_psk='',
                   ipv4_default_gateway=True,
                   ipv6_default_gateway=True):
        ok, msg = vpn_os.set_vpn(name, server, vtype,
                                 l2tp_psk=l2tp_psk,
                                 ipv4_default_gateway=bool(
                                     ipv4_default_gateway),
                                 ipv6_default_gateway=bool(
                                     ipv6_default_gateway))
        if ok:
            vpn_connect.prepare_profile(name)
            self.worker.reset_backoff()
            self.worker.refresh_connections()
        self.log(f'[vpn] 修改 {name} -> {ok} {msg}')
        return {'ok': ok, 'msg': msg}

    def remove_vpn(self, name):
        ok, msg = vpn_os.remove_vpn(name)
        if ok:
            with self._lock:
                changed = False
                if self.cfg.get('vpn_name') == name:
                    self.cfg['vpn_name'] = ''
                    changed = True
                creds = self.cfg.get('creds')
                if isinstance(creds, dict) and name in creds:
                    del creds[name]
                    changed = True
                statuses = self.cfg.get('credential_status')
                if isinstance(statuses, dict) and name in statuses:
                    del statuses[name]
                    changed = True
                if changed:
                    cfgmod.save(self.cfg)
            self.worker.reset_backoff()
            self.worker.refresh_connections()
        self.log(f'[vpn] 删除 {name} -> {ok} {msg}')
        return {'ok': ok, 'msg': msg}

    # ---------- 连接 ----------
    def vpn_status(self):
        """返回 worker 维护的全部连接快照，不在 UI 轮询中启动系统进程。

        不在 UI 轮询里现跑 rasdial/PowerShell: 冷启动阶段进程创建与
        杀软扫描会与 GUI 主线程争抢资源, 造成开局假死。
        """
        cfg = self._cfg_get()
        return self.worker.connection_snapshot(cfg.get('vpn_name', ''))

    # ---------- 网络出口 ----------
    def get_ip_info(self, force_refresh=False):
        """立即返回缓存，并在首次、过期或手动刷新时后台采集。"""
        now = time.time()
        thread_to_start = None
        queued = False
        with self._ip_info_lock:
            checked_at = float(self._ip_info.get('checked_at') or 0)
            stale = (now - checked_at >= 300 and
                     now >= float(getattr(self, '_ip_info_retry_after', 0)))
            running = bool(self._ip_info.get('loading'))
            if running and force_refresh:
                queued = not self._ip_info_refresh_queued
                self._ip_info_refresh_queued = True
            should_refresh = not running and (
                bool(force_refresh) or not checked_at or stale)
            if should_refresh:
                self._ip_info['loading'] = True
                thread_to_start = threading.Thread(
                    target=self._refresh_ip_info,
                    name='ip-info-refresh', daemon=True)
                self._ip_info_thread = thread_to_start
            snapshot = json.loads(json.dumps(self._ip_info))
        if thread_to_start:
            self.log('[ip] 网络出口刷新请求已提交')
            thread_to_start.start()
        elif queued:
            self.log('[ip] 网络出口发生新刷新请求，已排队等待当前任务结束')
        return snapshot

    def _refresh_ip_info(self):
        labels = {'local': '内网', 'domestic': '公网出口', 'overseas': '国外出口'}
        while True:
            started_at = time.monotonic()
            self.log(
                '[ip] 后台已领取网络出口刷新任务，开始执行三路并发查询'
                '（网络超时 6 秒，PowerShell 超时 8 秒）')
            completed = 0

            def publish_result(name, result):
                nonlocal completed
                incoming = result or ip_info._empty_entry('查询未返回结果')
                with self._ip_info_lock:
                    previous = self._ip_info.get(name) or {}
                    previous_ok = bool(previous.get('ok') and
                                       not previous.get('stale'))
                    if not incoming.get('ok') and previous.get('ok'):
                        preserved = dict(previous)
                        preserved['stale'] = True
                        preserved['error'] = incoming.get('error', '刷新失败')
                        incoming = preserved
                    self._ip_info[name] = incoming
                    completed += 1
                outcome = '成功' if result and result.get('ok') else '失败'
                elapsed = time.monotonic() - started_at
                if outcome == '失败' or not previous_ok:
                    self.log(
                        f'[ip] {labels.get(name, name)}查询{outcome}，'
                        f'进度 {completed}/3，耗时 {elapsed:.2f} 秒')

            try:
                result = ip_info.collect_ip_info(on_result=publish_result)
                available = sum(
                    bool((result.get(name) or {}).get('ok'))
                    for name in ('local', 'domestic', 'overseas'))
                error = None
            except Exception as exc:
                result = {}
                available = 0
                error = exc
            with self._ip_info_lock:
                self._ip_info['checked_at'] = time.time()
                self._ip_info['updated_at_text'] = (
                    f'{datetime.datetime.now():%H:%M}')
                rerun = self._ip_info_refresh_queued
                self._ip_info_refresh_queued = False
                self._ip_info['loading'] = bool(rerun)
                if available < 3:
                    self._ip_info_failures = min(
                        6, int(getattr(self, '_ip_info_failures', 0)) + 1)
                    delay = min(
                        1800, 300 * (2 ** (self._ip_info_failures - 1)))
                    self._ip_info_retry_after = time.time() + delay
                else:
                    self._ip_info_failures = 0
                    self._ip_info_retry_after = 0.0
            elapsed = time.monotonic() - started_at
            if error:
                self.log(
                    f'[ip] 网络出口信息刷新失败：{type(error).__name__}，'
                    f'耗时 {elapsed:.2f} 秒')
            else:
                self.log(
                    f'[ip] 网络出口信息刷新完成（{available}/3），'
                    f'耗时 {elapsed:.2f} 秒')
            if not error and available < 3:
                self.log(
                    f'[ip] 部分探测失败，下一次刷新将退避 '
                    f'{max(0, self._ip_info_retry_after - time.time()):.0f} 秒')
            if not rerun:
                break
            self.log('[ip] 后台已领取排队的网络出口刷新请求')

    def _connect_named(self, name, after_authorization=False):
        cfg = self._cfg_get()
        creds = cfg.get('creds', {}).get(name)
        source = 'authorization' if after_authorization else 'manual'
        self.worker._set_connection_action(
            'connecting', source, name, f'正在连接 {name}')
        ok, msg = vpn_service.connect(name, creds, log=self.log)
        has_tool_credentials = bool(creds and creds.get('user') and
                                    creds.get('pass'))
        needs_credentials = not ok and not has_tool_credentials
        policy = vpn_service.failure_policy(msg) if not ok else None
        if not ok:
            self.worker.record_connection_failure(msg, name)
            self.worker._set_connection_action('failed', source, name, msg)
        if policy and policy['trigger_renew'] and not needs_credentials:
            # 认证被拒可能是授权到期: 触发续期流程 (自动连接关闭时也能自愈)
            self.worker.request_renew('recovery')
            self.log('[vpn] 认证被拒 (691), 疑似授权到期, 已触发续期流程')
        profiles = self.worker.refresh_connections()
        connected_names = {
            row.get('name') for row in profiles
            if row.get('name') and row.get('status') == 'Connected'
        }
        pending = bool(ok and name not in connected_names)
        if ok and pending:
            self.worker.begin_connection_confirmation(name, source)
            msg = f'连接请求已提交，Windows 正在完成连接 {name}'
        elif ok:
            self.worker.reset_backoff(
                trigger_check=False, clear_confirmation=True)
            self.worker._set_connection_action(
                'connected', source, name, f'已连接 {name}')
        status = self.worker.connection_snapshot(cfg.get('vpn_name', ''))
        conflicts = status.get('route_conflicts') or []
        self.log(f'[vpn] 手动连接 {name} -> {"成功" if ok else "失败"}: {msg}')
        return {'ok': ok, 'msg': msg, 'pending': pending,
                'needs_credentials': needs_credentials,
                'profiles': profiles, 'status': status,
                'route_warning': '; '.join(
                    row.get('message', '') for row in conflicts if row.get('message')),
                **(policy or {})}

    def connect_vpn(self, name, mode='', after_authorization=False):
        """连接指定 VPN；已有其它连接时要求显式选择切换或并行。"""
        name = str(name or '').strip()
        if not name:
            return {'ok': False, 'msg': '未指定要连接的 VPN'}
        dial_snapshot = getattr(self.worker, 'dial_in_progress', None)
        dial_snapshot = dial_snapshot() if callable(dial_snapshot) else None
        if isinstance(dial_snapshot, dict):
            active_name = dial_snapshot.get('name') or 'VPN'
            return {
                'ok': active_name == name, 'pending': active_name == name,
                'busy': True,
                'msg': f'{active_name} 正在后台连接，请等待本次拨号结束后重试',
            }
        with self._vpn_action_lock, self._manual_connection_action():
            profiles = self.worker.refresh_connections()
            profile_names = {row.get('name') for row in profiles}
            if name not in profile_names:
                return {'ok': False, 'msg': f'Windows 中不存在 VPN“{name}”'}
            pending_check = getattr(self.worker, 'connection_pending', None)
            if callable(pending_check) and pending_check(name) is True:
                return {
                    'ok': True, 'pending': True,
                    'msg': f'Windows 正在完成连接 {name}，请稍候',
                    'status': self.worker.connection_snapshot(
                        self._cfg_get().get('vpn_name', '')),
                }
            target_profile = next(
                (row for row in profiles if row.get('name') == name), {})
            if str(target_profile.get('status') or '').casefold() \
                    == 'connecting':
                self.worker.begin_connection_confirmation(name, 'manual')
                return {
                    'ok': True, 'pending': True,
                    'msg': f'Windows 正在完成连接 {name}，请稍候',
                    'status': self.worker.connection_snapshot(
                        self._cfg_get().get('vpn_name', '')),
                }
            connected = [
                row for row in profiles if row.get('status') == 'Connected']
            connected_names = [row.get('name') for row in connected]
            if name in connected_names:
                self.worker.prepare_connection_target(name)
                self.worker._set_connection_action(
                    'connected', 'manual', name, f'已连接 {name}')
                return {
                    'ok': True, 'msg': f'{name} 已处于连接状态',
                    'status': self.worker.connection_snapshot(
                        self._cfg_get().get('vpn_name', '')),
                }
            others = [row for row in connected if row.get('name') != name]
            if others and mode not in ('switch', 'parallel'):
                defaults = [row['name'] for row in others
                            if row.get('default_route')]
                warning = ('当前连接包含默认路由，继续并行连接可能改变网络出口'
                           if defaults else
                           '同时连接多个 VPN 可能产生默认路由或网段冲突')
                return {
                    'ok': False, 'needs_mode_choice': True,
                    'name': name,
                    'active_names': [row['name'] for row in others],
                    'route_warning': warning,
                    'msg': '请选择切换连接或同时连接',
                }
            self.worker.prepare_connection_target(
                name, [row.get('name') for row in others]
                if mode == 'switch' else None)
            if others and mode == 'switch':
                failed = []
                for row in others:
                    ok, out = vpn_connect.disconnect(row['name'])
                    if not ok:
                        failed.append(
                            vpn_service.friendly(out) or row['name'])
                if failed:
                    self.worker.refresh_connections()
                    message = '无法断开现有 VPN：' + '；'.join(failed)
                    self.worker._set_connection_action(
                        'failed', 'manual', name, message)
                    return {
                        'ok': False,
                        'msg': message,
                    }
            return self._connect_named(name, bool(after_authorization))

    def connect_now(self, mode=''):
        """兼容总览入口：连接配置中的默认 VPN。"""
        name = self._cfg_get().get('vpn_name', '')
        if not name:
            return {
                'ok': False,
                'msg': '尚未设置默认 VPN，请在「VPN 配置」中点“设为默认”',
            }
        return self.connect_vpn(name, mode)

    def disconnect_vpn(self, name):
        """仅断开指定 VPN，不影响其它并行连接。"""
        name = str(name or '').strip()
        if not name:
            return {'ok': False, 'msg': '未指定要断开的 VPN'}
        with self._vpn_action_lock, self._manual_connection_action():
            profiles = self.worker.refresh_connections()
            active = {row.get('name') for row in profiles
                      if row.get('status') == 'Connected'}
            if name not in active:
                self.worker.note_manual_disconnect([name])
                return {
                    'ok': True, 'msg': f'{name} 已处于断开状态',
                    'status': self.worker.connection_snapshot(
                        self._cfg_get().get('vpn_name', '')),
                }
            ok, out = vpn_connect.disconnect(name)
            msg = vpn_service.friendly(out) or ('已断开' if ok else '断开失败')
            self.worker.refresh_connections()
            issue = self.worker.state.get('connection_error') or {}
            if ok and (not issue.get('name') or issue.get('name') == name):
                self.worker.state['connection_error'] = None
            status = self.worker.connection_snapshot(
                self._cfg_get().get('vpn_name', ''))
            self.log(f'[vpn] 断开 {name} -> {ok}')
            if ok:
                self.worker.note_manual_disconnect([name])
            return {'ok': ok, 'msg': msg, 'status': status}

    def disconnect_now(self):
        """兼容总览入口：仅断开默认 VPN。"""
        name = self._cfg_get().get('vpn_name', '')
        if not name:
            return {'ok': False, 'msg': '尚未设置默认 VPN'}
        return self.disconnect_vpn(name)

    def disconnect_all(self):
        """断开当前全部 VPN，返回逐条失败摘要。"""
        with self._vpn_action_lock, self._manual_connection_action():
            profiles = self.worker.refresh_connections()
            names = [row.get('name') for row in profiles
                     if row.get('status') == 'Connected' and row.get('name')]
            failed = []
            for name in names:
                ok, out = vpn_connect.disconnect(name)
                if not ok:
                    failed.append({
                        'name': name,
                        'message': vpn_service.friendly(out) or '断开失败',
                    })
            succeeded = [name for name in names
                         if name not in {row['name'] for row in failed}]
            if succeeded or not names:
                self.worker.note_manual_disconnect(succeeded or [
                    self._cfg_get().get('vpn_name', '')])
            self.worker.refresh_connections()
            if not failed:
                self.worker.state['connection_error'] = None
            status = self.worker.connection_snapshot(
                self._cfg_get().get('vpn_name', ''))
            self.log(f'[vpn] 断开全部 -> {len(names) - len(failed)}/{len(names)}')
            return {
                'ok': not failed,
                'msg': ('已断开全部 VPN' if names and not failed else
                        '当前没有已连接的 VPN' if not names else
                        '部分 VPN 断开失败：' + '；'.join(
                            f"{row['name']}：{row['message']}" for row in failed)),
                'failed': failed,
                'status': status,
            }

    def repair_vpn(self):
        """修复 RasMan/IKE/IPsec 服务 (连接卡死兜底, 需管理员权限)"""
        from core import vpn_repair

        with self._repair_lock:
            if self._repairing:
                return {'ok': False, 'msg': 'VPN 服务修复正在执行，请勿重复启动'}
            self._repairing = True
            self._repair_result = None
            self._repair_run_id += 1
            run_id = self._repair_run_id

        def _run():
            ok, msg = False, '修复未执行'
            try:
                ok, msg = vpn_repair.repair(log=self.log)
            except Exception as e:
                ok, msg = False, str(e)
            finally:
                self.log(f'[vpn] 服务修复 -> {"成功" if ok else "失败"}: {msg}')
                if ok:
                    try:
                        self.worker.refresh_connections()
                        self.worker.reset_backoff()
                    except Exception as e:
                        ok = False
                        msg = f'服务已修复，但状态刷新失败：{e}'
                        self.log(f'[vpn] {msg}')
                with self._repair_lock:
                    self._repair_result = {
                        'id': run_id, 'ok': ok, 'msg': msg,
                        'finished_at': datetime.datetime.now().strftime('%H:%M:%S'),
                    }
                    self._repairing = False

        threading.Thread(target=_run, daemon=True).start()
        self.log('[vpn] 已启动 VPN 服务修复 (将申请管理员权限)')
        return {'ok': True, 'msg': '修复已在后台启动, 请允许 UAC 授权 '
                                    '(进度见运行日志)'}

    def save_cred(self, name, user, pwd):
        """立即保存到工具和 Windows；保存动作不拨号、不触发网页授权。"""
        if not user:
            return {'ok': False, 'msg': '请输入用户名'}
        if not pwd:
            with self._lock:
                pwd = (self.cfg.get('creds', {}).get(name) or {}).get('pass', '')
            if not pwd:
                self.log(f'[vpn] {name} 未输入密码, 无已存凭据可保留')
                return {'ok': False, 'msg': '请输入密码'}
        windows_saved, windows_error = vpn_service.sync_credentials(
            name, user, pwd)
        if windows_saved:
            vpn_connect.prepare_profile(name)
        with self._lock:
            self.cfg.setdefault('creds', {})[name] = {
                'user': user, 'pass': pwd}
            statuses = self.cfg.setdefault('credential_status', {})
            if windows_saved:
                statuses.pop(name, None)
            else:
                statuses[name] = 'windows_sync_failed'
            cfgmod.save(self.cfg)
        self.worker.reset_backoff(
            trigger_check=True, clear_confirmation=False)
        self.worker.refresh_connections()
        if windows_saved:
            msg = '账号密码已保存到软件并同步到 Windows，可直接点击连接'
            self.log(f'[vpn] {name} 账号密码已同步到 Windows')
        else:
            msg = ('账号密码已保存到软件，但 Windows 同步失败 '
                   f'(错误 {windows_error})，请重试保存')
            self.log(f'[vpn] {name} Windows 凭据写入失败: {windows_error}')
        return {
            'ok': windows_saved, 'msg': msg, 'interactive': False,
            'authorization_started': False,
            'tool_saved': True, 'windows_saved': windows_saved,
        }

    def get_cred(self, name):
        """回显凭据: 用户名/密码来自工具保存的副本; 无副本时用户名取 Windows"""
        with self._lock:
            cr = dict(self.cfg.get('creds', {}).get(name) or {})
            validation_status = self.cfg.get('credential_status', {}).get(
                name, '')
        u, p = ras_cred.get(name)
        masked = bool(p) and set(p) == {'*'}
        return {'user': cr.get('user') or u, 'pass': cr.get('pass', ''),
                'has_saved': bool(p), 'masked': masked, 'stored': bool(cr),
                'validation_status': validation_status}

    # ---------- 内置浏览器 (原生窗口) ----------
    def open_browser(self):
        self.worker.browser_requested.set()
        self.log('[browser] 已提交手动打开内置浏览器请求，等待 worker 领取')
        return True

    def get_browser(self):
        up = bool(self.worker.browser and self.worker.browser.alive)
        url = ''
        if up:
            try:
                url = self.worker.page.url
            except Exception:
                pass
        with self.worker._net_lock:
            net_count = len(self.worker.netlog)
        return {'up': up, 'url': url,
                'progress': dict(self.worker.state['browser_progress']),
                'net_count': net_count}

    def browser_set_visible(self, visible):
        self.worker.browser_visible = bool(visible)
        browser = self.worker.browser
        if browser and browser.alive:
            browser.set_visible(self.worker.browser_visible)
            if visible:
                self.worker._push_browser_overlay()
        return True

    def browser_reload(self):
        if self.worker.page and self.worker.browser \
                and self.worker.browser.alive:
            ok = self.worker.page.reload()
            return {'ok': ok,
                    'msg': '浏览器已刷新' if ok else '浏览器刷新超时，请重试'}
        return {'ok': False, 'msg': '浏览器尚未启动'}

    def get_netlog(self):
        with self.worker._net_lock:
            return list(self.worker.netlog)

    # ---------- 续期 ----------
    def renew_now(self):
        if not self.worker.request_renew('manual'):
            return {'ok': False, 'msg': '授权处理任务已在执行，请勿重复发起'}
        self.log('[renew] 已请求授权处理')
        return {'ok': True, 'msg': '已开始处理授权，可在总览页查看进度或中断'}

    def cancel_renew(self):
        if not self.worker.cancel_renew():
            return {'ok': False, 'msg': '当前没有可中断的授权处理任务'}
        # 若流程正等待工具内人工验证码，立即解除该等待；短信弹窗也同步隐藏。
        if self._manual is not None:
            self.dismiss_manual_captcha()
        self.sms_show(False)
        self.log('[renew] 已请求中断当前授权处理')
        return {'ok': True, 'msg': '正在中断授权处理，请稍候'}

    def get_state(self):
        st = dict(self.worker.state)
        st['renew_queued'] = self.worker.renew_requested.is_set()
        with self._repair_lock:
            st['repairing'] = self._repairing
            st['repair_result'] = (dict(self._repair_result)
                                   if self._repair_result else None)
        st['last_renew'] = datetime.datetime.fromtimestamp(
            st['last_renew']).strftime('%m-%d %H:%M') if st['last_renew'] \
            else '—'
        authorization = dict(st.get('authorization') or {})
        expires_at = float(authorization.get('expires_at') or 0)
        next_renew_at = float(authorization.get('next_renew_at') or 0)
        now = datetime.datetime.now().timestamp()
        authorization['expires_at_text'] = (
            datetime.datetime.fromtimestamp(expires_at).strftime('%m-%d %H:%M')
            if expires_at else '待同步')
        authorization['next_renew_at_text'] = (
            datetime.datetime.fromtimestamp(next_renew_at).strftime('%m-%d %H:%M')
            if next_renew_at else '待同步')
        authorization['remaining_seconds'] = max(
            0, int(expires_at - now)) if expires_at else None
        st['authorization'] = authorization
        return st

    # ---------- 人工验证码 (工具内点击) ----------
    def request_manual_captcha(self, sprite_bytes, timeout=300, tip_bytes=None):
        """worker 线程调用: 挂起等待 UI 提交点击坐标"""
        import base64
        self._manual_id += 1
        self._manual = {
            'id': self._manual_id,
            'img': 'data:image/jpeg;base64,'
                   + base64.b64encode(sprite_bytes).decode(),
            'tip': ('data:image/png;base64,'
                    + base64.b64encode(tip_bytes).decode())
                   if tip_bytes else None}
        self._manual_clicks = None
        self._manual_event.clear()
        self.log('[captcha] 等待工具内人工点击')
        if self._desktop:
            self._desktop.request_attention(
                '需要人工完成图形验证',
                '未配置可用模型或自动识别未通过，请在主窗口中按顺序点击验证码。')
        ok = self._manual_event.wait(timeout)
        clicks = self._manual_clicks
        self._manual = None
        return clicks if ok else None

    def get_manual_captcha(self):
        return self._manual

    def submit_manual_clicks(self, clicks):
        self._manual_clicks = clicks
        self._manual_event.set()
        self.log(f'[captcha] 收到人工点击: {clicks}')
        return True

    def dismiss_manual_captcha(self):
        """UI 主动关闭人工验证码弹窗: 解除后端挂起 (worker 以 None 按失败处理)"""
        self._manual = None
        self._manual_clicks = None
        self._manual_event.set()
        self.log('[captcha] 用户关闭人工验证码弹窗, 放弃本次点击')
        return True

    # ---------- 手动短信验证码 (与自动捕获并行, 谁先取到用哪个) ----------
    def sms_show(self, show):
        """worker 调用: 控制手动短信弹窗显隐"""
        if show:
            self._sms_ui_id += 1
            self._sms_ui = True
        else:
            self._sms_ui = False

    def get_sms_ui(self):
        """UI 轮询: 返回 {'id': n} 表示应展示弹窗, None 表示应关闭"""
        return {'id': self._sms_ui_id} if self._sms_ui else None

    def dismiss_sms_ui(self):
        """UI 主动关闭短信弹窗: 后台自动捕获与等待流程不受影响"""
        self._sms_ui = False
        return True

    def submit_sms_code(self, code):
        code = (code or '').strip()
        if not code:
            return False
        self._manual_sms = code
        self._manual_sms_ev.set()
        self._sms_ui = False
        self.log(f'[sms] 收到手动输入的验证码: {code}')
        return True

    def poll_manual_sms(self):
        """worker/web_flow 轮询: 取 UI 手动提交的验证码 (无则 None), 取后即清"""
        if self._manual_sms_ev.is_set():
            self._manual_sms_ev.clear()
            v = self._manual_sms
            self._manual_sms = None
            return v
        return None

    # ---------- 模型 ----------
    def test_vlm(self):
        cfg = self._cfg_get()
        v = cfg.get('vlm') or {}
        if not (v.get('base') and v.get('key') and v.get('model')):
            return {'ok': False, 'msg': '请先填写 Base URL / API Key / 模型名'}
        payload = {'model': v['model'],
                   'messages': [{'role': 'user',
                                 'content': 'Reply with the word: pong'}],
                   'max_tokens': 8, 'temperature': 0}
        req = urllib.request.Request(
            v['base'].rstrip('/') + '/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + v['key'],
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            reply = data['choices'][0]['message']['content']
            self.log(f'[vlm] 测试成功: {reply!r}')
            return {'ok': True, 'msg': f'模型回复: {reply}'}
        except urllib.error.HTTPError as e:
            msg = f'HTTP {e.code}: {e.read()[:200].decode("utf-8", "replace")}'
            self.log(f'[vlm] 测试失败 {msg}')
            return {'ok': False, 'msg': msg}
        except Exception as e:
            self.log(f'[vlm] 测试失败: {e}')
            return {'ok': False, 'msg': str(e)}
