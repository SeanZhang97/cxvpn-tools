# -*- coding: utf-8 -*-
"""VPN 连接状态前置监控：状态翻转后复用路由事务重载，使未连接 VPN 的规则默认直连。"""
import copy
import threading
import time

from core import routing, vpn_os
from core.routing_environment import bounded_calls


class RoutingVpnWorker:
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
                                        name='routing-vpn-state')
        self._thread.start()

    def stop(self, timeout=3):
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0, min(float(timeout), 3)))
        return not thread or not thread.is_alive()

    def _run(self):
        # 不在 worker/UI 主循环中枚举 VPN；退出可打断等待。
        while not self._stop_event.wait(15):
            try:
                self.check()
            except Exception as exc:
                self._retry_at = time.monotonic() + 60
                self._log(f'[routing-vpn] VPN 状态复核失败：{type(exc).__name__}，60秒后再检查')

    def check(self):
        now = time.monotonic()
        if self._stop_event.is_set() or now < self._retry_at:
            return
        source = copy.deepcopy(self._config_getter().get('routing') or {})
        config = routing.normalize_config(source)
        target_vpns = routing._target_vpns(config)
        if not config['enabled'] or not target_vpns:
            self._candidate = None
            return
        state = self._manager._native_service.status()
        if not isinstance(state, dict):
            self._candidate = None
            return
        signature = self._manager._config_signature(config)
        if (not state.get('runtime_running') or state.get('runtime_mode') != 'active'
                or state.get('pending_transaction')
                or not self._manager._native_service._compatible(state)
                or not (state.get('applied_config_signature') == signature
                        or self._manager.runtime_matches(config, state))):
            self._candidate = None
            return
        result, errors = bounded_calls({'vpns': vpn_os.list_vpns}, 30)
        if errors:
            raise TimeoutError('VPN 连接状态扫描超过30秒或失败')
        current = routing._vpn_connection_signature(result['vpns'], target_vpns)
        # 基线是 apply 记录的全量签名（含非规则引用的 VPN）；锁定内复核必须
        # 与同一原始视图比较，不能与限定后的名单比较。
        raw_baseline = self._manager._runtime_vpns
        if raw_baseline is None:
            # 本进程未生成过运行态，其 VPN 状态未知；对账应用一次重建
            # 基线，避免沿用“生成时已连、现在已断”的陈旧规则。
            changes = '基线缺失'
        else:
            baseline = {name: raw_baseline.get(name, False)
                        for name in target_vpns}
            if current == baseline:
                self._candidate = None
                return
            changes = '、'.join(
                f'{name}{"已连接" if current[name] else "已断开"}'
                for name in target_vpns if current[name] != baseline[name])
        candidate = (signature, state.get('config_sha256'),
                     tuple(sorted(current.items())))
        if candidate != self._candidate:
            self._candidate = candidate
            self._observed_at = time.monotonic()
            self._log(f'[routing-vpn] VPN 连接状态{changes}：等待稳定，不修改现有运行态')
            return
        if time.monotonic() - self._observed_at < 15:
            return
        self._retry_at = time.monotonic() + 60
        self._log('[routing-vpn] VPN 状态自动切换请求已提交：锁等待上限2秒')
        if not self._routing_lock.acquire(timeout=2):
            self._log('[routing-vpn] 路由正忙，VPN 状态自动切换未领取，延后检查')
            return
        try:
            latest = self._manager._native_service.status()
            if (not isinstance(latest, dict)
                    or self._stop_event.is_set()
                    or source != (self._config_getter().get('routing') or {})
                    or latest.get('config_sha256') != state.get('config_sha256')
                    or latest.get('mihomo_pid') != state.get('mihomo_pid')
                    or latest.get('runtime_mode') != 'active'
                    or latest.get('pending_transaction')
                    or self._manager._runtime_vpns != raw_baseline):
                self._candidate = None
                self._log('[routing-vpn] VPN 状态自动切换已取消：配置、服务、基线或退出状态发生变化')
                return
            self._log('[routing-vpn] 后台已领取，开始应用 VPN 状态切换')
            if self._stop_event.is_set():
                self._candidate = None
                self._log('[routing-vpn] VPN 状态自动切换已取消：应用前收到退出请求')
                return
            started = time.monotonic()
            result = self._apply(source, current)
            self._log(f'[routing-vpn] VPN 状态自动切换{"成功" if result.get("ok") else "失败"}，'
                      f'耗时={time.monotonic()-started:.1f}秒；失败遵循原事务回滚结果')
            self._candidate = None
        finally:
            self._routing_lock.release()
