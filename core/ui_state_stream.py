# -*- coding: utf-8 -*-
"""UI 版本化状态流：在后端聚合快照，跨 pywebview 桥仅传递变化。"""
import copy
import json
import threading
import time


class UiStateStream:
    """以版本号发布不可变快照，并为前端提供可恢复的阻塞订阅。"""

    def __init__(self, snapshot_factory, log, interval=0.5):
        self._snapshot_factory = snapshot_factory
        self._log = log
        self._interval = max(0.1, float(interval))
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._poke = threading.Event()
        self._thread = None
        self._snapshot = None
        self._fingerprint = ''
        self._version = 0
        self._consumer_seen = False
        self._failure_type = ''

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name='ui-state-stream', daemon=True)
        self._log('[ui-stream] 版本化状态任务已提交，等待后台领取')
        self._thread.start()

    def stop(self):
        self._log('[ui-stream] 已提交停止请求，等待状态任务退出')
        self._stop.set()
        self._poke.set()
        with self._condition:
            self._condition.notify_all()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def poke(self):
        """业务状态变化时唤醒采样；高频事件会由 Event 自动合并。"""
        self._poke.set()

    def current(self):
        with self._condition:
            return {
                'ok': True,
                'version': self._version,
                'changed': self._snapshot is not None,
                'snapshot': copy.deepcopy(self._snapshot),
            }

    def wait(self, after_version=0, timeout=25):
        try:
            cursor = max(0, int(after_version or 0))
        except (TypeError, ValueError):
            cursor = 0
        try:
            wait_seconds = min(30, max(1, float(timeout or 25)))
        except (TypeError, ValueError):
            wait_seconds = 25
        with self._condition:
            if not self._consumer_seen:
                self._consumer_seen = True
                self._log('[ui-stream] UI 已领取版本化状态订阅，开始等待事件')
            deadline = time.monotonic() + wait_seconds
            while (not self._stop.is_set() and
                   (self._snapshot is None or self._version <= cursor)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            changed = self._snapshot is not None and self._version > cursor
            return {
                'ok': not self._stop.is_set(),
                'version': self._version,
                'changed': changed,
                'resync': cursor > self._version,
                'snapshot': copy.deepcopy(self._snapshot) if changed else None,
            }

    def _run(self):
        self._log('[ui-stream] 后台已领取版本化状态任务，开始生成快照')
        try:
            while not self._stop.is_set():
                started_at = time.monotonic()
                try:
                    snapshot = self._snapshot_factory()
                    fingerprint = json.dumps(
                        snapshot, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':'), default=str)
                    with self._condition:
                        if fingerprint != self._fingerprint:
                            self._snapshot = copy.deepcopy(snapshot)
                            self._fingerprint = fingerprint
                            self._version += 1
                            self._condition.notify_all()
                    if self._failure_type:
                        self._log('[ui-stream] 状态快照生成已恢复')
                        self._failure_type = ''
                except Exception as exc:
                    failure_type = type(exc).__name__
                    if failure_type != self._failure_type:
                        self._log(
                            f'[ui-stream] 状态快照生成失败: {failure_type}')
                        self._failure_type = failure_type
                remaining = max(0, self._interval - (
                    time.monotonic() - started_at))
                self._poke.wait(remaining)
                self._poke.clear()
        finally:
            with self._condition:
                self._condition.notify_all()
            self._log('[ui-stream] 版本化状态任务已停止')
