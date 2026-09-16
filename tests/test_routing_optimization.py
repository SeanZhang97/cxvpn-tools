# -*- coding: utf-8 -*-
"""代理控制并发与确认边界；外部调用均由确定性的事件或模拟替代。"""
import copy
import threading
import time
import unittest
from unittest.mock import Mock, patch

from api import Api
from core import routing, subscription_store
from core.routing_environment import EnvironmentCache, bounded_calls
from core.routing_tasks import commit_scope
from core.routing_speedtest import RoutingTestJobs, test_nodes
from core.routing_updates import RoutingUpdateWorker


def config():
    return routing.normalize_config({
        'enabled': False, 'capture_mode': 'system-proxy',
        'controller_secret': 'offline-secret', 'default_outbound': 'proxy:alpha',
        'proxy_providers': [{
            'id': 'alpha', 'name': '订阅 e\u0301 🇨🇳',
            'url': 'https://example.test/sub', 'enabled': True,
            'selection_mode': 'manual', 'selected_node': '节点 A 🇯🇵',
            'auto_update': True, 'interval': 300,
        }],
    })


def api_instance():
    api = Api.__new__(Api)
    api.cfg = {'routing': config()}
    api._lock = threading.Lock()
    api._routing_lock = threading.Lock()
    api._routing_revision = 0
    api.routing = Mock()
    api.routing.status.return_value = {'running': False, 'core_running': True}
    api.routing.select_proxy_node.return_value = {'groups': [{'id': 'alpha', 'selected': '节点 B'}]}
    api.log = Mock()
    api._poke_ui_state = Mock()
    return api


