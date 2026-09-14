# -*- coding: utf-8 -*-
"""自动物理出口复核：稳定变化后复用已有路由事务，不改写用户网卡偏好。"""
import copy
import threading
import time

from core import routing
from core.routing_environment import bounded_calls


class RoutingNetworkWorker:
    def __init__(self, config_getter, manager, routing_lock, apply, logger):
        self._config_getter = config_getter
        self._manager = manager
        self._routing_lock = routing_lock
        self._apply = apply
        self._log = logger
        self._stop_event = threading.Event()
        self._thread = None
        self._candidate = None
        self._observed_at = 0
        self._retry_at = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='routing-network-change')
        self._thread.start()

    def stop(self, timeout=3):
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0, min(float(timeout), 3)))
        return not thread or not thread.is_alive()

    def _run(self):
        # 不在 worker/UI 主循环中枚举网卡；退出可打断等待。
        while not self._stop_event.wait(15):
            try:
                self.check()
            except Exception as exc:
                self._retry_at = time.monotonic() + 60
                self._log(f'[routing-network] 出口复核失败：{type(exc).__name__}，60秒后再检查')

    def check(self):
        now = time.monotonic()
        if self._stop_event.is_set() or now < self._retry_at:
            return
        source = copy.deepcopy(self._config_getter().get('routing') or {})
        config = routing.normalize_config(source)
        if not config['enabled'] or config['physical_interface']:
            self._candidate = None
            return
        state = self._manager._native_service.status()
        signature = self._manager._config_signature(config)
        if (not state.get('runtime_running') or state.get('runtime_mode') != 'active'
                or state.get('pending_transaction')
                or not self._manager._native_service._compatible(state)
                or not (state.get('applied_config_signature') == signature
                        or self._manager.runtime_matches(config, state))):
            self._candidate = None
            return
        result, errors = bounded_calls({'interfaces': routing.list_physical_interfaces}, 30)
        if errors:
            raise TimeoutError('物理网卡扫描超过30秒或失败')
        interfaces = result['interfaces']
        target = interfaces[0]['name'] if interfaces else ''
        if target == state.get('applied_physical_interface'):
            self._candidate = None
            return
        candidate = (signature, state.get('config_sha256'), target)
        if candidate != self._candidate:
            self._candidate = candidate
            self._observed_at = time.monotonic()
            self._log(f'[routing-network] 物理出口变化：目标={target or "无可用物理接口"}；等待稳定，不修改现有运行态')
            return
        if not target or time.monotonic() - self._observed_at < 15:
            return
        self._retry_at = time.monotonic() + 60
        self._log(f'[routing-network] 自动出口切换请求已提交：目标={target}，锁等待上限2秒')
        if not self._routing_lock.acquire(timeout=2):
            self._log('[routing-network] 路由正忙，自动出口切换未领取，延后检查')
            return
        try:
            latest = self._manager._native_service.status()
            if (self._stop_event.is_set()
                    or source != (self._config_getter().get('routing') or {})
                    or latest.get('config_sha256') != state.get('config_sha256')
                    or latest.get('mihomo_pid') != state.get('mihomo_pid')
                    or latest.get('runtime_mode') != 'active'
                    or latest.get('pending_transaction')):
                self._candidate = None
                self._log('[routing-network] 自动出口切换已取消：配置、服务或退出状态发生变化')
                return
            self._log(f'[routing-network] 后台已领取，开始应用自动出口：{target}')
            if self._stop_event.is_set():
                self._candidate = None
                self._log('[routing-network] 自动出口切换已取消：应用前收到退出请求')
                return
            started = time.monotonic()
            result = self._apply(source, target)
            self._log(f'[routing-network] 自动出口切换{"成功" if result.get("ok") else "失败"}，'
                      f'耗时={time.monotonic()-started:.1f}秒；失败遵循原事务回滚结果')
            self._candidate = None
        finally:
            self._routing_lock.release()
