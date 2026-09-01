# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest import mock

from core import routing, subscription_store


def vpn_profile(name='公司 VPN', connected=True, gateway=False):
    return {
        'name': name,
        'server': 'vpn.example.test',
        'status': 'Connected' if connected else 'Disconnected',
        'ipv4_default_gateway': gateway,
        'ipv6_default_gateway': gateway,
    }


class RoutingConfigTests(unittest.TestCase):
    def base_config(self):
        return {
            'enabled': True,
            'physical_interface': '以太网',
            'proxy_provider_url': 'https://example.test/subscription',
            'default_outbound': 'proxy',
            'controller_secret': 'test-secret',
            'rules': [
                {'match_type': 'exact', 'domain': 'mh.chaoxing.com',
                 'outbound': 'vpn:公司 VPN'},
                {'match_type': 'suffix', 'domain': '*.chaoxing.com',
                 'outbound': 'physical'},
                {'match_type': 'wildcard', 'domain': '*.internal.*',
                 'outbound': 'block'},
            ],
        }

    def test_normalizes_suffix_and_preserves_rule_order(self):
        config = routing.normalize_config(self.base_config())

        self.assertEqual(config['rules'][1]['domain'], 'chaoxing.com')
        self.assertEqual(config['proxy_providers'][0]['id'], 'default')
        self.assertEqual(config['proxy_providers'][0]['name'], '默认订阅')
        self.assertEqual(
            [item['match_type'] for item in config['rules']],
            ['exact', 'suffix', 'wildcard'])

    def test_rejects_path_rules(self):
        config = self.base_config()
        config['rules'][0]['domain'] = 'mh.chaoxing.com/api/login'

        with self.assertRaisesRegex(routing.RoutingError, '不能包含协议、端口或路径'):
            routing.normalize_config(config)

    def test_generates_bound_direct_outbounds_and_ordered_rules(self):
        config = routing.normalize_config(self.base_config())

        generated = routing.build_mihomo_config(
            config, [vpn_profile()], ['203.0.113.8/32'])

        proxies = {item['name']: item for item in generated['proxies']}
        self.assertEqual(proxies['PHYSICAL']['interface-name'], '以太网')
        self.assertEqual(proxies['VPN-1']['interface-name'], '公司 VPN')
        self.assertEqual(generated['rules'], [
            'DOMAIN,mh.chaoxing.com,VPN-1',
            'DOMAIN-SUFFIX,chaoxing.com,PHYSICAL',
            'DOMAIN-WILDCARD,*.internal.*,REJECT',
            'MATCH,PROXY',
        ])
        self.assertEqual(
            generated['tun']['route-exclude-address'], ['203.0.113.8/32'])
        self.assertTrue(generated['tun']['auto-detect-interface'])
        self.assertTrue(generated['tun']['strict-route'])
        self.assertEqual(generated['external-controller'], '127.0.0.1:19090')
        self.assertNotIn('external-controller-cors', generated)
        self.assertFalse(generated['allow-lan'])
        primary_group = next(
            item for item in generated['proxy-groups']
            if item['name'] == 'PROXY')
        self.assertEqual(primary_group['type'], 'url-test')
        self.assertEqual(
            generated['proxy-providers']['provider-default']['override'],
            {'additional-prefix': '[默认订阅] ', 'interface-name': '以太网'})
        self.assertEqual(
            generated['proxy-providers']['provider-default']['proxy'],
            'SUBSCRIPTION-UPDATE-default')
        self.assertNotIn('interface-name', generated)
        self.assertEqual(
            generated['proxy-providers']['provider-default']['header'],
            {'User-Agent': ['Clash-Verge']})

    def test_standby_config_keeps_loopback_core_without_system_capture(self):
        config = routing.normalize_config(self.base_config())

        generated = routing.build_mihomo_config(
            config, [vpn_profile()], standby=True)

        self.assertFalse(generated['tun']['enable'])
        self.assertFalse(generated['allow-lan'])
        self.assertEqual(generated['bind-address'], '127.0.0.1')
        self.assertEqual(generated['mixed-port'], config['mixed_port'])
        self.assertEqual(generated['rules'], ['MATCH,PROXY'])
        self.assertNotIn('VPN-1', {item['name'] for item in generated['proxies']})
        self.assertEqual(
            generated['proxy-providers']['provider-default']['size-limit'],
            10 * 1024 * 1024)

    def test_global_mode_preserves_but_skips_domain_rules(self):
        source = self.base_config()
        source['traffic_mode'] = 'global'
        config = routing.normalize_config(source)

        generated = routing.build_mihomo_config(config, [vpn_profile()])
        explained = routing.explain_domain(
            config, 'mh.chaoxing.com', [vpn_profile()])

        self.assertEqual(config['traffic_mode'], 'global')
        self.assertEqual(len(config['rules']), 3)
        self.assertEqual(generated['rules'], ['MATCH,PROXY'])
        self.assertFalse(explained['matched'])
        self.assertEqual(explained['outbound'], 'proxy')

    def test_rejects_unknown_traffic_mode(self):
        source = self.base_config()
        source['traffic_mode'] = 'invalid'

        with self.assertRaisesRegex(routing.RoutingError, '连接模式'):
            routing.normalize_config(source)

    def test_generates_multiple_providers_and_specific_outbound(self):
        config = self.base_config()
        config.pop('proxy_provider_url')
        config['proxy_strategy'] = 'fallback'
        config['proxy_providers'] = [
            {'id': 'alpha', 'name': '机场一', 'url': 'https://a.example/sub',
             'enabled': True, 'strategy': 'url-test', 'interval': 900,
             'filter': '香港|HK', 'exclude_filter': '过期'},
            {'id': 'beta', 'name': '机场二', 'url': 'https://b.example/sub',
             'enabled': True, 'strategy': 'select', 'interval': 3600},
        ]
        config['rules'][0]['outbound'] = 'proxy:beta'

        generated = routing.build_mihomo_config(
            routing.normalize_config(config), [vpn_profile()])

        groups = {item['name']: item for item in generated['proxy-groups']}
        self.assertEqual(groups['PROXY']['type'], 'fallback')
        self.assertEqual(groups['PROXY-beta']['type'], 'select')
        self.assertNotIn('url', groups['PROXY-beta'])
        self.assertEqual(generated['rules'][0],
                         'DOMAIN,mh.chaoxing.com,PROXY-beta')
        self.assertEqual(
            generated['proxy-providers']['provider-alpha']['filter'],
            '香港|HK')
        self.assertEqual(
            generated['proxy-providers']['provider-alpha']['exclude-filter'],
            '过期')

    def test_auto_download_does_not_depend_on_windows_system_proxy(self):
        config = routing.normalize_config(self.base_config())

        generated = routing.build_mihomo_config(
            config, [vpn_profile()], system_proxy='http://127.0.0.1:7897')

        proxies = {item['name']: item for item in generated['proxies']}
        groups = {item['name']: item for item in generated['proxy-groups']}
        self.assertNotIn('SUBSCRIPTION-UPSTREAM-default', proxies)
        self.assertEqual(
            groups['SUBSCRIPTION-UPDATE-default']['use'],
            ['provider-default'])
        self.assertEqual(
            groups['SUBSCRIPTION-UPDATE-default']['empty-fallback'],
            'PHYSICAL')
        self.assertEqual(
            generated['proxy-providers']['provider-default']['proxy'],
            'SUBSCRIPTION-UPDATE-default')

    def test_auto_download_self_bootstraps_from_same_provider_cache(self):
        config = routing.normalize_config(self.base_config())

        generated = routing.build_mihomo_config(
            config, [vpn_profile()], system_proxy='')

        groups = {item['name']: item for item in generated['proxy-groups']}
        update_group = groups['SUBSCRIPTION-UPDATE-default']
        self.assertEqual(update_group['use'], ['provider-default'])
        self.assertEqual(update_group['empty-fallback'], 'PHYSICAL')
        self.assertEqual(
            generated['proxy-providers']['provider-default']['proxy'],
            'SUBSCRIPTION-UPDATE-default')

    def test_auto_download_without_cache_falls_back_to_physical(self):
        config = routing.normalize_config(self.base_config())

        generated = routing.build_mihomo_config(
            config, [vpn_profile()], system_proxy='')

        update_group = next(
            item for item in generated['proxy-groups']
            if item['name'] == 'SUBSCRIPTION-UPDATE-default')
        self.assertEqual(update_group['use'], ['provider-default'])
        self.assertEqual(update_group['empty-fallback'], 'PHYSICAL')

    def test_custom_download_proxy_must_be_local_http(self):
        for proxy in ('', 'https://127.0.0.1:7897',
                      'http://proxy.example.test:7897',
                      'http://user:pass@127.0.0.1:7897'):
            with self.subTest(proxy=proxy):
                config = self.base_config()
                config['proxy_providers'] = [{
                    'id': 'alpha', 'name': '机场一',
                    'url': 'https://a.example/sub',
                    'download_route': 'custom-proxy',
                    'download_proxy': proxy,
                }]
                config.pop('proxy_provider_url')
                with self.assertRaises(routing.RoutingError):
                    routing.normalize_config(config)

    def test_explicit_system_proxy_requires_detected_proxy(self):
        config = self.base_config()
        config['proxy_providers'] = [{
            'id': 'alpha', 'name': '机场一',
            'url': 'https://a.example/sub',
            'download_route': 'system-proxy',
        }]
        config.pop('proxy_provider_url')
        normalized = routing.normalize_config(config)

        with self.assertRaisesRegex(routing.RoutingError, '未检测到'):
            routing.build_mihomo_config(normalized, [vpn_profile()])

    def test_rejects_disabled_provider_reference(self):
        config = self.base_config()
        config.pop('proxy_provider_url')
        config['proxy_providers'] = [{
            'id': 'alpha', 'name': '机场一', 'url': 'https://a.example/sub',
            'enabled': False,
        }]
        config['default_outbound'] = 'proxy:alpha'

        with self.assertRaisesRegex(routing.RoutingError, '未启用'):
            routing.normalize_config(config)

    def test_rejects_duplicate_provider_names(self):
        config = self.base_config()
        config.pop('proxy_provider_url')
        config['proxy_providers'] = [
            {'id': 'alpha', 'name': '机场', 'url': 'https://a.example/sub'},
            {'id': 'beta', 'name': '机场', 'url': 'https://b.example/sub'},
        ]

        with self.assertRaisesRegex(routing.RoutingError, '名称重复'):
            routing.normalize_config(config)

    def test_rejects_provider_name_control_characters(self):
        config = self.base_config()
        config.pop('proxy_provider_url')
        config['proxy_providers'] = [{
            'id': 'alpha', 'name': '订阅\n伪造日志',
            'url': 'https://a.example/sub',
        }]

        with self.assertRaisesRegex(routing.RoutingError, '控制字符'):
            routing.normalize_config(config)

    def test_reads_runtime_proxy_groups_and_latest_delay(self):
        config = routing.normalize_config(self.base_config())
        node_name = '[默认订阅] 香港 01'
        payload = {'proxies': {
            'PROXY': {'all': [node_name], 'now': node_name},
            'PROXY-default': {'all': [node_name], 'now': node_name},
            node_name: {'alive': True, 'history': [{'delay': 68}]},
        }}
        manager = routing.RoutingManager()

        with mock.patch.object(manager, '_controller_request',
                               return_value=payload):
            groups = manager.proxy_overview(config)

        provider = next(item for item in groups if item['id'] == 'default')
        self.assertEqual(provider['selected'], node_name)
        self.assertEqual(provider['nodes'][0]['display_name'], '香港 01')
        self.assertEqual(provider['nodes'][0]['delay'], 68)
        self.assertEqual(provider['alive_count'], 1)

    def test_explains_domain_with_ordered_match_and_default(self):
        config = self.base_config()

        exact = routing.explain_domain(config, 'MH.Chaoxing.com', [vpn_profile()])
        suffix = routing.explain_domain(config, 'mooc.chaoxing.com', [vpn_profile()])
        fallback = routing.explain_domain(config, 'example.org', [vpn_profile()])

        self.assertEqual(exact['rule_index'], 1)
        self.assertEqual(exact['outbound_name'], '公司 VPN')
        self.assertTrue(exact['available'])
        self.assertEqual(suffix['rule_index'], 2)
        self.assertEqual(suffix['outbound'], 'physical')
        self.assertFalse(fallback['matched'])
        self.assertEqual(fallback['outbound'], 'proxy')

    def test_refreshes_saved_provider_through_controller(self):
        config = routing.normalize_config(self.base_config())
        manager = routing.RoutingManager()
        node_name = '[默认订阅] 节点 A'
        overview = {'proxies': {
            'PROXY': {'all': [node_name], 'now': node_name},
            'PROXY-default': {'all': [node_name], 'now': node_name},
            node_name: {'type': 'Vless', 'alive': True, 'history': []},
        }}
        manager._native_service = mock.Mock()
        manager._native_service.read_provider.return_value = {
            'content': b'proxies: []', 'modified_at': 1_800_000_000}
        with mock.patch.object(
                manager, '_controller_request',
                side_effect=[{}, overview, {'providers': {}}]) as request, \
                mock.patch.object(
                    routing.subscription_store, 'persist_bytes',
                    return_value={'available': True}) as persist:
            result = manager.refresh_proxy_provider(config, 'default')

        self.assertTrue(result['ok'])
        self.assertEqual(
            request.call_args_list[0].args[1],
            '/providers/proxies/provider-default')
        self.assertEqual(request.call_args_list[0].kwargs['method'], 'PUT')
        self.assertEqual(result['update_state'], 'remote_updated')
        self.assertFalse(result['used_cache'])
        persist.assert_called_once()

    def test_refresh_provider_does_not_report_success_when_nodes_unconfirmed(self):
        config = routing.normalize_config(self.base_config())
        manager = routing.RoutingManager()
        with mock.patch.object(
                manager, '_controller_request',
                side_effect=[{}, {'proxies': {}}, {'providers': {}}]):
            with self.assertRaisesRegex(routing.RoutingError, '无法确认'):
                manager.refresh_proxy_provider(config, 'default')

    def test_previews_unsaved_provider_without_tun_or_proxy_listener(self):
        provider = {
            'id': 'draft', 'name': '未保存订阅',
            'url': 'https://example.test/subscription',
            'enabled': False, 'filter': '香港', 'exclude_filter': '过期',
            'download_route': 'physical',
        }
        manager = routing.RoutingManager()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        written = {}

        def capture_config(_path, value):
            written.update(value)

        def start_preview(*args, **_kwargs):
            data_dir = args[0][args[0].index('-d') + 1]
            provider_dir = os.path.join(data_dir, 'providers')
            os.makedirs(provider_dir, exist_ok=True)
            with open(os.path.join(provider_dir, 'preview.yaml'), 'w',
                      encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: 香港 01\n    type: vmess\n')
            return process

        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(routing, '_write_json', side_effect=capture_config), \
                mock.patch.object(routing.subprocess, 'Popen',
                                  side_effect=start_preview), \
                mock.patch.object(routing, '_attach_kill_on_close_job',
                                  return_value=mock.Mock()) as attach_job, \
                mock.patch.object(manager, '_controller_request', side_effect=[
                    {'version': 'v1.19.30'},
                    {'proxies': [{'name': '香港 01', 'type': 'vmess'}]},
                ]), \
                tempfile.TemporaryDirectory() as cache_root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': cache_root}):
            result = manager.preview_proxy_provider(provider, {
                'physical_interface': '以太网',
                'dns_servers': '223.5.5.5, 1.1.1.1',
            })

        self.assertTrue(result['ok'])
        self.assertEqual(result['node_count'], 1)
        self.assertEqual(result['nodes'][0]['name'], '香港 01')
        self.assertNotIn('tun', written)
        self.assertNotIn('mixed-port', written)
        self.assertFalse(written['allow-lan'])
        self.assertNotIn('interface-name', written)
        physical = next(item for item in written['proxies']
                        if item['name'] == 'PHYSICAL')
        self.assertEqual(physical['interface-name'], '以太网')
        self.assertEqual(
            written['dns']['nameserver'], ['223.5.5.5', '1.1.1.1'])
        self.assertFalse(
            written['proxy-providers']['preview']['health-check']['enable'])
        self.assertEqual(
            written['proxy-providers']['preview']['proxy'], 'PHYSICAL')
        attach_job.assert_called_once_with(process)
        process.terminate.assert_called_once()

    def test_preview_nodes_can_be_tested_without_tun_or_service(self):
        provider = {
            'id': 'draft', 'name': '未保存订阅',
            'url': 'https://example.test/subscription',
            'enabled': False, 'download_route': 'physical',
        }
        manager = routing.RoutingManager()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        written = {}

        def capture_config(_path, value):
            written.update(value)

        def stage_cache(_provider, destination):
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, 'w', encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: 节点 A\n    type: vmess\n'
                             '  - name: 节点 B\n    type: vless\n')
            return True

        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(routing, '_write_json', side_effect=capture_config), \
                mock.patch.object(routing.subprocess, 'Popen', return_value=process), \
                mock.patch.object(routing, '_attach_kill_on_close_job',
                                  return_value=mock.Mock()), \
                mock.patch.object(subscription_store, 'stage_cache',
                                  side_effect=stage_cache), \
                mock.patch.object(manager, '_controller_request', side_effect=[
                    {'version': 'v1.19.30'},
                    {'proxies': [
                        {'name': '节点 A', 'type': 'vmess'},
                        {'name': '节点 B', 'type': 'vless'},
                    ]},
                    {'delay': 123},
                    OSError('节点不可用'),
                ]) as request, \
                mock.patch.object(subscription_store, 'persist_bytes') as persist:
            result = manager.test_preview_proxy_provider(
                provider, {'physical_interface': '以太网'})

        self.assertTrue(result['tested'])
        self.assertEqual(result['nodes'][0]['delay'], 123)
        self.assertTrue(result['nodes'][0]['alive'])
        self.assertTrue(result['nodes'][0]['tested'])
        self.assertEqual(result['nodes'][1]['delay'], 0)
        self.assertFalse(result['nodes'][1]['alive'])
        self.assertTrue(result['nodes'][1]['tested'])
        self.assertNotIn('tun', written)
        self.assertNotIn('mixed-port', written)
        self.assertEqual(written['proxy-providers']['preview']['type'], 'file')
        self.assertNotIn('url', written['proxy-providers']['preview'])
        self.assertNotIn('proxy', written['proxy-providers']['preview'])
        self.assertIn('/proxies/', request.call_args_list[2].args[1])
        self.assertFalse(any(
            call.kwargs.get('method') == 'PUT'
            for call in request.call_args_list))
        persist.assert_not_called()
        process.terminate.assert_called_once()

    def test_auto_preview_falls_back_to_detected_windows_proxy(self):
        provider = {
            'id': 'draft', 'name': '科学',
            'url': 'https://example.test/subscription',
            'enabled': False, 'download_route': 'auto',
        }
        manager = routing.RoutingManager()
        expected = {
            'ok': True, 'provider_name': '科学', 'node_count': 1,
            'nodes': [{'name': '东京 01'}],
            'download_route': 'Windows 系统代理迁移',
        }
        with mock.patch.object(
                manager, '_preview_proxy_provider_once', side_effect=[
                    routing.ProviderFetchError('无法获取订阅'), expected,
                ]) as preview_once, mock.patch.object(
                    routing, 'windows_system_proxy',
                    return_value='http://127.0.0.1:7892'):
            result = manager.preview_proxy_provider(provider)

        self.assertEqual(preview_once.call_count, 2)
        retry_provider = preview_once.call_args_list[1].args[0]
        self.assertEqual(retry_provider['download_route'], 'system-proxy')
        self.assertEqual(result['auto_fallback'], 'system-proxy')
        self.assertIn('Windows 系统代理自动回退', result['download_route'])

    def test_preview_speedtest_does_not_use_network_fallback(self):
        provider = {
            'id': 'draft', 'name': '科学',
            'url': 'https://example.test/subscription',
            'enabled': False, 'download_route': 'auto',
        }
        manager = routing.RoutingManager()
        with mock.patch.object(
                manager, '_preview_proxy_provider_once',
                side_effect=routing.ProviderFetchError('缓存不可用')) as once, \
                mock.patch.object(routing, 'windows_system_proxy') as proxy:
            with self.assertRaises(routing.ProviderFetchError):
                manager.test_preview_proxy_provider(provider)

        once.assert_called_once()
        proxy.assert_not_called()

    def test_auto_preview_reports_all_routes_failed(self):
        provider = {
            'id': 'draft', 'name': '科学',
            'url': 'https://example.test/subscription',
            'enabled': False, 'download_route': 'auto',
        }
        manager = routing.RoutingManager()
        with mock.patch.object(
                manager, '_preview_proxy_provider_once', side_effect=[
                    routing.ProviderFetchError('物理网络失败'),
                    routing.RoutingError('系统代理已不可用'),
                ]), mock.patch.object(
                    routing, 'windows_system_proxy',
                    return_value='http://127.0.0.1:7892'):
            with self.assertRaisesRegex(
                    routing.RoutingError, 'Windows 系统代理均失败'):
                manager.preview_proxy_provider(provider)

    def test_preview_test_requires_matching_cache_before_process_start(self):
        provider = {
            'id': 'draft', 'name': '未保存订阅',
            'url': 'https://example.test/subscription',
            'enabled': False, 'download_route': 'physical',
        }
        manager = routing.RoutingManager()
        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(subscription_store, 'stage_cache',
                                  return_value=False), \
                mock.patch.object(routing.subprocess, 'Popen') as start:
            with self.assertRaisesRegex(routing.RoutingError, '请先获取节点'):
                manager.test_preview_proxy_provider(provider)

        start.assert_not_called()

    def test_preview_test_marks_malformed_node_result_unavailable(self):
        provider = {
            'id': 'draft', 'name': '未保存订阅',
            'url': 'https://example.test/subscription',
            'enabled': False, 'download_route': 'physical',
        }
        manager = routing.RoutingManager()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        def stage_cache(_provider, destination):
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, 'w', encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: 节点 A\n    type: vmess\n')
            return True

        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(subscription_store, 'stage_cache',
                                  side_effect=stage_cache), \
                mock.patch.object(routing.subprocess, 'Popen', return_value=process), \
                mock.patch.object(routing, '_attach_kill_on_close_job',
                                  return_value=mock.Mock()), \
                mock.patch.object(manager, '_controller_request', side_effect=[
                    {'version': 'v1.19.30'},
                    {'proxies': [{'name': '节点 A', 'type': 'vmess'}]},
                    [],
                ]):
            result = manager.test_preview_proxy_provider(provider)

        self.assertTrue(result['tested'])
        self.assertTrue(result['nodes'][0]['tested'])
        self.assertFalse(result['nodes'][0]['alive'])
        self.assertEqual(result['nodes'][0]['delay'], 0)

        process.terminate.assert_called_once()

    def test_successful_preview_persists_downloaded_provider_yaml(self):
        provider = {
            'id': 'draft', 'name': '未保存订阅',
            'url': 'https://example.test/subscription',
            'enabled': False, 'download_route': 'physical',
        }
        manager = routing.RoutingManager()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        def start_preview(*args, **_kwargs):
            data_dir = args[0][args[0].index('-d') + 1]
            provider_dir = os.path.join(data_dir, 'providers')
            os.makedirs(provider_dir, exist_ok=True)
            with open(os.path.join(provider_dir, 'preview.yaml'), 'w',
                      encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: Hong Kong 01\n    type: direct\n')
            return process

        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(routing.subprocess, 'Popen',
                                  side_effect=start_preview), \
                mock.patch.object(routing, '_attach_kill_on_close_job',
                                  return_value=mock.Mock()), \
                mock.patch.object(manager, '_controller_request', side_effect=[
                    {'version': 'v1.19.30'},
                    {'proxies': [{'name': 'Hong Kong 01', 'type': 'direct'}]},
                ]), \
                mock.patch.object(subscription_store, 'persist_bytes',
                                  wraps=subscription_store.persist_bytes) as persist, \
                tempfile.TemporaryDirectory() as cache_root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': cache_root}):
            result = manager.preview_proxy_provider(provider, {
                'physical_interface': '以太网',
            })
            normalized = routing.normalize_config({
                'proxy_providers': [provider],
            })['proxy_providers'][0]
            cached_path = subscription_store.cache_path(normalized)
            with open(cached_path, encoding='utf-8') as stream:
                cached_text = stream.read()

        self.assertEqual(result['node_count'], 1)
        self.assertIn('Hong Kong 01', cached_text)
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[0]['id'], 'draft')
        self.assertEqual(persist.call_args.args[2], 1)
        self.assertEqual(persist.call_args.args[3], '物理网络获取')

    def test_failed_preview_keeps_existing_provider_cache(self):
        provider = routing.normalize_config({
            'proxy_providers': [{
                'id': 'draft', 'name': '未保存订阅',
                'url': 'https://example.test/subscription',
                'download_route': 'physical',
            }],
        })['proxy_providers'][0]
        manager = routing.RoutingManager()
        process = mock.Mock()
        process.poll.side_effect = [None, 0, 0]
        process.wait.return_value = 0

        with tempfile.TemporaryDirectory() as cache_root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': cache_root}):
            source_path = os.path.join(cache_root, 'old.yaml')
            with open(source_path, 'w', encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: Cached Node\n    type: direct\n')
            subscription_store.persist_cache(
                provider, source_path, 1, 'preview')
            cached_path = subscription_store.cache_path(provider)
            before_status = subscription_store.cache_status(provider)
            with open(cached_path, 'rb') as stream:
                before = stream.read()

            with mock.patch.object(routing, 'verify_runtime'), \
                    mock.patch.object(routing.subprocess, 'Popen',
                                      return_value=process), \
                    mock.patch.object(routing, '_attach_kill_on_close_job',
                                      return_value=mock.Mock()), \
                    mock.patch.object(manager, '_controller_request', side_effect=[
                        {'version': 'v1.19.30'},
                        {'proxies': [{'name': 'Cached Node', 'type': 'direct'}]},
                        OSError('simulated update failure'),
                    ]):
                result = manager.preview_proxy_provider(provider, {
                    'physical_interface': '以太网',
                })

            with open(cached_path, 'rb') as stream:
                after = stream.read()
            after_status = subscription_store.cache_status(provider)
            self.assertEqual(after, before)
            self.assertEqual(
                after_status['updated_at'], before_status['updated_at'])
            self.assertIn('继续使用上次成功缓存', result['msg'])

    def test_provider_cache_filename_is_scoped_to_subscription_url(self):
        provider = {
            'id': 'alpha', 'url': 'https://example.test/subscription?token=one',
        }
        changed = dict(
            provider,
            url='https://example.test/subscription?token=two')

        first_name = subscription_store.provider_filename(provider)
        second_name = subscription_store.provider_filename(changed)

        self.assertTrue(first_name.startswith('alpha-'))
        self.assertTrue(first_name.endswith('.yaml'))
        self.assertNotEqual(first_name, second_name)
        self.assertNotIn('token', first_name)

    def test_changed_subscription_url_does_not_stage_old_cache(self):
        provider = {'id': 'alpha', 'url': 'https://example.test/one'}
        changed = {'id': 'alpha', 'url': 'https://example.test/two'}
        with tempfile.TemporaryDirectory() as root:
            source_path = os.path.join(root, 'source.yaml')
            target_path = os.path.join(root, 'staging', 'alpha.yaml')
            with open(source_path, 'w', encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: Cached Node\n    type: direct\n')
            subscription_store.persist_cache(
                provider, source_path, 1, 'preview', root=root)

            self.assertTrue(
                subscription_store.cache_status(provider, root)['available'])
            self.assertFalse(
                subscription_store.cache_status(changed, root)['available'])
            self.assertFalse(
                subscription_store.stage_cache(changed, target_path, root))
            self.assertFalse(os.path.exists(target_path))

    def test_provider_cache_atomic_update_failure_keeps_old_content(self):
        provider = {'id': 'alpha', 'url': 'https://example.test/sub'}
        with tempfile.TemporaryDirectory() as root:
            old_source = os.path.join(root, 'old.yaml')
            new_source = os.path.join(root, 'new.yaml')
            with open(old_source, 'w', encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: Old Node\n    type: direct\n')
            with open(new_source, 'w', encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: New Node\n    type: direct\n')
            subscription_store.persist_cache(
                provider, old_source, 1, 'preview', root=root)
            cached_path = subscription_store.cache_path(provider, root)
            with open(cached_path, 'rb') as stream:
                before = stream.read()

            with mock.patch.object(
                    subscription_store.os, 'replace',
                    side_effect=OSError('simulated replace failure')):
                with self.assertRaises(OSError):
                    subscription_store.persist_cache(
                        provider, new_source, 1, 'refresh', root=root)

            with open(cached_path, 'rb') as stream:
                after = stream.read()
            self.assertEqual(after, before)

    def test_install_stages_matching_provider_cache_for_service(self):
        config = routing.normalize_config(self.base_config())
        provider = config['proxy_providers'][0]
        manager = routing.RoutingManager()
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': root}):
            source_path = os.path.join(root, 'source.yaml')
            config_path = os.path.join(root, 'config.json')
            with open(source_path, 'w', encoding='utf-8') as stream:
                stream.write('proxies:\n  - name: Cached Node\n    type: direct\n')
            with open(config_path, 'w', encoding='utf-8') as stream:
                stream.write('{}')
            subscription_store.persist_cache(
                provider, source_path, 1, 'preview')
            expected_cache_path = os.path.realpath(
                subscription_store.cache_path(provider))

            manager._native_service = mock.Mock()
            manager._native_service.apply.return_value = {
                'transaction_id': 'cache-transaction'}
            with mock.patch.object(manager, '_wait_native_ready'):
                manager._install(config_path, config)

        staged = manager._native_service.apply.call_args.args[1]
        self.assertEqual(staged[0]['name'],
                         subscription_store.provider_filename(provider))
        self.assertEqual(staged[0]['path'],
                         expected_cache_path)

    def test_preview_error_does_not_echo_subscription_url(self):
        message = routing._provider_preview_error(
            'fetch https://secret.test/sub?token=value returned 403', False)

        self.assertIn('拒绝访问', message)
        self.assertNotIn('secret.test', message)

    def test_imported_yaml_is_validated_cached_and_previewed(self):
        provider = {
            'id': 'draft', 'name': '导入订阅',
            'url': 'https://example.test/subscription',
            'enabled': True,
        }
        manager = routing.RoutingManager()
        expected = {
            'ok': True, 'provider_name': '导入订阅',
            'node_count': 2, 'nodes': [],
        }
        with mock.patch.object(routing, 'verify_runtime'), \
                mock.patch.object(manager, '_test_config') as test_config, \
                mock.patch.object(
                    routing.subscription_store, 'persist_bytes_and_nodes',
                    return_value=({'available': True}, {})) as persist, \
                mock.patch.object(
                    manager, 'preview_proxy_provider', return_value=expected) as preview:
            result = manager.import_proxy_provider(
                provider, 'proxies:\n  - name: test\n    type: http\n'
                          '    server: 127.0.0.1\n    port: 8080\n',
                {'physical_interface': '以太网'})

        self.assertTrue(result['ok'])
        self.assertIn('2 个节点', result['msg'])
        test_config.assert_called_once()
        persist.assert_called_once()
        preview.assert_called_once()
        self.assertFalse(preview.call_args.kwargs['_persist_snapshot'])
        self.assertEqual(
            preview.call_args.args[0]['download_route'], 'auto')

    def test_imported_yaml_rejects_oversized_content(self):
        manager = routing.RoutingManager()
        provider = {
            'id': 'draft', 'name': '导入订阅',
            'url': 'https://example.test/subscription',
        }
        with self.assertRaisesRegex(routing.RoutingError, '超过 10 MB'):
            manager.import_proxy_provider(
                provider, 'x' * (routing.SUBSCRIPTION_SIZE_LIMIT + 1))

    def test_bootstrap_uses_only_local_snapshot_and_quick_service_status(self):
        manager = routing.RoutingManager()
        quick_status = {
            'ok': True, 'installed': True, 'running': False,
            'service_state': 'Running', 'service_backend': 'native',
        }
        cache = {'available': True, 'node_count': 72, 'updated_at': 1}
        snapshots = {'default': {'nodes': [{
            'name': '节点 A', 'alive': None, 'tested': False,
        }]}}
        with mock.patch.object(
                manager, 'status', return_value=quick_status) as status, \
                mock.patch.object(
                    routing.subscription_store, 'cache_status',
                    return_value=cache), \
                mock.patch.object(
                    routing._selection, 'reconcile_provider_nodes',
                    return_value=snapshots), \
                mock.patch.object(
                    routing.vpn_os, 'list_vpns',
                    side_effect=AssertionError('首屏不应枚举 VPN')) as vpns, \
                mock.patch.object(
                    routing, 'list_physical_interfaces',
                    side_effect=AssertionError('首屏不应枚举网卡')) as interfaces, \
                mock.patch.object(
                    routing, 'list_tun_conflicts',
                    side_effect=AssertionError('首屏不应扫描 TUN')) as conflicts, \
                mock.patch.object(routing, 'windows_system_proxy',
                                  return_value='http://127.0.0.1:7897'):
            result = manager.bootstrap(self.base_config())

        self.assertTrue(result['partial'])
        self.assertEqual(result['provider_caches']['default'], cache)
        self.assertEqual(result['provider_nodes'], snapshots)
        status.assert_called_once()
        self.assertTrue(status.call_args.kwargs['quick'])
        vpns.assert_not_called()
        interfaces.assert_not_called()
        conflicts.assert_not_called()

    def test_quick_status_does_not_wait_for_controller_probe(self):
        manager = routing.RoutingManager()
        native_state = {
            'installed': True, 'state': 'Running', 'backend': 'native',
            'runtime_running': True, 'runtime_enabled': True,
            'system_proxy_active': False, 'service_version': '0.2.0',
        }
        with mock.patch.object(
                manager, '_service_state', return_value=native_state), \
                mock.patch.object(
                    manager, '_controller_version',
                    side_effect=AssertionError('快速首屏不应等待 Controller')) as probe:
            status = manager.status(self.base_config(), quick=True)

        self.assertTrue(status['running'])
        probe.assert_not_called()

    def test_disabled_config_reports_running_core_as_standby_not_proxy(self):
        manager = routing.RoutingManager()
        config = self.base_config()
        config['enabled'] = False
        native_state = {
            'installed': True, 'state': 'Running', 'backend': 'native',
            'runtime_running': True, 'runtime_enabled': True,
            'runtime_mode': 'standby', 'system_proxy_active': False,
            'service_version': '0.3.0',
        }
        with mock.patch.object(
                manager, '_service_state', return_value=native_state), \
                mock.patch.object(manager, '_controller_version',
                                  return_value='v1.19.30'):
            status = manager.status(config)

        self.assertFalse(status['running'])
        self.assertTrue(status['core_running'])
        self.assertTrue(status['standby'])
        self.assertEqual(status['runtime_mode'], 'standby')

    def test_saves_disabled_config_without_service_operation(self):
        config = self.base_config()
        config['enabled'] = False
        manager = routing.RoutingManager()
        with mock.patch.object(
                manager, '_service_state',
                return_value={'installed': False, 'state': 'NotInstalled'}), \
                mock.patch.object(manager, '_uninstall_legacy') as uninstall:
            result = manager.apply(config)

        self.assertEqual(result['msg'], '分流配置已保存')
        uninstall.assert_not_called()

    def test_tests_proxy_group_through_controller(self):
        config = routing.normalize_config(self.base_config())
        manager = routing.RoutingManager()
        node_name = '[默认订阅] 节点 A'
        overview = {'proxies': {
            'PROXY': {'all': [node_name], 'now': node_name},
            'PROXY-default': {'all': [node_name], 'now': node_name},
        }}
        providers = {'providers': {'provider-default': {'proxies': [{
            'name': node_name, 'type': 'Vless', 'alive': True,
            'history': [{'delay': 31}],
        }]}}}

        def controller(_config, path, **_kwargs):
            if path == '/proxies':
                return overview
            if path == '/providers/proxies':
                return providers
            if '/healthcheck?' in path:
                return {'delay': 42}
            raise AssertionError(path)

        with mock.patch.object(
                manager, '_controller_request',
                side_effect=controller) as request:
            result = manager.test_proxy_group(config, 'all')

        self.assertEqual(result['delays'], {node_name: 42})
        health_path = next(call.args[1] for call in request.call_args_list
                           if '/healthcheck?' in call.args[1])
        self.assertIn('/providers/proxies/provider-default/', health_path)

    def test_proxy_overview_merges_provider_only_nodes(self):
        config = routing.normalize_config(self.base_config())
        manager = routing.RoutingManager()
        node_name = '[默认订阅] 节点 A'
        overview = {'proxies': {
            'PROXY': {'all': [node_name], 'now': node_name},
            'PROXY-default': {'all': [node_name], 'now': node_name},
        }}
        providers = {'providers': {'provider-default': {'proxies': [{
            'name': node_name, 'type': 'Vless', 'alive': True,
            'history': [{'delay': 56}],
        }]}}}
        with mock.patch.object(
                manager, '_controller_request',
                side_effect=[overview, providers]):
            groups = manager.proxy_overview(config)

        node = groups[1]['nodes'][0]
        self.assertEqual(node['display_name'], '节点 A')
        self.assertEqual(node['type'], 'Vless')
        self.assertEqual(node['delay'], 56)
        self.assertTrue(node['alive'])
        self.assertEqual(node['provider_name'], 'provider-default')

    def test_single_provider_node_uses_provider_healthcheck_endpoint(self):
        config = routing.normalize_config(self.base_config())
        manager = routing.RoutingManager()
        node_name = '[默认订阅] 节点 A'
        groups = [{
            'id': 'default', 'nodes': [{
                'name': node_name, 'provider_name': 'provider-default'}],
        }]
        with mock.patch.object(
                manager, 'proxy_overview', return_value=groups), \
                mock.patch.object(
                    manager, '_controller_request',
                    return_value={'delay': 63}) as request, \
                mock.patch.object(
                    routing._selection, 'persist_provider_nodes') as persist:
            result = manager.test_proxy_node(config, 'default', node_name)

        self.assertEqual(result['delay'], 63)
        self.assertIn(
            '/providers/proxies/provider-default/', request.call_args.args[1])
        saved = persist.call_args.args[2]
        self.assertEqual(saved[0]['delay'], 63)
        self.assertTrue(saved[0]['tested'])

    def test_manual_node_selection_rejects_automatic_group(self):
        config = routing.normalize_config(self.base_config())
        manager = routing.RoutingManager()

        with self.assertRaisesRegex(routing.RoutingError, '不是手动选择模式'):
            manager.select_proxy_node(config, 'default', '任意节点')

    def test_rejects_vpn_with_remote_default_gateway(self):
        config = routing.normalize_config(self.base_config())

        with self.assertRaisesRegex(routing.RoutingError, '远程默认网关'):
            routing.validate_environment(
                config, [vpn_profile(gateway=True)],
                [{'name': '以太网'}], [])

    def test_rejects_another_active_tun(self):
        config = routing.normalize_config(self.base_config())

        with self.assertRaisesRegex(routing.RoutingError, '其他 TUN'):
            routing.validate_environment(
                config, [vpn_profile()], [{'name': '以太网'}],
                [{'name': 'Clash'}])

    def test_disconnected_vpn_is_fail_closed_warning(self):
        config = routing.normalize_config(self.base_config())

        warnings = routing.validate_environment(
            config, [vpn_profile(connected=False)], [{'name': '以太网'}], [])

        self.assertIn('请求会失败', warnings[0])


if __name__ == '__main__':
    unittest.main()
