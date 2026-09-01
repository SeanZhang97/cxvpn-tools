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
    def test_new_install_defaults_to_system_proxy(self):
        value = routing.default_config()
        self.assertEqual(value['schema_version'], 2)
        self.assertEqual(value['capture_mode'], 'system-proxy')
        self.assertEqual(value['builtin_rule_pack'], 'local-direct-v1')

    def test_legacy_object_keeps_tun_and_user_only_rules(self):
        value = routing.normalize_config({
            'enabled': True,
            'physical_interface': 'Ethernet',
            'default_outbound': 'physical',
        })
        self.assertEqual(value['capture_mode'], 'tun')
        self.assertEqual(value['builtin_rule_pack'], 'off')
        self.assertEqual(value['schema_version'], 2)

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
        self.assertEqual(rules[1:1 + len(routing_rules.LOCAL_DIRECT_V1)],
                         routing_rules.rules_for('local-direct-v1'))
        self.assertEqual(rules[-1], 'MATCH,PHYSICAL')

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
            **routing.default_config(), 'physical_interface': 'Ethernet'})
        with mock.patch.object(
                routing, 'windows_system_proxy',
                return_value='http://127.0.0.1:17890'):
            manager._install('candidate.json', config)
        manager._native_service.activate_system_proxy.assert_called_once_with('tx-system')
        manager._native_service.commit.assert_called_once_with('tx-system')
        manager._native_service.rollback.assert_not_called()

    def test_system_proxy_health_failure_restores_through_rollback(self):
        manager = routing.RoutingManager()
        manager._native_service = mock.Mock()
        manager._native_service.apply.return_value = {'transaction_id': 'tx-fail'}
        manager._native_provider_files = mock.Mock(return_value=[])
        manager._wait_native_ready = mock.Mock()
        manager._probe_connectivity = mock.Mock(
            side_effect=routing.RoutingError('system chain failed'))
        config = routing.normalize_config({
            **routing.default_config(), 'physical_interface': 'Ethernet'})
        with mock.patch.object(
                routing, 'windows_system_proxy',
                return_value='http://127.0.0.1:17890'):
            with self.assertRaisesRegex(routing.RoutingError, 'system chain failed'):
                manager._install('candidate.json', config)
        manager._native_service.activate_system_proxy.assert_called_once_with('tx-fail')
        manager._native_service.rollback.assert_called_once_with('tx-fail')

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


if __name__ == '__main__':
    unittest.main()
