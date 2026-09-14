# -*- coding: utf-8 -*-
import json
import os
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from core import routing, routing_support, subscription_store
from core.routing_updates import RoutingUpdateWorker


def provider():
    return {
        'id': 'alpha',
        'name': '订阅一',
        'url': 'https://example.test/sub?token=secret-value',
        'enabled': True,
    }


def snapshot_nodes():
    return [{
        'name': 'JP-01',
        'display_name': '日本东京 01',
        'type': 'vless',
        'delay': 86,
        'alive': True,
        'tested': True,
        'tested_at': 1_800_000_000,
    }]


class RoutingReliabilityTests(unittest.TestCase):
    def test_cache_status_treats_file_race_as_unavailable(self):
        item = provider()
        with tempfile.TemporaryDirectory() as root:
            path = subscription_store.cache_path(item, root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as stream:
                stream.write(b'proxies: []\n')
            with mock.patch.object(
                    subscription_store.os.path, 'getsize',
                    side_effect=OSError('cache removed during status check')):
                status = subscription_store.cache_status(item, root)
        self.assertFalse(status['available'])
        self.assertEqual(status['node_count'], 0)

    def test_temporary_process_is_killed_when_graceful_stop_times_out(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('mihomo', 3), 0]
        close_job = mock.Mock()

        routing_support.stop_temporary_process(process, close_job)

        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        close_job.assert_called_once()

    def test_disabled_rules_are_preserved_but_not_generated(self):
        config = routing.normalize_config({
            'enabled': False,
            'default_outbound': 'physical',
            'rules': [{
                'id': 'disabled-rule',
                'enabled': False,
                'match_type': 'suffix',
                'domain': 'example.test',
                'outbound': 'block',
            }],
        })

        generated = routing.build_mihomo_config(config, [])

        self.assertEqual(len(config['rules']), 1)
        self.assertFalse(config['rules'][0]['enabled'])
        self.assertEqual(generated['rules'], ['MATCH,PHYSICAL'])

    def test_aggregate_proxy_requires_enabled_provider(self):
        with self.assertRaisesRegex(routing.RoutingError, '至少需要一个已启用'):
            routing.normalize_config({
                'enabled': False,
                'default_outbound': 'proxy',
                'proxy_providers': [],
            })

    def test_unknown_service_state_prevents_false_disable_success(self):
        manager = routing.RoutingManager()
        manager._service_state = mock.Mock(return_value={
            'installed': None, 'state': 'Unknown'})

        with self.assertRaisesRegex(routing.RoutingError, '未保存关闭状态'):
            manager.apply({'enabled': False, 'default_outbound': 'physical'})

    def test_native_disable_stops_runtime_without_uninstall_or_uac(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._service_state = mock.Mock(return_value={
            'installed': True, 'state': 'Running', 'backend': 'native',
            'runtime_running': True})
        manager.status = mock.Mock(return_value={'running': False})
        manager._uninstall_legacy = mock.Mock()

        with mock.patch.object(
                routing.proxy_guard, 'refresh_user_proxy_settings',
                return_value={'ok': True}) as refresh:
            result = manager.apply({
                'enabled': False, 'default_outbound': 'physical'})

        self.assertTrue(result['ok'])
        manager._native_service.stop_runtime.assert_called_once()
        manager._uninstall_legacy.assert_not_called()
        refresh.assert_called_once_with(timeout_ms=1000)

    def test_mihomo_error_redacts_urls_and_secrets(self):
        detail = routing._sanitize_mihomo_error(
            'GET https://example.test/sub?token=abc failed; Authorization: Bearer top-secret')

        self.assertNotIn('example.test', detail)
        self.assertNotIn('top-secret', detail)
        self.assertIn('已隐藏 URL', detail)

    def test_import_does_not_replace_cache_before_preview_succeeds(self):
        manager = routing.RoutingManager()
        manager._test_config = mock.Mock()
        manager.preview_proxy_provider = mock.Mock(
            side_effect=routing.RoutingError('解析失败'))

        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(subscription_store, 'persist_bytes') as persist:
            with self.assertRaisesRegex(routing.RoutingError, '解析失败'):
                manager.import_proxy_provider(provider(), 'proxies: []')

        persist.assert_not_called()

    def test_auto_update_failure_keeps_retry_state_and_is_not_logged_success(self):
        config = routing.normalize_config({
            'enabled': False,
            'default_outbound': 'physical',
            'proxy_providers': [{
                **provider(), 'auto_update': True, 'interval': 300,
            }],
        })
        manager = mock.Mock()
        manager.preview_proxy_provider.return_value = {
            'ok': True, 'refreshed': False,
            'warning': '在线更新失败，已继续使用上次成功缓存',
        }
        logs = []
        worker = RoutingUpdateWorker(
            lambda: {'routing': config}, manager, threading.Lock(), logs.append)
        with mock.patch.object(
                subscription_store, 'cache_status',
                return_value={'available': True, 'updated_at': 1}), \
                mock.patch('core.routing_updates.time.time', return_value=1000):
            worker._update_one_due_provider()

        self.assertTrue(any('自动更新未完成' in item for item in logs))
        self.assertFalse(any(item.endswith('自动更新完成') for item in logs))

    def test_auto_update_prefers_running_standby_core(self):
        config = routing.normalize_config({
            'enabled': False,
            'default_outbound': 'physical',
            'proxy_providers': [{
                **provider(), 'auto_update': True, 'interval': 300,
            }],
        })
        manager = mock.Mock()
        manager.status.return_value = {
            'core_running': True, 'standby': True}
        manager.refresh_proxy_provider.return_value = {
            'ok': True, 'refreshed': True}
        logs = []
        worker = RoutingUpdateWorker(
            lambda: {'routing': config}, manager, threading.Lock(), logs.append)
        with mock.patch.object(
                subscription_store, 'cache_status',
                return_value={'available': True, 'updated_at': 1}), \
                mock.patch('core.routing_updates.time.time', return_value=1000):
            worker._update_one_due_provider()

        manager.refresh_proxy_provider.assert_called_once()
        manager.preview_proxy_provider.assert_not_called()
        self.assertTrue(any('待机核心' in item for item in logs))

    def test_native_install_uses_transaction_and_commits_after_readiness(self):
        manager = routing.RoutingManager()
        config = routing.normalize_config({
            'enabled': True,
            'default_outbound': 'physical',
            'controller_secret': 'controller-secret',
        })
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {
            'transaction_id': 'transaction-1'}
        manager._wait_native_ready = mock.Mock()
        manager._refresh_user_proxy_settings = mock.Mock(return_value=True)
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'config.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({'mode': 'rule'}, stream)
            with mock.patch.object(
                    routing, 'windows_manual_proxy_state',
                    return_value={'enabled': False, 'server': ''}):
                manager._install(path, config)

        manager._native_service.ensure_installed.assert_called_once()
        manager._native_service.apply.assert_called_once()
        manager._wait_native_ready.assert_called_once_with(config, 'active')
        manager._native_service.commit.assert_called_once_with('transaction-1')
        manager._native_service.rollback.assert_not_called()

    def test_native_disable_can_defer_standby_after_capture_is_stopped(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._service_state = mock.Mock(return_value={
            'installed': True, 'state': 'Running', 'backend': 'native',
            'runtime_running': True, 'runtime_mode': 'standby',
        })
        manager.status = mock.Mock(return_value={
            'running': False, 'core_running': True, 'standby': True})
        manager._start_standby_runtime = mock.Mock()

        result = manager.apply({
            'enabled': False,
            'default_outbound': 'proxy',
            'proxy_provider_url': provider()['url'],
        }, defer_standby=True)

        self.assertEqual(result['msg'], '代理已关闭，节点核心正在后台启动')
        self.assertTrue(result['standby_pending'])
        manager._native_service.stop_runtime.assert_called_once()
        manager._start_standby_runtime.assert_not_called()

    def test_standby_failure_keeps_capture_closed_and_returns_warning(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._service_state = mock.Mock(return_value={
            'installed': True, 'state': 'Running', 'backend': 'native',
            'runtime_running': False, 'runtime_mode': 'stopped',
        })
        manager.status = mock.Mock(return_value={
            'running': False, 'core_running': False, 'standby': False})
        manager._start_standby_runtime = mock.Mock(
            side_effect=routing.RoutingError('端口被占用'))

        result = manager.apply({
            'enabled': False,
            'default_outbound': 'proxy',
            'proxy_provider_url': provider()['url'],
        })

        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])
        manager._native_service.stop_runtime.assert_called_once()

    def test_controller_requests_force_direct_loopback_access(self):
        manager = routing.RoutingManager()
        config = routing.normalize_config({
            'controller_secret': 'controller-secret',
        })
        response = mock.MagicMock()
        response.read.return_value = b'{"version":"1.2.3"}'
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        direct_handler = object()

        with mock.patch.object(
                routing.urllib.request, 'ProxyHandler',
                return_value=direct_handler) as proxy_handler, \
                mock.patch.object(
                    routing.urllib.request, 'build_opener',
                    return_value=opener) as build_opener, \
                mock.patch.object(routing.urllib.request, 'urlopen') as urlopen:
            result = manager._controller_request(config, '/version')

        self.assertEqual(result, {'version': '1.2.3'})
        proxy_handler.assert_called_once_with({})
        build_opener.assert_called_once_with(direct_handler)
        opener.open.assert_called_once()
        urlopen.assert_not_called()

    def test_fast_disable_keeps_ready_core_and_skips_standby_rebuild(self):
        manager = routing.RoutingManager()
        manager._service_state = mock.Mock(return_value={
            'installed': True, 'state': 'Running', 'backend': 'native',
            'runtime_running': True, 'runtime_mode': 'active',
            'fast_toggle_ready': True, 'crash_fused': False,
        })
        manager._fast_toggle_system_proxy = mock.Mock(return_value={
            'ok': True, 'msg': '代理已关闭',
            'config': routing.normalize_config({'enabled': False}),
            'warnings': [], 'status': {}, 'standby_pending': False,
        })
        manager._native_service = mock.Mock()
        manager._start_standby_runtime = mock.Mock()

        result = manager.apply(
            {'enabled': False, 'schema_version': 7,
             'capture_mode': 'system-proxy'}, defer_standby=True,
            allow_fast_toggle=True)

        self.assertTrue(result['ok'])
        manager._fast_toggle_system_proxy.assert_called_once()
        manager._native_service.stop_runtime.assert_not_called()
        manager._start_standby_runtime.assert_not_called()

    def test_fast_enable_probes_loaded_core_before_system_proxy_write(self):
        manager = routing.RoutingManager()
        config = routing.normalize_config({
            'enabled': True,
            'capture_mode': 'system-proxy',
            'mixed_port': 17890,
        })
        manager._native_service = mock.Mock()
        manager._verify_runtime_egress = mock.Mock()
        manager.status = mock.Mock(return_value={'running': True})

        with mock.patch.object(
                routing, 'windows_system_proxy',
                return_value='http://127.0.0.1:17890'):
            result = manager._fast_toggle_system_proxy(config, True)

        self.assertTrue(result['ok'])
        manager._verify_runtime_egress.assert_called_once_with(config)
        manager._native_service.set_system_proxy_enabled.assert_called_once_with(
            True)

    def test_fast_toggle_requires_ready_native_system_proxy_core(self):
        config = routing.normalize_config({
            'capture_mode': 'system-proxy',
        })
        ready = {
            'installed': True, 'backend': 'native', 'runtime_running': True,
            'runtime_mode': 'standby', 'fast_toggle_ready': True,
            'crash_fused': False,
        }

        self.assertTrue(
            routing.RoutingManager._can_fast_toggle_system_proxy(config, ready))
        self.assertFalse(
            routing.RoutingManager._can_fast_toggle_system_proxy(
                config, {**ready, 'fast_toggle_ready': False}))
        self.assertFalse(
            routing.RoutingManager._can_fast_toggle_system_proxy(
                {**config, 'capture_mode': 'tun'}, ready))

    def test_native_install_rolls_back_when_manual_node_is_not_ready(self):
        manager = routing.RoutingManager()
        config = routing.normalize_config({
            'enabled': True,
            'default_outbound': 'proxy:alpha',
            'controller_secret': 'controller-secret',
            'proxy_providers': [{
                **provider(), 'selection_mode': 'manual',
                'selected_node': 'JP-01',
            }],
        })
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {
            'transaction_id': 'transaction-2'}
        manager._wait_native_ready = mock.Mock(side_effect=routing.RoutingError(
            '手动选择的代理节点未能加载'))
        manager._refresh_user_proxy_settings = mock.Mock(return_value=True)
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'config.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({'mode': 'rule'}, stream)
            with mock.patch.object(
                    routing, 'windows_manual_proxy_state',
                    return_value={'enabled': False, 'server': ''}), \
                    self.assertRaisesRegex(routing.RoutingError, '手动选择'):
                manager._install(path, config)

        manager._native_service.rollback.assert_called_once_with('transaction-2')
        manager._native_service.commit.assert_not_called()

    def test_diagnostic_unicode_logging_failure_cannot_block_rollback(self):
        flag = '\U0001f1ef\U0001f1f5'

        def logger(message):
            if flag in message:
                raise UnicodeEncodeError('gbk', flag, 0, 1, 'illegal sequence')

        manager = routing.RoutingManager(logger)
        config = routing.normalize_config({
            'enabled': True,
            'default_outbound': 'physical',
            'controller_secret': 'controller-secret',
        })
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {
            'transaction_id': 'transaction-unicode'}
        manager._native_service.diagnostics.return_value = {
            'mihomo_log': [f'当前节点 {flag} 日本东京']}
        manager._wait_native_ready = mock.Mock(side_effect=routing.RoutingError(
            'TUN 公网自检失败'))
        manager._refresh_user_proxy_settings = mock.Mock(return_value=True)

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'config.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({'mode': 'rule'}, stream)
            with mock.patch.object(
                    routing, 'windows_manual_proxy_state',
                    return_value={'enabled': False, 'server': ''}), \
                    self.assertRaisesRegex(routing.RoutingError, '公网自检'):
                manager._install(path, config)

        manager._native_service.rollback.assert_called_once_with(
            'transaction-unicode')
        manager._native_service.commit.assert_not_called()

    def test_runtime_egress_confirms_selected_node_and_tun_connectivity(self):
        manager = routing.RoutingManager()
        config = routing.normalize_config({
            'enabled': True,
            'default_outbound': 'proxy:alpha',
            'controller_secret': 'controller-secret',
            'proxy_providers': [{**provider(), 'selection_mode': 'manual',
                                 'selected_node': 'JP-01'}],
        })
        runtime_node = '[订阅一] JP-01'
        group_payload = {'now': runtime_node, 'all': [runtime_node]}
        response = mock.MagicMock()
        response.status = 204
        response.read.return_value = b''
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response

        def controller(_config, path, **_kwargs):
            if path == '/proxies/PROXY-alpha':
                return group_payload
            raise AssertionError(path)

        manager._controller_request = mock.Mock(side_effect=controller)
        with mock.patch.object(
                routing.urllib.request, 'build_opener', return_value=opener):
            manager._verify_runtime_egress(config)

        manager._controller_request.assert_called_once_with(
            config, '/proxies/PROXY-alpha', timeout=5)
        self.assertLessEqual(opener.open.call_count, 2)
        self.assertGreaterEqual(opener.open.call_count, 1)

    def test_startup_does_not_repeat_node_speedtest_before_end_to_end_probe(self):
        manager = routing.RoutingManager()
        config = routing.normalize_config({
            'enabled': True,
            'default_outbound': 'proxy:alpha',
            'controller_secret': 'controller-secret',
            'proxy_providers': [{**provider(), 'selection_mode': 'manual',
                                 'selected_node': 'JP-01'}],
        })
        runtime_node = '[订阅一] JP-01'
        manager._controller_request = mock.Mock(return_value={
            'now': runtime_node, 'all': [runtime_node]})
        response = mock.MagicMock()
        response.status = 204
        response.read.return_value = b''
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response

        with mock.patch.object(
                routing.urllib.request, 'build_opener', return_value=opener):
            manager._verify_runtime_egress(config)

        paths = [call.args[1]
                 for call in manager._controller_request.call_args_list]
        self.assertEqual(paths, ['/proxies/PROXY-alpha'])
        self.assertFalse(any('healthcheck' in path for path in paths))

    def test_tun_connectivity_failure_is_actionable(self):
        manager = routing.RoutingManager()
        config = routing.normalize_config({
            'enabled': True, 'default_outbound': 'physical'})
        opener = mock.Mock()
        opener.open.side_effect = OSError('dns failed')

        with mock.patch.object(
                routing.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(
                    routing.RoutingError, '已恢复 Windows 原始路由.*请检查'):
                manager._verify_runtime_egress(config)

    def test_elevated_child_error_is_returned_instead_of_generic_uac_hint(self):
        with tempfile.TemporaryDirectory() as root:
            result_path = os.path.join(root, 'elevated.result')
            result_file = mock.Mock()
            result_file.name = result_path

            def run(*_args, **_kwargs):
                with open(result_path, 'w', encoding='utf-8') as stream:
                    stream.write(
                        'ERROR:Selected manual proxy node did not become ready')
                return subprocess.CompletedProcess([], 1, '', '')

            with mock.patch.object(
                    routing.tempfile, 'NamedTemporaryFile',
                    return_value=result_file), \
                    mock.patch.object(routing.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(
                        routing.RoutingError,
                        '手动选择的代理节点未能加载'):
                    routing._run_elevated("throw 'failure'")

            result_file.close.assert_called_once()
            self.assertFalse(os.path.exists(result_path))

    def test_elevated_script_is_compressed_below_windows_command_limit(self):
        with tempfile.TemporaryDirectory() as root:
            result_path = os.path.join(root, 'elevated.result')
            result_file = mock.Mock()
            result_file.name = result_path
            completed = subprocess.CompletedProcess([], 0, '', '')
            long_script = "Write-Output '重复内容'\n" * 5000

            def run(command, **_kwargs):
                self.assertLess(len(command[-1]), 30000)
                with open(result_path, 'w', encoding='utf-8') as stream:
                    stream.write('OK')
                return completed

            with mock.patch.object(
                    routing.tempfile, 'NamedTemporaryFile',
                    return_value=result_file), \
                    mock.patch.object(routing.subprocess, 'run', side_effect=run):
                routing._run_elevated(long_script)

            self.assertFalse(os.path.exists(result_path))


class SubscriptionStoreReliabilityTests(unittest.TestCase):
    def test_node_snapshot_round_trip_only_persists_safe_fields(self):
        item = provider()
        nodes = snapshot_nodes()
        nodes[0].update({
            'server': 'private.example.test',
            'port': 443,
            'uuid': 'sensitive-uuid',
            'subscription_url': item['url'],
        })
        nodes.append({
            'name': 'US-01',
            'display_name': '美国 01',
            'type': 'ss',
        })
        with tempfile.TemporaryDirectory() as root:
            saved = subscription_store.persist_node_snapshot(
                item, nodes, root=root, updated_at=1_800_000_001)
            loaded = subscription_store.load_node_snapshot(item, root=root)
            path = subscription_store.node_snapshot_path(item, root)
            with open(path, encoding='utf-8') as stream:
                raw = stream.read()

        self.assertEqual(loaded, saved)
        self.assertTrue(path.endswith('.yaml.nodes.json'))
        self.assertEqual(set(loaded), {
            'version', 'url_fingerprint', 'updated_at', 'nodes'})
        self.assertEqual(set(loaded['nodes'][0]), {
            'name', 'display_name', 'type', 'delay', 'alive', 'tested',
            'tested_at'})
        self.assertNotIn('private.example.test', raw)
        self.assertNotIn('sensitive-uuid', raw)
        self.assertNotIn(item['url'], raw)
        self.assertEqual(loaded['nodes'][1]['delay'], None)
        self.assertEqual(loaded['nodes'][1]['alive'], None)
        self.assertFalse(loaded['nodes'][1]['tested'])
        self.assertEqual(loaded['nodes'][1]['tested_at'], 0)

    def test_node_snapshot_is_isolated_by_url_fingerprint(self):
        original = provider()
        changed = dict(original, url='https://other.example.test/sub')
        with tempfile.TemporaryDirectory() as root:
            subscription_store.persist_node_snapshot(
                original, snapshot_nodes(), root=root,
                updated_at=1_800_000_001)
            with open(subscription_store.node_snapshot_path(original, root), 'rb') as stream:
                payload = stream.read()
            changed_path = subscription_store.node_snapshot_path(changed, root)
            with open(changed_path, 'wb') as stream:
                stream.write(payload)

            loaded = subscription_store.load_node_snapshot(changed, root=root)

        self.assertEqual(loaded['url_fingerprint'],
                         subscription_store.node_snapshot_fingerprint(changed))
        self.assertEqual(loaded['updated_at'], 0)
        self.assertEqual(loaded['nodes'], [])

    def test_corrupt_node_snapshots_return_safe_empty_snapshot(self):
        item = provider()
        fingerprint = subscription_store.node_snapshot_fingerprint(item)
        valid_node = snapshot_nodes()[0]
        base = {
            'version': subscription_store.NODE_SNAPSHOT_VERSION,
            'url_fingerprint': fingerprint,
            'updated_at': 1_800_000_001,
            'nodes': [valid_node],
        }
        corrupt_values = {
            'invalid-json': b'{',
            'wrong-version': dict(base, version='1'),
            'wrong-fingerprint': dict(base, url_fingerprint='0' * 16),
            'invalid-timestamp': dict(base, updated_at=10 ** 100),
            'too-many-nodes': dict(
                base, nodes=[valid_node] *
                (subscription_store.MAX_SNAPSHOT_NODE_COUNT + 1)),
            'extra-node-field': dict(
                base, nodes=[dict(valid_node, server='secret.example.test')]),
            'long-string': dict(base, nodes=[dict(
                valid_node,
                name='x' * (subscription_store.MAX_NODE_NAME_LENGTH + 1))]),
            'invalid-state-type': dict(
                base, nodes=[dict(valid_node, alive=1)]),
        }
        with tempfile.TemporaryDirectory() as root:
            path = subscription_store.node_snapshot_path(item, root)
            for label, value in corrupt_values.items():
                payload = value if isinstance(value, bytes) else json.dumps(
                    value, ensure_ascii=False).encode('utf-8')
                with self.subTest(label=label):
                    with open(path, 'wb') as stream:
                        stream.write(payload)
                    loaded = subscription_store.load_node_snapshot(
                        item, root=root)
                    self.assertEqual(loaded['updated_at'], 0)
                    self.assertEqual(loaded['nodes'], [])

            with open(path, 'wb') as stream:
                stream.write(
                    b'x' * (subscription_store.MAX_NODE_SNAPSHOT_SIZE + 1))
            loaded = subscription_store.load_node_snapshot(item, root=root)
            self.assertEqual(loaded['nodes'], [])

    def test_node_snapshot_atomic_failure_keeps_previous_snapshot(self):
        item = provider()
        old_nodes = snapshot_nodes()
        new_nodes = [dict(old_nodes[0], delay=123)]
        with tempfile.TemporaryDirectory() as root:
            subscription_store.persist_node_snapshot(
                item, old_nodes, root=root, updated_at=1_800_000_001)
            path = subscription_store.node_snapshot_path(item, root)
            original_replace = os.replace

            def replace(source, target):
                if (target == path and
                        os.path.basename(source).startswith(
                            'subscription.nodes.')):
                    raise OSError('snapshot locked')
                return original_replace(source, target)

            with mock.patch.object(
                    subscription_store.os, 'replace', side_effect=replace):
                with self.assertRaises(OSError):
                    subscription_store.persist_node_snapshot(
                        item, new_nodes, root=root,
                        updated_at=1_800_000_002)

            loaded = subscription_store.load_node_snapshot(item, root=root)

        self.assertEqual(loaded['updated_at'], 1_800_000_001)
        self.assertEqual(loaded['nodes'], old_nodes)

    def test_prune_cache_removes_stale_node_snapshot(self):
        current = provider()
        stale = dict(current, id='stale', url='https://stale.example.test/sub')
        with tempfile.TemporaryDirectory() as root:
            subscription_store.persist_node_snapshot(
                current, snapshot_nodes(), root=root,
                updated_at=1_800_000_001)
            subscription_store.persist_node_snapshot(
                stale, snapshot_nodes(), root=root,
                updated_at=1_800_000_001)
            current_path = subscription_store.node_snapshot_path(current, root)
            stale_path = subscription_store.node_snapshot_path(stale, root)

            removed = subscription_store.prune_cache([current], root=root)

            self.assertTrue(os.path.isfile(current_path))
            self.assertFalse(os.path.exists(stale_path))
            self.assertEqual(removed, 1)

    def test_node_snapshot_is_invalidated_when_filters_change(self):
        item = provider()
        filtered = dict(item, filter='香港', exclude_filter='过期')
        changed = dict(item, filter='日本', exclude_filter='')
        with tempfile.TemporaryDirectory() as root:
            subscription_store.persist_node_snapshot(
                filtered, snapshot_nodes(), root=root,
                updated_at=1_800_000_001)

            same = subscription_store.load_node_snapshot(filtered, root=root)
            stale = subscription_store.load_node_snapshot(changed, root=root)

        self.assertEqual(len(same['nodes']), 1)
        self.assertEqual(stale['nodes'], [])
        self.assertNotEqual(
            subscription_store.node_snapshot_fingerprint(filtered),
            subscription_store.node_snapshot_fingerprint(changed))

    def test_corrupt_metadata_is_bounded_and_does_not_break_status(self):
        item = provider()
        with tempfile.TemporaryDirectory() as root:
            path = subscription_store.cache_path(item, root)
            os.makedirs(root, exist_ok=True)
            with open(path, 'wb') as stream:
                stream.write(b'proxies: []')
            with open(path + '.json', 'w', encoding='utf-8') as stream:
                json.dump({
                    'url_fingerprint': subscription_store.url_fingerprint(item),
                    'node_count': 'not-an-int',
                    'updated_at': 10 ** 100,
                }, stream)

            status = subscription_store.cache_status(item, root)

        self.assertTrue(status['available'])
        self.assertEqual(status['node_count'], 0)
        self.assertGreaterEqual(status['updated_at'], 0)

    def test_pair_commit_failure_restores_previous_body_and_metadata(self):
        item = provider()
        with tempfile.TemporaryDirectory() as root:
            body = subscription_store.cache_path(item, root)
            metadata = subscription_store.metadata_path(item, root)
            os.makedirs(root, exist_ok=True)
            with open(body, 'wb') as stream:
                stream.write(b'old-body')
            with open(metadata, 'wb') as stream:
                stream.write(b'old-metadata')
            original_replace = os.replace
            failed = False

            def replace(source, target):
                nonlocal failed
                if (not failed and target == metadata and
                        os.path.basename(source).startswith('subscription.metadata.')):
                    failed = True
                    raise OSError('metadata locked')
                return original_replace(source, target)

            with mock.patch.object(subscription_store.os, 'replace', side_effect=replace):
                with self.assertRaises(OSError):
                    subscription_store.persist_bytes(
                        item, b'new-body', 2, '测试', root=root)

            with open(body, 'rb') as stream:
                self.assertEqual(stream.read(), b'old-body')
            with open(metadata, 'rb') as stream:
                self.assertEqual(stream.read(), b'old-metadata')

    def test_bundle_commit_failure_restores_body_metadata_and_nodes(self):
        item = provider()
        with tempfile.TemporaryDirectory() as root:
            subscription_store.persist_bytes_and_nodes(
                item, b'old-body', 1, '旧缓存', snapshot_nodes(), root=root)
            paths = [
                subscription_store.cache_path(item, root),
                subscription_store.metadata_path(item, root),
                subscription_store.node_snapshot_path(item, root),
            ]
            before = {}
            for path in paths:
                with open(path, 'rb') as stream:
                    before[path] = stream.read()
            original_replace = os.replace
            failed = False

            def replace(source, target):
                nonlocal failed
                if (not failed and target == paths[2] and
                        os.path.basename(source).startswith('subscription.nodes.')):
                    failed = True
                    raise OSError('nodes locked')
                return original_replace(source, target)

            with mock.patch.object(
                    subscription_store.os, 'replace', side_effect=replace):
                with self.assertRaises(OSError):
                    subscription_store.persist_bytes_and_nodes(
                        item, b'new-body', 1, '新缓存', [{
                            **snapshot_nodes()[0], 'name': 'NEW',
                            'display_name': '新节点',
                        }], root=root)

            for path in paths:
                with open(path, 'rb') as stream:
                    self.assertEqual(stream.read(), before[path])


if __name__ == '__main__':
    unittest.main()
