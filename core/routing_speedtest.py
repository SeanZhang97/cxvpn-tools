# -*- coding: utf-8 -*-
"""分流节点测速并发执行与可轮询任务状态。"""
from __future__ import annotations

import copy
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait, FIRST_COMPLETED


METADATA_NODE_RE = re.compile(
    r'(?:剩余|可用|已用|总计|套餐|流量|重置|到期|过期|有效期|官网|网站|'
    r'公告|通知|客服|群组|QQ群|TG群|Telegram|更新时间|订阅信息)[：:\s]|'
    r'(?:GB|MB|TB)\s*(?:剩余|可用)|(?:距离|下次).*重置|'
    r'\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}', re.I)
JOB_TTL_SECONDS = 15 * 60
# Keep the controller request timeout slightly above Mihomo's healthcheck
# timeout.  These are intentionally module constants so callers can opt into
# a different budget without having to duplicate the endpoint contract.
DEFAULT_HEALTHCHECK_TIMEOUT_MS = 10_000
DEFAULT_NODE_REQUEST_TIMEOUT_SECONDS = 12.0


def is_metadata_node(node):
    return bool(METADATA_NODE_RE.search(str(
        node.get('display_name') or node.get('name') or '')))


def node_healthcheck_path(node, health_url, timeout=DEFAULT_HEALTHCHECK_TIMEOUT_MS):
    """生成兼容普通代理和 proxy-provider 节点的测速路径。"""
    name = str(node.get('name') or '')
    # Runtime snapshots use ``provider_name``; callers importing Clash Verge
    # style proxy records commonly expose the same value as ``provider``.
    provider_name = str(
        node.get('provider_name') or node.get('provider') or '').strip()
    query = urllib.parse.urlencode({
        'url': health_url, 'timeout': int(timeout), 'expected': 204})
    encoded_name = urllib.parse.quote(name, safe='')
    if provider_name:
        encoded_provider = urllib.parse.quote(provider_name, safe='')
        return (f'/providers/proxies/{encoded_provider}/{encoded_name}'
                f'/healthcheck?{query}')
    return f'/proxies/{encoded_name}/delay?{query}'


def _coerce_delay(value):
    """Normalize Mihomo's delay value without accepting sentinel booleans.

    Mihomo normally returns an integer, but proxy-provider implementations and
    older controller builds have returned numeric strings/floats.  Treating
    those as a failed probe made otherwise healthy nodes appear unavailable.
    Boolean values are explicitly rejected because ``bool`` subclasses
    ``int`` in Python and must not become a 1ms result.
    """
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return max(1, int(round(number)))


