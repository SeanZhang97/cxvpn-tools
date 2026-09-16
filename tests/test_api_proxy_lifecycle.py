# -*- coding: utf-8 -*-
"""API 代理事务提交后的离线回归；服务、持久化与后台任务均使用替身。"""
import copy
import threading
import unittest
from unittest import mock

from api import Api
from core import routing


class ProxyPostCommitTests(unittest.TestCase):
    def make_api(self):
        target = Api.__new__(Api)
        target._lock = threading.Lock()
        target._routing_lock = threading.Lock()
        previous = routing.normalize_config({
            'enabled': True,
            'controller_secret': 'offline-test-secret',
            'proxy_providers': [{
                'id': 'alpha', 'name': '离线订阅',
                'url': 'https://example.test/sub', 'enabled': True,
            }],
        })
        requested = copy.deepcopy(previous)
        requested['enabled'] = False
        target.cfg = {'routing': previous}
        target.routing = mock.Mock()
        target.routing.apply.return_value = {
            'ok': True, 'config': requested, 'warnings': [],
            'msg': '代理已关闭，节点核心正在后台启动',
            'standby_pending': True,
        }
        target.log = mock.Mock()
        target._routing_changed = mock.Mock()
        target._routing_with_private_fields = mock.Mock(side_effect=lambda value: value)
        target._routing_response = mock.Mock(side_effect=lambda value: value)
        target._close_codex_after_proxy_disabled = mock.Mock()
        target._start_routing_standby_reconcile = mock.Mock()
        target._poke_ui_state = mock.Mock()
        return target, requested

    def test_cache_cleanup_failure_preserves_commit_and_standby_dispatch(self):
        target, requested = self.make_api()
        with mock.patch('api.cfgmod.save') as save, mock.patch(
                'api.routing.subscription_store.prune_cache',
                side_effect=PermissionError('offline cache permission denied')):
            result = target.apply_routing(requested)

        self.assertTrue(result['ok'])
        self.assertEqual(result['msg'], '配置已保存，代理保持关闭')
        self.assertFalse(target.cfg['routing']['enabled'])
        save.assert_called_once()
        self.assertFalse(save.call_args.args[0]['routing']['enabled'])
        target.routing.apply.assert_called_once()
        target._start_routing_standby_reconcile.assert_called_once_with('代理关闭后')
        self.assertTrue(any(
            'PermissionError' in call.args[0] and '保留本次应用结果' in call.args[0]
            for call in target.log.call_args_list))

    def test_disabled_save_and_toggle_report_only_committed_operation(self):
        for source, message in (('manual', '配置已保存，代理保持关闭'),
                                ('proxy_toggle', '代理已关闭')):
            for pending in (False, True):
                with self.subTest(source=source, pending=pending):
                    target, requested = self.make_api()
                    target.cfg['routing']['enabled'] = False
                    target.routing.apply.return_value['standby_pending'] = pending
                    with mock.patch('api.cfgmod.save'), mock.patch(
                            'api.routing.subscription_store.prune_cache'):
                        result = target._apply_routing_locked(requested, source)
                    self.assertTrue(result['ok'])
                    self.assertEqual(result['msg'], message)
                    self.assertEqual(target._start_routing_standby_reconcile.called, pending)

    def test_save_failure_does_not_claim_saved_or_dispatch_standby(self):
        target, requested = self.make_api()
        with mock.patch('api.cfgmod.save', side_effect=OSError('offline save failure')):
            result = target.apply_routing(requested)
        self.assertFalse(result['ok'])
        self.assertIn('配置保存失败', result['msg'])
        self.assertTrue(target.cfg['routing']['enabled'])
        target._start_routing_standby_reconcile.assert_not_called()

    def test_standby_completion_publishes_runtime_after_success_failure_and_skip(self):
        for outcome in ('success', 'failure', 'skip'):
            with self.subTest(outcome=outcome):
                target, requested = self.make_api()
                target.cfg['routing'] = requested
                target._routing_revision = 7
                target.routing._service_state.return_value = {
                    'installed': True, 'backend': 'native',
                    'runtime_running': False, 'runtime_mode': 'stopped',
                }
                if outcome == 'failure':
                    target.routing.apply.side_effect = routing.RoutingError('offline failure')
                elif outcome == 'skip':
                    target.cfg['routing']['proxy_providers'] = []
                with mock.patch('api.routing.windows_system_proxy', return_value=''):
                    Api._start_routing_standby_reconcile(target, '代理关闭后')
                    target._routing_standby_thread.join(1)
                self.assertFalse(target._routing_standby_thread.is_alive())
                self.assertEqual(target._routing_revision, 8)
                target._poke_ui_state.assert_called_once()
                target._routing_changed.assert_not_called()
                self.assertFalse(target.cfg['routing']['enabled'])


if __name__ == '__main__':
    unittest.main()