class RoutingOptimizationTests(unittest.TestCase):
    def test_failed_hot_reload_rolls_back_before_one_full_transaction_retry(self):
        manager = routing.RoutingManager()
        value = config()
        value['capture_mode'] = 'tun'
        manager._native_service = Mock()
        manager._native_service.apply.side_effect = [
            {'transaction_id': 'hot', 'hot_reload': True, 'config_path': 'fixed-service-path'},
            {'transaction_id': 'full', 'config_sha256': 'ABC'}]
        manager._controller_request = Mock(side_effect=OSError('reload rejected'))
        manager._wait_native_ready = Mock()
        manager._native_provider_files = Mock(return_value=[])
        manager._refresh_user_proxy_settings = Mock(return_value=True)
        with patch.object(
                routing, 'windows_manual_proxy_state',
                return_value={'enabled': False, 'server': ''}):
            manager._install('offline-candidate', value)
        manager._native_service.rollback.assert_called_once_with('hot')
        manager._native_service.commit.assert_called_once_with('full')
        self.assertEqual([call.kwargs['allow_reload'] for call in manager._native_service.apply.call_args_list], [True, False])
        self.assertTrue(manager.runtime_matches(value, {'config_sha256': 'ABC'}))

    def test_commit_success_is_not_rolled_back_when_success_log_fails(self):
        manager = routing.RoutingManager()
        value = config()
        value['capture_mode'] = 'tun'
        manager._native_service = Mock()
        manager._native_service.apply.return_value = {'transaction_id': 'tx', 'config_sha256': 'ABC'}
        manager._wait_native_ready = Mock()
        manager._native_provider_files = Mock(return_value=[])
        manager._refresh_user_proxy_settings = Mock(return_value=True)
        def log(message):
            if '事务提交成功' in message:
                raise UnicodeEncodeError('ascii', '节点 🇯🇵', 0, 1, 'offline console')
        manager.log = log
        with patch.object(
                routing, 'windows_manual_proxy_state',
                return_value={'enabled': False, 'server': ''}):
            manager._install('offline-candidate', value)
        manager._native_service.commit.assert_called_once_with('tx')
        manager._native_service.rollback.assert_not_called()

    def test_slow_provider_does_not_hold_control_lock_and_stale_commit_is_rejected(self):
        api = api_instance()
        entered, release = threading.Event(), threading.Event()
        committed, errors = [], []

        def operation(_config):
            entered.set()
            release.wait(2)
            with commit_scope():
                committed.append(True)
            return {'ok': True}

        def run():
            try:
                api._routing_provider_task('alpha', operation)
            except routing.RoutingError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(api._routing_lock.acquire(timeout=.1))
            try:
                api._routing_changed()
            finally:
                api._routing_lock.release()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(committed)
        self.assertTrue(errors)

    def test_duplicate_provider_task_is_rejected_and_lock_is_reusable_after_failure(self):
        api = api_instance()
        def operation(_):
            with self.assertRaises(routing.RoutingError):
                api._routing_provider_task('alpha', lambda _: {})
            raise ValueError('offline')
        with self.assertRaises(ValueError):
            api._routing_provider_task('alpha', operation)
        self.assertTrue(api._routing_provider_task('alpha', lambda _: {'ok': True})['ok'])

    def test_worker_stop_rejects_nested_commit(self):
        api = api_instance()
        cancel = threading.Event()
        def operation(_):
            cancel.set()
            with commit_scope():
                self.fail('cancelled write')
        with self.assertRaises(routing.RoutingError):
            api._routing_provider_task('alpha', operation, cancel)

    def test_nested_commit_scope_is_reentrant(self):
        api = api_instance()
        def operation(_):
            with commit_scope(), commit_scope():
                return {'ok': True}
        self.assertTrue(api._routing_provider_task('alpha', operation)['ok'])

    def test_invalid_task_id_is_rejected_before_logging(self):
        api = api_instance()
        with self.assertRaises(routing.RoutingError):
            api._routing_provider_task('https://example.test/?token=secret', lambda _: {})
        api.log.assert_not_called()

    def test_saved_preference_does_not_claim_other_pending_settings_are_running(self):
        manager = routing.RoutingManager()
        previous = config()
        manager._runtime_signature = manager._config_signature(previous)
        manager._runtime_sha256 = 'ABC'
        changed = copy.deepcopy(previous)
        changed['traffic_mode'] = 'global'
        selected = copy.deepcopy(changed)
        selected['proxy_providers'][0]['selected_node'] = '节点 B'
        manager.remember_selection(selected, changed)
        self.assertFalse(manager.runtime_matches(selected, {'config_sha256': 'ABC'}))
        manager.remember_selection(selected, previous)
        self.assertTrue(manager.runtime_matches(selected, {'config_sha256': 'ABC'}))

    def test_dirty_standby_configuration_cannot_use_fast_enable(self):
        manager = routing.RoutingManager()
        value = config()
        manager._runtime_signature = manager._config_signature(value)
        manager._runtime_sha256 = 'ABC'
        value['proxy_providers'][0]['selected_node'] = '节点 B'
        value['enabled'] = True
        manager._service_state = Mock(return_value={
            'installed': True, 'backend': 'native', 'state': 'Running',
            'runtime_running': True, 'runtime_mode': 'standby',
            'fast_toggle_ready': True, 'config_sha256': 'ABC', 'service_compatible': True})
        manager._fast_toggle_system_proxy = Mock()
        with patch.object(routing, 'verify_runtime', side_effect=routing.RoutingError('full apply reached')):
            with self.assertRaisesRegex(routing.RoutingError, 'full apply reached'):
                manager.apply(value, allow_fast_toggle=True)
        manager._fast_toggle_system_proxy.assert_not_called()

    def test_manual_selection_readback_mismatch_restores_old_node(self):
        manager = routing.RoutingManager()
        value = config()
        prefix = f'[{value["proxy_providers"][0]["name"]}] '
        old, new = prefix + '节点 A 🇯🇵', prefix + '节点 e\u0301 🇨🇳'
        manager._controller_request = Mock(side_effect=[
            {'all': [old, new], 'now': old}, {}, {'now': old}, {}, {'now': old}])
        with self.assertRaisesRegex(routing.RoutingError, '未确认生效'):
            manager.select_proxy_node(value, 'alpha', new)
        self.assertEqual(manager._controller_request.call_args_list[3].kwargs['payload'], {'name': old})

    def test_confirmed_selection_returns_groups_even_if_overview_is_unavailable(self):
        manager = routing.RoutingManager()
        value = config()
        name = f'[{value["proxy_providers"][0]["name"]}] 节点 e\u0301 🇨🇳'
        manager._controller_request = Mock(side_effect=[{'all': [name], 'now': name}, {}, {'now': name}])
        manager.proxy_overview = Mock(return_value=[])
        self.assertEqual(manager.select_proxy_node(value, 'alpha', name)['groups'][0]['selected'], name)

    def test_standby_manual_selection_is_confirmed_before_save(self):
        api = api_instance()
        events = []
        api.routing.select_proxy_node.side_effect = lambda *_: events.append('confirmed') or {'groups': []}
        with patch.object(subscription_store, 'load_node_snapshot', return_value={'nodes': [{'name': '节点 B'}]}), \
                patch('api.cfgmod.save', side_effect=lambda _: events.append('saved')):
            result = api.save_proxy_preference('alpha', 'manual', '节点 B')
        self.assertTrue(result['ok'])
        self.assertEqual(events, ['confirmed', 'saved'])

    def test_failed_selection_never_saves_and_failed_save_restores_runtime(self):
        for selection_fails in (True, False):
            with self.subTest(selection_fails=selection_fails):
                api = api_instance()
                previous = copy.deepcopy(api.cfg)
                if selection_fails:
                    api.routing.select_proxy_node.side_effect = routing.RoutingError('readback mismatch')
                with patch.object(subscription_store, 'load_node_snapshot', return_value={'nodes': [{'name': '节点 B'}]}), \
                        patch('api.cfgmod.save', side_effect=OSError('disk full')) as save:
                    result = api.save_proxy_preference('alpha', 'manual', '节点 B')
                self.assertFalse(result['ok'])
                self.assertEqual(api.cfg, previous)
                self.assertEqual(save.call_count, 0 if selection_fails else 1)
                self.assertEqual(api.routing.select_proxy_node.call_count, 1 if selection_fails else 2)

    def test_failed_subscription_does_not_starve_next_due_subscription(self):
        value = config()
        value['proxy_providers'].append({**value['proxy_providers'][0], 'id': 'beta', 'name': '订阅二'})
        manager = Mock()
        manager.status.return_value = {'core_running': True}
        manager.refresh_proxy_provider.side_effect = [routing.RoutingError('offline'), {'ok': True}]
        worker = RoutingUpdateWorker(lambda: {'routing': value}, manager, threading.Lock())
        with patch.object(subscription_store, 'cache_status', return_value={'updated_at': 0}), \
                patch('core.routing_updates.time.time', return_value=1000):
            self.assertTrue(worker._update_one_due_provider())
            self.assertTrue(worker._update_one_due_provider())
            self.assertFalse(worker._update_one_due_provider())
        self.assertEqual([call.args[1] for call in manager.refresh_proxy_provider.call_args_list], ['alpha', 'beta'])

    def test_environment_scan_is_parallel_cached_and_tun_is_opt_in(self):
        barrier = threading.Barrier(2)
        vpn = Mock(side_effect=lambda: barrier.wait(1) and [])
        interface = Mock(side_effect=lambda: barrier.wait(1) and [])
        tun = Mock(return_value=[])
        cache = EnvironmentCache(vpn, interface, tun, lambda _: None)
        cache.snapshot(False)
        cache.snapshot(False)
        vpn.assert_called_once()
        interface.assert_called_once()
        tun.assert_not_called()

    def test_bounded_dns_or_probe_returns_without_waiting_for_stalled_os_call(self):
        release = threading.Event()
        started = time.monotonic()
        try:
            values, errors = bounded_calls({'slow': lambda: release.wait(2)}, .03)
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(values)
        self.assertIsInstance(errors['slow'], TimeoutError)

    def test_cancelled_test_does_not_wait_for_inflight_request_or_publish_late_progress(self):
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        events, returned = [], threading.Event()
        def request(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return {'delay': 20}
        def run():
            test_nodes(request, {}, [{'name': '节点 🇨🇳'}], 'https://example.test/', events.append, cancel)
            returned.set()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            cancel.set()
            self.assertTrue(returned.wait(.5))
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(any(item['event'] == 'result' for item in events))

    def test_job_version_skips_unchanged_payload_and_timeout_has_distinct_result(self):
        jobs = RoutingTestJobs()
        release = threading.Event()
        def runner(progress, cancel):
            release.wait(1)
            cancel.set()
            raise TimeoutError('deadline')
        started = jobs.start(runner)
        job_id = started['job_id']
        state = jobs.get(job_id)['job']
        self.assertTrue(jobs.get(job_id, state['version'])['unchanged'])
        release.set()
        for _ in range(100):
            state = jobs.get(job_id)['job']
            if state['status'] == 'error':
                break
            time.sleep(.005)
        self.assertEqual(state['msg'], '测速超时')


if __name__ == '__main__':
    unittest.main()
