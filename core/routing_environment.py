# -*- coding: utf-8 -*-
"""环境扫描合并、缓存及外部调用总期限。"""
import copy
import queue
import threading
import time


_slots = threading.BoundedSemaphore(16)


def bounded_calls(calls, timeout, first_success=False):
    """总期限独立于系统 DNS；最多 16 个在途调用，迟到结果不参与本次决策。"""
    results, errors = {}, {}
    completed = queue.Queue()
    deadline = time.monotonic() + max(.01, timeout)
    pending = set()

    def execute(key, function):
        try:
            completed.put((key, function(), None))
        except Exception as exc:
            completed.put((key, None, exc))
        finally:
            _slots.release()

    for key, function in calls.items():
        if not _slots.acquire(blocking=False):
            errors[key] = TimeoutError('环境检查已有过多在途任务')
            continue
        pending.add(key)
        threading.Thread(target=execute, args=(key, function), daemon=True,
                         name='routing-environment').start()
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            key, value, error = completed.get(timeout=remaining)
        except queue.Empty:
            break
        pending.discard(key)
        if error is None:
            results[key] = value
            if first_success:
                return results, errors
        else:
            errors[key] = error
    errors.update({key: TimeoutError('环境检查超过总期限') for key in pending})
    return results, errors


class EnvironmentCache:
    def __init__(self, vpn_getter, interface_getter, conflict_getter, log):
        self._getters = {'vpns': vpn_getter, 'interfaces': interface_getter,
                         'tun_conflicts': conflict_getter}
        self._log = log
        self._cache = {}
        self._lock = threading.Lock()

    def snapshot(self, include_tun=False, fresh=False):
        requested = time.monotonic()
        if not self._lock.acquire(timeout=36):
            raise TimeoutError('网络环境扫描仍在执行，请稍后重试')
        try:
            cached = self._cache.get(bool(include_tun))
            if cached and ((not fresh and requested - cached[0] < 30)
                           or cached[0] >= requested):
                return copy.deepcopy(cached[1])
            self._log('[routing] 网络环境扫描开始：VPN/网卡并行，总期限=35秒')
            calls = {key: value for key, value in self._getters.items()
                     if key != 'tun_conflicts' or include_tun}
            result, errors = bounded_calls(calls, 35)
            if errors:
                stage = next(iter(errors))
                self._log(f'[routing] 网络环境扫描失败：stage={stage}，异常={type(errors[stage]).__name__}')
                raise RuntimeError(f'网络环境读取失败（{stage}），请稍后重试') from errors[stage]
            result.setdefault('tun_conflicts', [])
            self._cache[bool(include_tun)] = (time.monotonic(), result)
            self._log(f'[routing] 网络环境扫描成功：耗时={time.monotonic()-requested:.2f}秒')
            return copy.deepcopy(result)
        finally:
            self._lock.release()
