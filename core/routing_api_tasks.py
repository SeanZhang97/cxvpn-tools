# -*- coding: utf-8 -*-
"""API 路由任务提交、版本校验与短事务提交边界。"""
import copy
import time
from contextlib import contextmanager
import threading

from core import routing
from core.routing_tasks import commit_scope, operation_scope


class RoutingTaskApi:
    def _routing_snapshot(self):
        with self._lock:
            return (copy.deepcopy(self.cfg.get('routing') or {}),
                    getattr(self, '_routing_revision', 0))

    def _routing_changed(self):
        with self._lock:
            self._routing_revision = getattr(self, '_routing_revision', 0) + 1
        jobs = getattr(self, 'routing_test_jobs', None)
        if jobs is not None:
            jobs.cancel_all('代理配置已变化，旧测速任务已取消')

    def _routing_response(self, result, revision=None):
        value = dict(result)
        value['routing_revision'] = (getattr(self, '_routing_revision', 0)
                                     if revision is None else revision)
        return self._routing_result_for_ui(value)

    @contextmanager
    def _routing_commit(self, revision, cancel_event=None):
        if not self._routing_lock.acquire(timeout=2):
            raise routing.RoutingError('代理配置正在变更，本次后台结果未写入，请稍后刷新')
        try:
            if (revision != getattr(self, '_routing_revision', 0)
                    or (cancel_event is not None and cancel_event.is_set())):
                raise routing.RoutingError('代理配置已变化，本次旧任务结果未写入')
            yield
        finally:
            self._routing_lock.release()

    def _routing_provider_task(self, provider_id, operation, cancel_event=None):
        key = str(provider_id or 'all').strip().lower()
        if key != 'all' and not routing.PROVIDER_ID_RE.fullmatch(key):
            raise routing.RoutingError('订阅标识无效')
        with self._lock:
            if not hasattr(self, '_provider_task_locks'):
                self._provider_task_locks = {}
            lock = self._provider_task_locks.setdefault(key, threading.Lock())
            if not lock.acquire(blocking=False):
                raise routing.RoutingError('该订阅已有获取或测速任务，请等待完成或停止测速')
        started = time.monotonic()
        self.log(f'[routing-task] 请求已提交并由后台领取: provider={key}')
        try:
            config, revision = self._routing_snapshot()
            with operation_scope(lambda: self._routing_commit(revision, cancel_event)):
                self.log(f'[routing-task] 开始执行: provider={key}，配置版本={revision}')
                result = operation(config)
                with commit_scope():
                    result = self._routing_response(result, revision)
            self.log(f'[routing-task] 执行成功: provider={key}，耗时={time.monotonic()-started:.2f}秒')
            return result
        except Exception as exc:
            self.log(f'[routing-task] 执行失败或取消: provider={key}，异常={type(exc).__name__}，耗时={time.monotonic()-started:.2f}秒')
            raise
        finally:
            with self._lock:
                lock.release()
                if self._provider_task_locks.get(key) is lock:
                    self._provider_task_locks.pop(key, None)
