# -*- coding: utf-8 -*-
import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from core import routing
from core.routing_network import RoutingNetworkWorker


class AutomaticInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.config = routing.normalize_config({'enabled': True,
            'capture_mode': 'system-proxy', 'physical_interface': ''})

    def test_generation_resolves_runtime_without_pinning_preference(self):
        generated = routing.build_mihomo_config(self.config, [], physical_interface='WLAN')
        self.assertEqual(self.config['physical_interface'], '')
        self.assertEqual(generated['proxies'][0]['interface-name'], 'WLAN')
        self.assertEqual(generated['cxvpn-config-signature'],
                         routing.RoutingManager._config_signature(self.config))

    def test_apply_returns_auto_but_writes_resolved_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            manager = routing.RoutingManager()
            manager._data_dir = root
            manager._service_state = Mock(return_value={})
            manager._environment.snapshot = Mock(return_value={
                'vpns': [], 'interfaces': [{'name': 'WLAN'}], 'tun_conflicts': []})
            manager._test_config = Mock()
            manager._install = Mock()
            manager.status = Mock(return_value={})
            with patch.object(routing, 'verify_runtime'), patch.object(
                    routing, 'windows_system_proxy', return_value=''):
                result = manager.apply(self.config)
            self.assertEqual(result['config']['physical_interface'], '')
            path = manager._install.call_args.args[0]
            with open(path, encoding='utf-8') as stream:
                runtime = json.load(stream)
            self.assertEqual(runtime['proxies'][0]['interface-name'], 'WLAN')

    def test_fixed_disconnected_interface_is_not_replaced(self):
        self.config['physical_interface'] = '以太网'
        with self.assertRaisesRegex(routing.RoutingError, '指定物理接口.*以太网.*WLAN'):
            routing.validate_environment(self.config, [], [{'name': 'WLAN'}], [])
        self.assertEqual(self.config['physical_interface'], '以太网')

    def test_no_interfaces_is_a_distinct_error(self):
        with self.assertRaisesRegex(routing.RoutingError, '未找到可用'):
            routing.validate_environment(self.config, [], [], [])

    def test_status_uses_actual_tun_not_saved_system_proxy(self):
        manager = routing.RoutingManager()
        manager._service_state = Mock(return_value={'backend': 'native',
            'state': 'Running', 'runtime_running': True, 'runtime_mode': 'active',
            'applied_capture_mode': 'tun', 'applied_physical_interface': 'WLAN',
            'system_proxy_active': False})
        result = manager.status(self.config, quick=True)
        self.assertTrue(result['running'])
        self.assertTrue(result['configuration_pending'])
        self.assertEqual(result['capture_mode'], 'tun')
        self.assertEqual(result['physical_interface'], 'WLAN')

    def test_status_infers_tun_from_older_active_native_service(self):
        manager = routing.RoutingManager()
        manager._service_state = Mock(return_value={'backend': 'native',
            'state': 'Running', 'runtime_running': True, 'runtime_mode': 'active',
            'system_proxy_active': False})
        result = manager.status(self.config, quick=True)
        self.assertTrue(result['running'])
        self.assertTrue(result['configuration_pending'])
        self.assertEqual(result['capture_mode'], 'tun')

    def test_status_marks_different_applied_signature_pending(self):
        manager = routing.RoutingManager()
        manager._service_state = Mock(return_value={'backend': 'native',
            'state': 'Running', 'runtime_running': True, 'runtime_mode': 'active',
            'applied_capture_mode': 'system-proxy', 'applied_config_signature': 'old',
            'system_proxy_active': True})
        result = manager.status(self.config, quick=True)
        self.assertTrue(result['running'])
        self.assertTrue(result['configuration_pending'])

    def test_status_requires_same_version_service_when_binary_hash_differs(self):
        manager = routing.RoutingManager()
        manager._service_state = Mock(return_value={'backend': 'native',
            'state': 'Running', 'runtime_running': True, 'runtime_mode': 'active',
            'service_version': routing._routing_service.SERVICE_VERSION,
            'service_compatible': False, 'applied_capture_mode': 'tun',
            'system_proxy_active': False})
        result = manager.status(self.config, quick=True)
        self.assertTrue(result['service_update_required'])

    def test_physical_scan_filters_virtual_and_orders_combined_metric(self):
        with patch.object(routing, '_run_powershell_json', return_value=[]) as run:
            routing.list_physical_interfaces()
        script = run.call_args.args[0]
        self.assertIn('HardwareInterface', script)
        self.assertIn('$_.RouteMetric + $_.InterfaceMetric', script)


class NetworkWorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = routing.normalize_config({'enabled': True,
            'capture_mode': 'system-proxy', 'physical_interface': ''})
        self.state = {'runtime_running': True, 'runtime_mode': 'active',
            'applied_config_signature': routing.RoutingManager._config_signature(self.config),
            'applied_physical_interface': '以太网', 'config_sha256': 'original', 'mihomo_pid': 123}
        self.manager = Mock()
        self.manager._config_signature = routing.RoutingManager._config_signature
        self.manager.runtime_matches.return_value = False
        self.manager._native_service.status.side_effect = lambda: dict(self.state)
        self.manager._native_service._compatible.return_value = True
        self.apply = Mock(return_value={'ok': True})
        self.lock = threading.Lock()
        self.worker = RoutingNetworkWorker(lambda: {'routing': copy.deepcopy(self.config)},
            self.manager, self.lock, self.apply, Mock())
        clock = patch('core.routing_network.time.monotonic', return_value=100)
        scan = patch('core.routing_network.bounded_calls',
            return_value=({'interfaces': [{'name': 'WLAN'}]}, {}))
        self.clock, self.scan = clock.start(), scan.start()
        self.addCleanup(clock.stop)
        self.addCleanup(scan.stop)

    def test_two_stable_samples_trigger_one_serialized_apply(self):
        self.worker.check()
        self.apply.assert_not_called()
        self.clock.return_value = 116
        self.worker.check()
        self.apply.assert_called_once_with(self.config, 'WLAN')
        self.assertFalse(self.lock.locked())
        self.worker.check()
        self.assertEqual(self.apply.call_count, 1)

    def test_no_available_interface_does_not_stop_existing_runtime(self):
        self.scan.return_value = ({'interfaces': []}, {})
        self.worker.check()
        self.clock.return_value = 130
        self.worker.check()
        self.apply.assert_not_called()

    def test_fixed_or_disabled_configuration_is_never_switched(self):
        self.config['physical_interface'] = '以太网'
        self.worker.check()
        self.scan.assert_not_called()
        self.config['physical_interface'] = ''
        self.config['enabled'] = False
        self.worker.check()
        self.scan.assert_not_called()

    def test_changes_restart_debounce(self):
        self.worker.check()
        self.clock.return_value = 116
        self.scan.return_value = ({'interfaces': [{'name': 'USB网卡'}]}, {})
        self.worker.check()
        self.apply.assert_not_called()

    def test_config_change_during_lock_cancels_old_candidate(self):
        self.worker.check()
        self.clock.return_value = 116
        original = dict(self.state)
        self.manager._native_service.status.side_effect = [original, {**original, 'config_sha256': 'changed'}]
        self.worker.check()
        self.apply.assert_not_called()

    def test_shutdown_cancels_pending_work(self):
        self.worker.check()
        self.clock.return_value = 116
        self.worker.stop()
        self.worker.check()
        self.apply.assert_not_called()

    def test_stop_waits_for_worker_with_bounded_timeout(self):
        thread = Mock()
        thread.is_alive.return_value = False
        self.worker._thread = thread
        self.assertTrue(self.worker.stop(timeout=10))
        thread.join.assert_called_once_with(3)

    def test_incompatible_service_does_not_trigger_background_upgrade(self):
        self.manager._native_service._compatible.return_value = False
        self.worker.check()
        self.scan.assert_not_called()

    def test_unavailable_service_is_deferred_without_scanning(self):
        self.manager._native_service.status.return_value = None
        self.manager._native_service.status.side_effect = None

        self.worker.check()

        self.scan.assert_not_called()
        self.apply.assert_not_called()

    def test_different_unapplied_config_is_not_applied_implicitly(self):
        self.state['applied_config_signature'] = 'other'
        self.worker.check()
        self.scan.assert_not_called()

    def test_failed_apply_is_throttled_and_lock_released(self):
        self.apply.return_value = {'ok': False}
        self.worker.check()
        self.clock.return_value = 116
        self.worker.check()
        self.worker.check()
        self.apply.assert_called_once()
        self.assertFalse(self.lock.locked())

    def test_service_disappearing_before_apply_cancels_candidate(self):
        self.worker.check()
        self.clock.return_value = 116
        self.manager._native_service.status.side_effect = [dict(self.state), None]

        self.worker.check()

        self.apply.assert_not_called()
        self.assertFalse(self.lock.locked())


if __name__ == '__main__':
    unittest.main()
