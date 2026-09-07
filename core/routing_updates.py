# -*- coding: utf-8 -*-
"""GUI 订阅更新：按订阅轮转、失败退避，网络请求不占用路由提交锁。"""
from __future__ import annotations

import copy
import threading
import time
from contextlib import contextmanager

from core import subscription_store
from core.routing_tasks import operation_scope, commit_scope


class RoutingUpdateWorker:
    def __init__(self, config_getter, manager, routing_lock, logger=None, task_runner=None):
        self._config_getter = config_getter
        self._manager = manager
        self._routing_lock = routing_lock
        self._logger = logger or (lambda _message: None)
        self._task_runner = task_runner
        self._stop_event = threading.Event()
        self._thread = None
        self._cursor = 0
        self._attempts = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='routing-subscription-update')
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        if self._stop_event.wait(5):
            return
        while not self._stop_event.is_set():
            attempted = False
            try:
                attempted = self._update_one_due_provider()
            except Exception as exc:
                self._logger(f'[routing] 订阅自动更新检查失败: {type(exc).__name__}')
            self._stop_event.wait(2 if attempted else 60)

    def _execute(self, config, provider_id):
        from core.routing import normalize_config, RoutingError
        current_config = normalize_config(config)
        provider = next((item for item in current_config['proxy_providers']
                         if item['id'] == provider_id and item['enabled'] and item['auto_update']), None)
        if self._stop_event.is_set() or current_config['enabled'] or provider is None:
            raise RoutingError('自动更新已取消：运行配置已变化')
        status = self._manager.status(current_config, quick=True)
        if status.get('core_running') is True:
            self._logger(f'[routing] 自动更新由待机核心执行: provider={provider_id}，超时=15秒')
            return self._manager.refresh_proxy_provider(current_config, provider_id)
        return self._manager.preview_proxy_provider(provider, {
            'physical_interface': current_config['physical_interface'],
            'dns_servers': current_config['dns_servers'],
        })

    def _update_one_due_provider(self):
        from core.routing import normalize_config, RoutingError
        source = copy.deepcopy(self._config_getter().get('routing') or {})
        config = normalize_config(source)
        if config['enabled'] or self._stop_event.is_set():
            return False
        providers = config['proxy_providers']
        keys = {(item['id'], subscription_store.url_fingerprint(item)) for item in providers}
        self._attempts = {key: value for key, value in self._attempts.items() if key in keys}
        now = time.time()
        provider = None
        for offset in range(len(providers)):
            index = (self._cursor + offset) % len(providers)
            item = providers[index]
            key = (item['id'], subscription_store.url_fingerprint(item))
            attempt = self._attempts.get(key, {})
            if (item['enabled'] and item['auto_update'] and now >= attempt.get('next', 0)
                    and now - subscription_store.cache_status(item).get('updated_at', 0) >= item['interval']):
                provider = item
                self._cursor = (index + 1) % len(providers)
                break
        if provider is None:
            return False
        key = (provider['id'], subscription_store.url_fingerprint(provider))
        self._logger(f'[routing] 订阅自动更新已提交并开始执行: provider={provider["id"]}')

        @contextmanager
        def guard():
            if not self._routing_lock.acquire(timeout=2):
                raise RoutingError('路由配置正在变更，本次自动更新结果未写入')
            try:
                if self._stop_event.is_set() or source != (self._config_getter().get('routing') or {}):
                    raise RoutingError('配置已变化，本次自动更新结果未写入')
                yield
            finally:
                self._routing_lock.release()

        try:
            if self._task_runner:
                result = self._task_runner(provider['id'],
                    lambda latest: self._execute(latest, provider['id']), cancel_event=self._stop_event)
            else:
                with operation_scope(guard):
                    result = self._execute(config, provider['id'])
                    with commit_scope():
                        pass
            if result.get('refreshed') is False:
                raise RoutingError('远端更新未完成，保留上次成功缓存')
            self._attempts[key] = {'next': time.time() + provider['interval'], 'failures': 0}
            self._logger(f'[routing] 订阅自动更新成功: provider={provider["id"]}')
        except Exception as exc:
            failures = min(7, self._attempts.get(key, {}).get('failures', 0) + 1)
            delay = min(3600, 60 * 2 ** (failures - 1))
            self._attempts[key] = {'next': time.time() + delay, 'failures': failures}
            self._logger(f'[routing] 订阅自动更新未完成: provider={provider["id"]}，异常={type(exc).__name__}，{delay}秒后可重试')
        return True