def test_nodes(controller_request, controller_config, nodes, health_url,
               progress=None, cancel_event=None, workers=8, total_timeout=120):
    """并发测试节点；每完成一个节点立即回调，不测试订阅说明伪节点。"""
    cancel = cancel_event or threading.Event()
    candidates = [dict(node) for node in nodes or [] if not is_metadata_node(node)]
    if progress:
        progress({'event': 'init', 'nodes': candidates})

    def test_one(node):
        name = str(node.get('name') or '')
        if cancel.is_set():
            return None
        if progress:
            progress({'event': 'testing', 'name': name})
        started = time.monotonic()
        path = node_healthcheck_path(
            node, health_url, timeout=DEFAULT_HEALTHCHECK_TIMEOUT_MS)
        delay = 0
        try:
            payload = controller_request(
                controller_config, path,
                timeout=DEFAULT_NODE_REQUEST_TIMEOUT_SECONDS)
            value = payload.get('delay') if isinstance(payload, dict) else 0
            delay = _coerce_delay(value)
        except (OSError, ValueError, urllib.error.URLError):
            delay = 0
        elapsed_ms = max(0, int(round((time.monotonic() - started) * 1000)))
        result = {
            **node,
            'delay': delay,
            'alive': delay > 0,
            'tested': True,
            'tested_at': int(time.time()),
            'elapsed_ms': elapsed_ms,
        }
        return result

    results = {}
    concurrency = max(1, min(int(workers or 8), 16))
    deadline = time.monotonic() + max(.01, float(total_timeout))
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='routing-speedtest')
    iterator = iter(candidates)
    pending = set()
    def submit_one():
        node = next(iterator, None)
        if node is not None and not cancel.is_set():
            pending.add(pool.submit(test_one, node))
    try:
        for _ in range(concurrency):
            submit_one()
        while pending and not cancel.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancel.set()
                raise TimeoutError('节点测速超过总期限，已停止未完成请求')
            completed, _ = wait(pending, timeout=min(.2, remaining), return_when=FIRST_COMPLETED)
            for future in completed:
                pending.remove(future)
                try:
                    result = future.result()
                except CancelledError:
                    continue
                if result:
                    results[result['name']] = result
                    if progress and not cancel.is_set():
                        # 在领取任务的线程中提交，保留配置版本 guard；worker 仅做网络请求。
                        progress({'event': 'result', 'node': result})
                submit_one()
    finally:
        for future in pending:
            if future.done() and not future.cancelled():
                result = future.result()
                if result:
                    results[result['name']] = result
        for future in pending:
            future.cancel()
        # 正在执行的回环 HTTP 请求最多等待自身 12 秒；取消不阻塞其它路由操作。
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def test_group(manager, config, group_id, health_url, error_type,
               persist_nodes, progress=None, cancel_event=None):
    """对运行态代理组逐节点测速，并保存订阅组的最新安全快照。"""
    target = str(group_id or '').strip().lower()
    _internal_name, display_name, _strategy = manager._group_identity(
        config, target)
    group = next((item for item in manager.proxy_overview(config)
                  if item.get('id') == target), None)
    if not group:
        raise error_type('代理组不存在，或统一分流服务尚未运行')
    collected = {}

    def on_progress(event):
        if event.get('event') == 'result':
            result = event['node']
            collected[result['name']] = result
            if target != 'all':
                merged = [{**node, **collected.get(node['name'], {})}
                          for node in group.get('nodes') or []]
                persist_nodes(config, target, merged)
        if progress:
            progress(event)

    results = test_nodes(
        manager._controller_request, config, group.get('nodes') or [],
        health_url, on_progress, cancel_event, workers=8)
    if target != 'all':
        merged = [{**node, **results.get(str(node.get('name') or ''), {})}
                  for node in group.get('nodes') or []]
        persist_nodes(config, target, merged)
    return {
        'ok': True,
        'msg': f'代理组“{display_name}”测速完成',
        'nodes': list(results.values()),
        'delays': {name: item.get('delay', 0)
                   for name, item in results.items()},
        'groups': manager.proxy_overview(config),
    }


