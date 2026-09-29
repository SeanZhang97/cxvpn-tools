# -*- coding: utf-8 -*-
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from core import routing
from core.routing_vpn import RoutingVpnWorker


class VpnWorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = routing.normalize_config({'enabled': True,
            'capture_mode': 'system-proxy', 'physical_interface': '',
            'rules': [{'match_type': 'suffix', 'domain': 'chaoxing.com',
                       'outbound': 'vpn:公司'}]})
        self.state = {'runtime_running': True, 'runtime_mode': 'active',
            'applied_config_signature': routing.RoutingManager._config_signature(self.config),
            'config_sha256': 'original', 'mihomo_pid': 123}
        self.manager = Mock()
        self.manager._config_signature = routing.RoutingManager._config_signature
        self.manager.runtime_matches.return_value = False
        self.manager._native_service.status.side_effect = lambda: dict(self.state)
        self.manager._native_service._compatible.return_value = True
        self.manager._runtime_vpns = {'公司': True}
        self.apply = Mock(return_value={'ok': True})
        self.lock = threading.Lock()
        self.worker = RoutingVpnWorker(lambda: {'routing': copy.deepcopy(self.config)},
            self.manager, self.lock, self.apply, Mock())
        clock = patch('core.routing_vpn.time.monotonic', return_value=100)
        scan = patch('core.routing_vpn.bounded_calls',
            return_value=({'vpns': [{'name': '公司', 'status': 'Disconnected'}]}, {}))
        self.clock, self.scan = clock.start(), scan.start()
        self.addCleanup(clock.stop)
        self.addCleanup(scan.stop)

    def test_two_stable_samples_trigger_one_serialized_apply(self):
        self.worker.check()
        self.apply.assert_not_called()
        self.clock.return_value = 116
        self.worker.check()
        self.apply.assert_called_once()
        self.assertEqual(self.apply.call_args.args[0], self.config)
        self.assertEqual(self.apply.call_args.args[1], {'公司': False})
        self.assertFalse(self.lock.locked())
        self.worker.check()
        self.assertEqual(self.apply.call_count, 1)

    def test_matching_baseline_never_applies(self):
        self.scan.return_value = ({'vpns': [{'name': '公司', 'status': 'Connected'}]}, {})
        self.worker.check()
        self.clock.return_value = 116
        self.worker.check()
        self.apply.assert_not_called()

    def test_unregistered_target_counts_as_disconnected(self):
        self.scan.return_value = ({'vpns': [{'name': '其他', 'status': 'Connected'}]}, {})
        self.worker.check()
        self.clock.return_value = 116
        self.worker.check()
        self.apply.assert_called_once()
        self.assertEqual(self.apply.call_args.args[1], {'公司': False})

    def test_missing_baseline_reconciles_once(self):
        self.manager._runtime_vpns = None
        self.worker.check()
        self.clock.return_value = 116
        self.worker.check()
        self.apply.assert_called_once()
        # 真实流程由 manager.apply 重建基线；以新基线验证后续不再对账。
        self.manager._runtime_vpns = {'公司': False}
        self.clock.return_value = 200
        self.worker.check()
        self.assertEqual(self.apply.call_count, 1)

    def test_baseline_with_extra_vpns_matches_limited_names(self):
        self.manager._runtime_vpns = {'公司': True, '其他': False}
        self.scan.return_value = ({'vpns': [
            {'name': '公司', 'status': 'Connected'},
            {'name': '其他', 'status': 'Disconnected'}]}, {})
        self.worker.check()
        self.clock.return_value = 116
        self.worker.check()
        self.apply.assert_not_called()

    def test_no_target_vpns_disables_monitoring(self):
        self.config['rules'] = []
        self.config['default_outbound'] = 'physical'
        self.worker.check()
        self.scan.assert_not_called()

    def test_disabled_configuration_is_never_switched(self):
        self.config['enabled'] = False
        self.worker.check()
        self.scan.assert_not_called()

    def test_changes_restart_debounce(self):
        self.worker.check()
        self.clock.return_value = 116
        self.scan.return_value = ({'vpns': [{'name': '公司', 'status': 'Connected'}]}, {})
        self.worker.check()
        self.apply.assert_not_called()

    def test_config_change_during_lock_cancels_old_candidate(self):
        self.worker.check()
        self.clock.return_value = 116
        original = dict(self.state)
        self.manager._native_service.status.side_effect = [original, {**original, 'config_sha256': 'changed'}]
        self.worker.check()
        self.apply.assert_not_called()

    def test_baseline_change_during_lock_cancels_candidate(self):
        self.worker.check()
        self.clock.return_value = 116
        original = dict(self.state)
        self.manager._native_service.status.side_effect = [original, dict(original)]
        self.manager._runtime_vpns = {'公司': False}
        self.worker.check()
        self.apply.assert_not_called()

    def test_shutdown_cancels_pending_work(self):
        self.worker.check()
        self.clock.return_value = 116
        self.worker.stop()
        self.worker.check()
        self.apply.assert_not_called()

    def test_failed_apply_is_throttled_and_lock_released(self):
        self.apply.return_value = {'ok': False}
        self.worker.check()
        self.clock.return_value = 116
        self.worker.check()
        self.worker.check()
        self.apply.assert_called_once()
        self.assertFalse(self.lock.locked())

    def test_scan_failure_raises_for_retry_backoff(self):
        self.scan.return_value = ({}, {'vpns': TimeoutError('slow')})
        with self.assertRaises(TimeoutError):
            self.worker.check()

    def test_unavailable_service_is_deferred_without_scanning(self):
        self.manager._native_service.status.return_value = None
        self.manager._native_service.status.side_effect = None
        self.worker.check()
        self.scan.assert_not_called()
        self.apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
