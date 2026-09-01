# -*- coding: utf-8 -*-
"""仅在管理器前台运行时执行用户明确开启的订阅定时更新。"""
from __future__ import annotations

import threading
import time

from core import subscription_store


class RoutingUpdateWorker:
    def __init__(self, config_getter, manager, routing_lock, logger=None):
        self._config_getter = config_getter
        self._manager = manager
        self._routing_lock = routing_lock
        self._logger = logger or (lambda _message: None)
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='routing-subscription-update')
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        if self._stop_event.wait(5):
            return
        while not self._stop_event.is_set():
            try:
                self._update_one_due_provider()
            except Exception as exc:
                self._logger(
                    f'[routing] 订阅自动更新检查失败: {type(exc).__name__}')
            self._stop_event.wait(60)

    def _update_one_due_provider(self):
        from core.routing import normalize_config

        config = normalize_config(
            (self._config_getter().get('routing') or {}))
        # 启用服务后由 Mihomo 按 provider interval 更新，避免重复请求。
        if config['enabled']:
            return
        now = int(time.time())
        provider = next((item for item in config['proxy_providers']
                         if item['enabled'] and item.get('auto_update') and
                         now - subscription_store.cache_status(item).get(
                             'updated_at', 0) >= item['interval']), None)
        if not provider or not self._routing_lock.acquire(blocking=False):
            return
        try:
            latest = normalize_config(
                (self._config_getter().get('routing') or {}))
            current = next((item for item in latest['proxy_providers']
                            if item['id'] == provider['id'] and
                            item['enabled'] and item.get('auto_update')), None)
            if not current or latest['enabled']:
                return
            status = self._manager.status(latest, quick=True)
            if status.get('core_running') is True:
                self._logger(
                    f'[routing] 订阅“{current["name"]}”自动更新已提交到待机核心')
                result = self._manager.refresh_proxy_provider(
                    latest, current['id'])
            else:
                self._logger(
                    f'[routing] 订阅“{current["name"]}”自动更新改由临时核心执行')
                result = self._manager.preview_proxy_provider(current, {
                    'physical_interface': latest['physical_interface'],
                    'dns_servers': latest['dns_servers'],
                })
            if result.get('refreshed') is False:
                self._logger(
                    f'[routing] 订阅“{current["name"]}”自动更新未完成，'
                    '保留上次成功缓存并等待下次重试')
            else:
                self._logger(f'[routing] 订阅“{current["name"]}”自动更新完成')
        except Exception as exc:
            self._logger(
                f'[routing] 订阅“{provider["name"]}”自动更新失败: '
                f'{type(exc).__name__}')
        finally:
            self._routing_lock.release()