class RoutingTestJobs:
    """保存后台测速状态，供 pywebview 前端轮询和取消。"""
    def __init__(self, logger=None):
        self._logger = logger or (lambda _message: None)
        self._lock = threading.Lock()
        self._jobs = {}

    def _prune(self):
        cutoff = time.time() - JOB_TTL_SECONDS
        stale = [job_id for job_id, job in self._jobs.items()
                 if job['updated_at'] < cutoff and
                 job['status'] in {'completed', 'cancelled', 'error'}]
        for job_id in stale:
            self._jobs.pop(job_id, None)

    @staticmethod
    def _public(job):
        nodes = list(job['nodes'].values())
        nodes.sort(key=lambda item: item.get('order', 0))
        return {
            'id': job['id'],
            'version': job.get('version', 0),
            'status': job['status'],
            'total': job['total'],
            'completed': job['completed'],
            'alive': job['alive'],
            'failed': job['failed'],
            'nodes': copy.deepcopy(nodes),
            'msg': job.get('msg', ''),
            'error': job.get('error', ''),
        }

    def start(self, runner, label='节点测速', error_message=None):
        job_id = uuid.uuid4().hex
        job = {
            'id': job_id,
            'version': 1,
            'status': 'pending',
            'total': 0,
            'completed': 0,
            'alive': 0,
            'failed': 0,
            'nodes': {},
            'msg': f'{label}准备中…',
            'error': '',
            'updated_at': time.time(),
            'cancel': threading.Event(),
        }
        with self._lock:
            self._prune()
            if sum(item['status'] in {'pending', 'running'} for item in self._jobs.values()) >= 4:
                return {'ok': False, 'msg': '已有多个测速任务，请先等待完成或停止测速'}
            self._jobs[job_id] = job
        self._logger(f'[routing-test] 任务已提交: job={job_id[:8]}，总期限=120秒')

        def progress(event):
            with self._lock:
                current = self._jobs.get(job_id)
                if not current or current['status'] not in {'pending', 'running'}:
                    return
                current['version'] += 1
                kind = event.get('event')
                if kind == 'init':
                    current['nodes'] = {
                        str(node.get('name') or ''): {
                            **node, 'order': index, 'state': 'pending'}
                        for index, node in enumerate(event.get('nodes') or [])
                        if str(node.get('name') or '')
                    }
                    current['total'] = len(current['nodes'])
                    current['status'] = 'running'
                    current['msg'] = f'正在测速 0/{current["total"]}'
                elif kind == 'testing':
                    node = current['nodes'].get(str(event.get('name') or ''))
                    if node:
                        node['state'] = 'testing'
                elif kind == 'result':
                    result = dict(event.get('node') or {})
                    name = str(result.get('name') or '')
                    previous = current['nodes'].get(name, {})
                    if name:
                        current['nodes'][name] = {
                            **previous, **result, 'state': 'completed'}
                    if previous.get('state') != 'completed':
                        current['completed'] += 1
                        current['alive'] += int(result.get('alive') is True)
                    else:
                        current['alive'] += int(result.get('alive') is True) - int(previous.get('alive') is True)
                    current['failed'] = current['completed'] - current['alive']
                    current['msg'] = (
                        f'正在测速 {current["completed"]}/{current["total"]}，'
                        f'可用 {current["alive"]}')
                current['updated_at'] = time.time()

        def execute():
            try:
                with self._lock:
                    job['updated_at'] = time.time()
                self._logger(f'[routing-test] 后台已领取任务: job={job_id[:8]}')
                result = runner(progress, job['cancel'])
                with self._lock:
                    current = self._jobs.get(job_id)
                    if not current:
                        return
                    if job['cancel'].is_set():
                        current['status'] = 'cancelled'
                        current['msg'] = (
                            f'测速已停止，已完成 {current["completed"]}/'
                            f'{current["total"]}')
                    else:
                        current['status'] = 'completed'
                        current['msg'] = (
                            f'测速完成：{current["alive"]}/'
                            f'{current["total"]} 个节点可用')
                    current['result'] = result
                    current['version'] += 1
                    current['updated_at'] = time.time()
                self._logger(f'[routing-test] 任务终态: job={job_id[:8]}，status={current["status"]}')
            except Exception as exc:
                self._logger(f'[routing] 后台测速任务失败: {type(exc).__name__}')
                safe_error = '测速任务执行失败，请重试或查看运行日志'
                if callable(error_message):
                    try:
                        safe_error = str(error_message(exc) or safe_error)
                    except Exception:
                        pass
                with self._lock:
                    current = self._jobs.get(job_id)
                    if current:
                        if isinstance(exc, TimeoutError):
                            current['status'] = 'error'
                            current['error'] = '节点测速超过总期限，已停止未完成请求'
                            current['msg'] = '测速超时'
                        elif job['cancel'].is_set():
                            current['status'] = 'cancelled'
                            current['error'] = ''
                            current['msg'] = (
                                f'测速已停止，已完成 {current["completed"]}/'
                                f'{current["total"]}')
                        else:
                            current['status'] = 'error'
                            current['error'] = safe_error
                            current['msg'] = '测速失败'
                        current['version'] += 1
                        current['updated_at'] = time.time()

        threading.Thread(
            target=execute, daemon=True,
            name=f'routing-speedtest-{job_id[:8]}').start()
        return {'ok': True, 'job_id': job_id, 'job': self.get(job_id)['job']}

    def get(self, job_id, after_version=-1):
        with self._lock:
            self._prune()
            job = self._jobs.get(str(job_id or ''))
            if not job:
                return {'ok': False, 'msg': '测速任务不存在或已过期'}
            if int(after_version) == job.get('version', 0):
                return {'ok': True, 'unchanged': True, 'version': job['version']}
            return {'ok': True, 'job': self._public(job)}

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(str(job_id or ''))
            if not job:
                return {'ok': False, 'msg': '测速任务不存在或已过期'}
            job['cancel'].set()
            job['version'] += 1
            if job['status'] in {'pending', 'running'}:
                job['msg'] = '正在停止测速…'
            job['updated_at'] = time.time()
            return {'ok': True, 'job': self._public(job)}

    def cancel_all(self, message='测速已取消'):
        with self._lock:
            for job in self._jobs.values():
                if job['status'] in {'pending', 'running'}:
                    job['cancel'].set()
                    job['msg'] = message
                    job['version'] += 1
                    job['updated_at'] = time.time()
