# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

from core import config as cfgmod
from core import routing
from core import routing_rules


class RoutingSchemaTests(unittest.TestCase):
    def test_subscription_url_moves_path_appended_query_into_query(self):
        config = routing.normalize_config({
            'proxy_providers': [{
                'id': 'dirty', 'name': '脏 URL',
                'url': 'https://example.test/subscription&token=a%2Bb&client=clash',
            }],
        })
        self.assertEqual(
            config['proxy_providers'][0]['url'],
            'https://example.test/subscription?token=a%2Bb&client=clash')

    def test_subscription_url_with_real_query_is_not_rewritten(self):
        value = 'https://example.test/subscription?a=1&b=2'
        config = routing.normalize_config({
            'proxy_providers': [{
                'id': 'clean', 'name': '正常 URL', 'url': value,
            }],
        })
        self.assertEqual(config['proxy_providers'][0]['url'], value)

    def test_new_install_defaults_to_system_proxy(self):
        value = routing.default_config()
        self.assertEqual(value['schema_version'], 8)
        self.assertEqual(value['capture_mode'], 'system-proxy')
        self.assertEqual(value['builtin_rule_pack'], 'cn-direct-v1')
        self.assertEqual(value['dns_mode'], 'simple')

    def test_legacy_object_keeps_tun_and_user_only_rules(self):
        value = routing.normalize_config({
            'enabled': True,
            'physical_interface': 'Ethernet',
            'default_outbound': 'physical',
        })
        self.assertEqual(value['capture_mode'], 'tun')
        self.assertEqual(value['builtin_rule_pack'], 'off')
        self.assertEqual(value['schema_version'], 8)
        self.assertEqual(value['dns_mode'], 'advanced')

    def test_config_load_migrates_legacy_without_changing_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'config.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({'routing': {'enabled': True}}, stream)
            with mock.patch.object(cfgmod, 'CFG_PATH', path):
                value = cfgmod.load()['routing']
        self.assertEqual(value['capture_mode'], 'tun')
        self.assertEqual(value['builtin_rule_pack'], 'off')

    def test_system_proxy_config_has_mixed_port_and_layered_dns(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'physical_interface': 'Ethernet',
            'dns_mode': 'advanced',
            'default_nameserver': ['223.5.5.5'],
            'proxy_server_nameserver': ['1.1.1.1'],
            'direct_nameserver': ['119.29.29.29'],
        })
        generated = routing.build_mihomo_config(config, [])
        self.assertEqual(generated['mixed-port'], 17890)
        self.assertFalse(generated['tun']['enable'])
        self.assertEqual(generated['dns']['fake-ip-range6'], 'fdfe:dcba:9876::1/64')
        self.assertEqual(generated['dns']['default-nameserver'], ['223.5.5.5'])
        self.assertEqual(generated['dns']['proxy-server-nameserver'], ['1.1.1.1'])
        self.assertEqual(generated['dns']['direct-nameserver'], ['119.29.29.29'])
        self.assertTrue(generated['unified-delay'])
        self.assertTrue(generated['tcp-concurrent'])

    def test_simple_dns_ignores_preserved_advanced_draft(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'dns_mode': 'simple',
            'dns_servers': ['9.9.9.9'],
            'dns_enhanced_mode': 'redir-host',
        })

        generated = routing.build_mihomo_config(config, [])['dns']

        self.assertEqual(generated['enhanced-mode'], 'fake-ip')
        self.assertEqual(generated['nameserver'], ['223.5.5.5', '119.29.29.29'])
        self.assertTrue(generated['respect-rules'])
        self.assertEqual(generated['proxy-server-nameserver'], [
            'https://dns.alidns.com/dns-query',
            'https://doh.pub/dns-query',
        ])
        self.assertEqual(generated['fallback'], [
            'https://dns.alidns.com/dns-query',
            'https://doh.pub/dns-query',
        ])
        self.assertNotIn('1.1.1.1', repr(generated))
        self.assertNotIn('8.8.8.8', repr(generated))
        self.assertEqual(generated['fallback-filter']['geoip-code'], 'CN')
        self.assertEqual(config['dns_servers'], ['9.9.9.9'])

    def test_advanced_dns_generates_policy_and_validates_fake_ip_pool(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'dns_mode': 'advanced',
            'dns_enhanced_mode': 'fake-ip',
            'dns_respect_rules': True,
            'fake_ip_range': '198.19.0.1/16',
            'fake_ip_filter': ['+.lan', '*.corp.example'],
            'nameserver_policy': [{
                'domain': '*.corp.example',
                'servers': ['tls://dns.google'],
            }],
        })

        generated = routing.build_mihomo_config(config, [])['dns']

        self.assertTrue(generated['respect-rules'])
        self.assertEqual(generated['fake-ip-range'], '198.19.0.0/16')
        self.assertEqual(generated['fake-ip-filter'], ['+.lan', '*.corp.example'])
        self.assertEqual(generated['nameserver-policy'], {
            '+.corp.example': ['tls://dns.google']})

        with self.assertRaisesRegex(routing.RoutingError, '198.18.0.0/15'):
            routing.normalize_config({
                **routing.default_config(),
                'dns_mode': 'advanced', 'fake_ip_range': '10.0.0.0/16',
            })

    def test_redir_host_omits_fake_ip_runtime_fields(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'dns_mode': 'advanced',
            'dns_enhanced_mode': 'redir-host',
        })

        generated = routing.build_mihomo_config(config, [])['dns']

        self.assertEqual(generated['enhanced-mode'], 'redir-host')
        self.assertNotIn('fake-ip-range', generated)
        self.assertNotIn('fake-ip-filter', generated)

    def test_rule_order_is_user_then_builtin_then_match(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'physical_interface': 'Ethernet',
            'builtin_rule_pack': 'local-direct-v1',
            'rules': [{
                'id': '中文-rule', 'match_type': 'suffix',
                'domain': 'example.com', 'outbound': 'block',
            }],
        })
        rules = routing.build_mihomo_config(config, [])['rules']
        self.assertEqual(rules[0], 'DOMAIN-SUFFIX,example.com,REJECT')
        local_rules = routing_rules.rules_for('local-direct-v1')
        self.assertEqual(rules[1:1 + len(local_rules)], local_rules)
        self.assertEqual(rules[-1], 'MATCH,PHYSICAL')

    def test_builtin_rule_catalog_is_structured_readonly_and_complete(self):
        catalog = routing_rules.catalog()

        self.assertEqual(list(catalog), [
            'off', 'local-direct-v1', 'cn-direct-v1'])
        self.assertTrue(catalog['cn-direct-v1']['readonly'])
        self.assertTrue(catalog['cn-direct-v1']['file_editable'])
        self.assertEqual(
            catalog['cn-direct-v1']['source_file'],
            'rule-packs/cn-direct-v1.txt')
        self.assertEqual(
            catalog['cn-direct-v1']['rule_count'],
            len(routing_rules.rules_for('cn-direct-v1')))
        self.assertEqual(
            catalog['cn-direct-v1']['system_proxy_domains'],
            routing_rules.cn_direct_suffixes())
        self.assertEqual(
            catalog['cn-direct-v1']['system_proxy_domain_count'],
            len(routing_rules.cn_direct_suffixes()))
        self.assertNotIn('lan', routing_rules.cn_direct_suffixes())
        self.assertGreaterEqual(catalog['cn-direct-v1']['rule_count'], 75)
        self.assertEqual(catalog['cn-direct-v1']['rules'][0], {
            'index': 1,
            'type': 'DOMAIN-SUFFIX',
            'value': 'lan',
            'outbound': 'PHYSICAL',
            'options': [],
        })
        values = {
            item['value'] for item in catalog['cn-direct-v1']['rules']
        }
        self.assertTrue({
            'chaoxing.com', 'wisweb.com', 'cldisk.com', 'llm-api.net',
            'aichaoxing.com', 'sslibrary.com',
            'aliyuncs.com', 'baidubce.com', 'tencentcloudapi.com',
            'volces.com', 'deepseek.com', 'stepfun.com',
            'baichuan-ai.com', 'xf-yun.com',
        }.issubset(values))

    def test_rule_pack_files_load_once_and_normalize_unicode_domains(self):
        self.addCleanup(routing_rules.load_rule_packs)
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'local-direct-v1.txt'), 'w',
                      encoding='utf-8') as stream:
                stream.write(
                    'DOMAIN-SUFFIX,例子.中国\n'
                    'DOMAIN-WILDCARD,*.example.com\n'
                    'IP-CIDR,10.0.0.1/8,no-resolve\n')
            cn_path = os.path.join(root, 'cn-direct-v1.txt')
            with open(cn_path, 'w', encoding='utf-8') as stream:
                stream.write('例子.公司.cn\ne\u0301xample.com\n')
            with mock.patch.object(routing_rules, 'RULE_PACK_DIR', root):
                routing_rules.load_rule_packs()
                rules = routing_rules.rules_for('cn-direct-v1')
                self.assertIn(
                    'DOMAIN-SUFFIX,xn--fsqu00a.xn--fiqs8s,PHYSICAL', rules)
                self.assertIn(
                    'DOMAIN-SUFFIX,xn--fsqu00a.xn--55qx5d.cn,PHYSICAL', rules)
                self.assertIn(
                    'DOMAIN-SUFFIX,xn--xample-9ua.com,PHYSICAL', rules)
                self.assertIn(
                    'DOMAIN-WILDCARD,*.example.com,PHYSICAL', rules)
                self.assertIn(
                    'IP-CIDR,10.0.0.0/8,PHYSICAL,no-resolve', rules)
                with open(cn_path, 'w', encoding='utf-8') as stream:
                    stream.write('changed.example\n')
                self.assertNotIn(
                    'DOMAIN-SUFFIX,changed.example,PHYSICAL',
                    routing_rules.rules_for('cn-direct-v1'))
                routing_rules.load_rule_packs()
                self.assertIn(
                    'DOMAIN-SUFFIX,changed.example,PHYSICAL',
                    routing_rules.rules_for('cn-direct-v1'))

    def test_invalid_non_bmp_rule_is_reported_without_breaking_catalog(self):
        self.addCleanup(routing_rules.load_rule_packs)
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'local-direct-v1.txt'), 'w',
                      encoding='utf-8') as stream:
                stream.write('DOMAIN-SUFFIX,local\n')
            with open(os.path.join(root, 'cn-direct-v1.txt'), 'w',
                      encoding='utf-8') as stream:
                stream.write('🇨🇳.example\n')
            with mock.patch.object(routing_rules, 'RULE_PACK_DIR', root):
                routing_rules.load_rule_packs()
                with self.assertRaisesRegex(
                        routing_rules.RulePackError, '第 1 行域名格式无效'):
                    routing_rules.rules_for('cn-direct-v1')
                detail = routing_rules.catalog()['cn-direct-v1']
                self.assertEqual(detail['rules'], [])
                self.assertIn('第 1 行域名格式无效', detail['error'])

    def test_invalid_rule_pack_is_exposed_as_routing_error(self):
        self.addCleanup(routing_rules.load_rule_packs)
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'local-direct-v1.txt'), 'w',
                      encoding='utf-8') as stream:
                stream.write('DOMAIN-SUFFIX,https://example.com\n')
            with open(os.path.join(root, 'cn-direct-v1.txt'), 'w',
                      encoding='utf-8') as stream:
                stream.write('cn\n')
            config = routing.normalize_config({
                **routing.default_config(),
                'physical_interface': 'Ethernet',
                'builtin_rule_pack': 'local-direct-v1',
            })
            with mock.patch.object(routing_rules, 'RULE_PACK_DIR', root):
                routing_rules.load_rule_packs()
                with self.assertRaisesRegex(
                        routing.RoutingError, '第 1 行域名格式无效'):
                    routing.build_mihomo_config(config, [])

    def test_global_mode_only_generates_match(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'physical_interface': 'Ethernet',
            'traffic_mode': 'global',
            'builtin_rule_pack': 'cn-direct-v1',
            'rules': [{'domain': 'example.com', 'outbound': 'block'}],
        })
        self.assertEqual(routing.build_mihomo_config(config, [])['rules'],
                         ['MATCH,PHYSICAL'])

    def test_custom_bypass_precedes_global_match_and_enables_process_lookup(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'physical_interface': 'Ethernet',
            'traffic_mode': 'global',
            'system_proxy_bypass': {
                'domains': ['Internal.Example.com', '.internal.example.com'],
                'processes': ['Updater.exe'],
            },
        })

        generated = routing.build_mihomo_config(config, [])

        self.assertEqual(config['system_proxy_bypass']['domains'], [
            'internal.example.com'])
        self.assertEqual(generated['find-process-mode'], 'strict')
        self.assertEqual(generated['rules'], [
            'DOMAIN-SUFFIX,internal.example.com,PHYSICAL',
            'PROCESS-NAME,Updater.exe,PHYSICAL',
            'MATCH,PHYSICAL',
        ])
        explained = routing.explain_domain(config, 'api.internal.example.com')
        self.assertEqual(explained['source'], 'bypass')
        self.assertEqual(explained['outbound'], 'physical')

    def test_cn_pack_reference_adds_system_bypass_and_geoip_fallback(self):
        config = routing.normalize_config({
            **routing.default_config(),
            'physical_interface': 'Ethernet',
            'builtin_rule_pack': 'off',
            'system_proxy_bypass': {
                'include_cn_direct': True,
                'domains': ['chaoxing.com'],
                'processes': [],
            },
        })
        with mock.patch.object(
                routing_rules, 'cn_direct_suffixes',
                return_value=['cn', 'chaoxing.com', 'baidu.com']):
            self.assertEqual(routing.system_proxy_bypass_domains(config), [
                'chaoxing.com', 'cn', 'baidu.com'])
            self.assertEqual(
                routing.explain_domain(config, 'www.baidu.com')['source'],
                'bypass')
            self.assertEqual(routing.build_mihomo_config(config, [])['rules'], [
                'DOMAIN-SUFFIX,chaoxing.com,PHYSICAL',
                'GEOIP,CN,PHYSICAL',
                'MATCH,PHYSICAL',
            ])

    def test_rejects_path_like_bypass_process(self):
        with self.assertRaisesRegex(routing.RoutingError, '进程名称无效'):
            routing.normalize_config({
                **routing.default_config(),
                'system_proxy_bypass': {'processes': [r'C:\\Tools\\app.exe']},
            })

    def test_hit_explanation_identifies_all_layers(self):
        base = {
            **routing.default_config(),
            'physical_interface': 'Ethernet',
            'builtin_rule_pack': 'cn-direct-v1',
            'rules': [{'domain': 'example.com', 'outbound': 'block'}],
        }
        self.assertEqual(routing.explain_domain(base, 'a.example.com')['source'], 'user')
        self.assertEqual(routing.explain_domain(base, 'www.baidu.com')['source'], 'builtin')
        self.assertEqual(routing.explain_domain(base, 'example.net')['source'], 'default')

        with_fallback = routing.normalize_config({
            **base,
            'system_proxy_bypass': {'include_cn_direct': True},
        })
        runtime = routing.explain_domain(with_fallback, 'unknown.example.net')
        self.assertEqual(runtime['source'], 'default')
        self.assertTrue(runtime['runtime_geoip_fallback'])
        self.assertIn('中国 IP', runtime['runtime_detail'])
        explicit = routing.explain_domain(with_fallback, 'a.example.com')
        self.assertFalse(explicit['runtime_geoip_fallback'])

    def test_config_preflight_seeds_verified_geoip_database(self):
        manager = routing.RoutingManager()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
                routing.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            data_dir = os.path.join(root, 'data')
            manager._test_config(os.path.join(root, 'candidate.json'), data_dir)
            target = os.path.join(data_dir, routing.GEOIP_DATABASE)
            self.assertTrue(os.path.isfile(target))
            self.assertEqual(routing._sha256(target), routing.GEOIP_DATABASE_SHA256)

    def test_unicode_node_names_survive_normalization(self):
        value = routing.normalize_config({
            **routing.default_config(),
            'proxy_providers': [{
                'id': 'unicode', 'name': '订阅e\u0301', 'url': 'https://example.test/sub',
                'enabled': True, 'selection_mode': 'manual',
                'selected_node': '日本🇯🇵-e\u0301',
            }],
            'default_outbound': 'proxy:unicode',
        })
        self.assertEqual(value['proxy_providers'][0]['selected_node'], '日本🇯🇵-e\u0301')

    def test_system_proxy_activation_is_inside_service_transaction(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {'transaction_id': 'tx-system'}
        manager._native_provider_files = mock.Mock(return_value=[])
        manager._wait_native_ready = mock.Mock()
        manager._probe_connectivity = mock.Mock()
        config = routing.normalize_config({
            **routing.default_config(), 'physical_interface': 'Ethernet',
            'system_proxy_bypass': {
                'domains': ['chaoxing.com'], 'processes': []},
        })
        with mock.patch.object(
                routing, 'windows_system_proxy',
                return_value='http://127.0.0.1:17890'):
            manager._install('candidate.json', config)
        manager._native_service.activate_system_proxy.assert_called_once_with('tx-system')
        self.assertEqual(
            manager._native_service.apply.call_args.kwargs[
                'system_proxy_bypass_domains'],
            ['chaoxing.com'])
        manager._native_service.commit.assert_called_once_with('tx-system')
        manager._native_service.rollback.assert_not_called()

    def test_system_proxy_activation_merges_cn_pack_reference(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {'transaction_id': 'tx-cn'}
        manager._native_provider_files = mock.Mock(return_value=[])
        manager._wait_native_ready = mock.Mock()
        manager._probe_connectivity = mock.Mock()
        config = routing.normalize_config({
            **routing.default_config(), 'physical_interface': 'Ethernet',
            'system_proxy_bypass': {
                'include_cn_direct': True,
                'domains': ['chaoxing.com'], 'processes': []},
        })
        with mock.patch.object(
                routing_rules, 'cn_direct_suffixes',
                return_value=['cn', 'chaoxing.com', 'baidu.com']), mock.patch.object(
                routing, 'windows_system_proxy',
                return_value='http://127.0.0.1:17890'):
            manager._install('candidate.json', config)
        self.assertEqual(
            manager._native_service.apply.call_args.kwargs[
                'system_proxy_bypass_domains'],
            ['chaoxing.com', 'cn', 'baidu.com'])

    def test_system_proxy_health_failure_restores_through_rollback(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {'transaction_id': 'tx-fail'}
        manager._native_provider_files = mock.Mock(return_value=[])
        manager._wait_native_ready = mock.Mock(
            side_effect=routing.RoutingError('system chain failed'))
        config = routing.normalize_config({
            **routing.default_config(), 'physical_interface': 'Ethernet'})
        with mock.patch.object(
                routing, 'windows_system_proxy',
                return_value='http://127.0.0.1:17890'):
            with self.assertRaisesRegex(routing.RoutingError, 'system chain failed'):
                manager._install('candidate.json', config)
        manager._native_service.activate_system_proxy.assert_not_called()
        manager._native_service.rollback.assert_called_once_with('tx-fail')

    def test_tun_confirms_manual_proxy_is_suspended_before_commit(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {'transaction_id': 'tx-tun'}
        manager._native_provider_files = mock.Mock(return_value=[])
        manager._wait_native_ready = mock.Mock()
        manager._refresh_user_proxy_settings = mock.Mock(return_value=True)
        config = routing.normalize_config({
            **routing.default_config(), 'capture_mode': 'tun',
            'physical_interface': 'Ethernet'})

        with mock.patch.object(
                routing, 'windows_manual_proxy_state',
                return_value={'enabled': False, 'server': ''}):
            manager._install('candidate.json', config)

        manager._refresh_user_proxy_settings.assert_called_once_with(
            'TUN 接管关闭系统代理后')
        manager._wait_native_ready.assert_called_once_with(config, 'active')
        manager._native_service.commit.assert_called_once_with('tx-tun')
        manager._native_service.rollback.assert_not_called()

    def test_tun_manual_proxy_readback_or_refresh_failure_rolls_back(self):
        for refreshed, state in (
                (False, {'enabled': False, 'server': ''}),
                (True, {'enabled': True, 'server': '127.0.0.1:17890'}),
                (True, {'enabled': False, 'server': '127.0.0.1:17890'})):
            with self.subTest(refreshed=refreshed, state=state):
                manager = routing.RoutingManager()
                manager._native_service = mock.Mock()
                manager._native_service.apply.return_value = {
                    'transaction_id': 'tx-tun-fail'}
                manager._native_provider_files = mock.Mock(return_value=[])
                manager._wait_native_ready = mock.Mock()
                manager._refresh_user_proxy_settings = mock.Mock(
                    return_value=refreshed)
                config = routing.normalize_config({
                    **routing.default_config(), 'capture_mode': 'tun',
                    'physical_interface': 'Ethernet'})

                with mock.patch.object(
                        routing, 'windows_manual_proxy_state',
                        return_value=state), self.assertRaisesRegex(
                            routing.RoutingError, '关闭回读不一致'):
                    manager._install('candidate.json', config)

                manager._wait_native_ready.assert_not_called()
                manager._native_service.commit.assert_not_called()
                manager._native_service.rollback.assert_called_once_with(
                    'tx-tun-fail')

    def test_system_proxy_registry_readback_mismatch_rolls_back(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {'transaction_id': 'tx-readback'}
        manager._native_provider_files = mock.Mock(return_value=[])
        manager._wait_native_ready = mock.Mock()
        config = routing.normalize_config({
            **routing.default_config(), 'physical_interface': 'Ethernet'})
        with mock.patch.object(
                routing, 'windows_system_proxy',
                return_value='http://127.0.0.1:7897'):
            with self.assertRaisesRegex(routing.RoutingError, '回读不一致'):
                manager._install('candidate.json', config)
        manager._native_service.rollback.assert_called_once_with('tx-readback')

    def test_transaction_repairs_only_confirmed_local_disabled_proxy(self):
        for server, enabled, confirmed, should_write in (
                ('127.0.0.1:17890', False, True, True),
                ('127.0.0.1:7897', False, True, False),
                ('127.0.0.1:17890', True, True, False),
                ('127.0.0.1:17890', False, False, False)):
            with self.subTest(server=server, enabled=enabled, confirmed=confirmed):
                manager = routing.RoutingManager()
                manager._native_service = mock.Mock()
                manager._native_service.apply.return_value = {'transaction_id': 'tx'}
                manager._native_service.activate_system_proxy.return_value = {
                    'system_proxy_active': confirmed, 'mixed_port': 17890}
                manager._native_provider_files = mock.Mock(return_value=[])
                manager._wait_native_ready = mock.Mock()
                config = routing.normalize_config(routing.default_config())
                with mock.patch.object(routing, 'windows_system_proxy',
                                       side_effect=['', 'http://127.0.0.1:17890']), \
                        mock.patch.object(routing.proxy_guard, 'read_proxy_state',
                                          return_value={'enable': enabled, 'server': server}), \
                        mock.patch.object(routing.proxy_guard, 'set_proxy_enabled') as write:
                    manager._install('candidate.json', config)
                self.assertEqual(write.call_count, int(should_write))
                manager._native_service.commit.assert_called_once_with('tx')
                manager._native_service.rollback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
