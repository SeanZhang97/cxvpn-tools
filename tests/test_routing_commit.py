# -*- coding: utf-8 -*-
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from api import Api
from core import config as cfgmod


class ConfigAtomicSaveTests(unittest.TestCase):
    def test_save_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'config.json')
            with patch.object(cfgmod, 'CFG_PATH', path):
                cfgmod.save({'routing': {'enabled': True}})
            with open(path, encoding='utf-8') as stream:
                self.assertEqual(json.load(stream)['routing']['enabled'], True)
            self.assertEqual(
                [name for name in os.listdir(root) if name.endswith('.tmp')], [])

    def test_export_failure_keeps_committed_database_and_previous_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'config.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({'version': 'old'}, stream)
            with patch.object(cfgmod, 'CFG_PATH', path), \
                    patch('core.config.os.replace', side_effect=OSError('locked')):
                self.assertTrue(cfgmod.save({'version': 'new'}))
                self.assertEqual(cfgmod.load()['version'], 'new')
            with open(path, encoding='utf-8') as stream:
                self.assertEqual(json.load(stream), {'version': 'old'})
            self.assertEqual(
                [name for name in os.listdir(root) if name.endswith('.tmp')], [])

    def test_load_recovers_from_valid_backup_when_primary_is_corrupt(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'config.json')
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('{corrupt')
            with open(path + '.bak', 'w', encoding='utf-8') as stream:
                json.dump({'routing': {'rules': [{'domain': 'example.com'}]}}, stream)
            with patch.object(cfgmod, 'CFG_PATH', path), \
                    patch.object(cfgmod, 'CFG_BACKUP_PATH', path + '.bak'):
                value = cfgmod.load()
            self.assertEqual(value['routing']['rules'][0]['domain'], 'example.com')


class RoutingCommitTests(unittest.TestCase):
    def make_api(self):
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._routing_lock = threading.Lock()
        instance.cfg = {
            'vpn_name': '',
            'routing': {'enabled': False, 'default_outbound': 'physical'},
        }
        instance.log = Mock()
        instance.routing = Mock()
        instance._start_routing_standby_reconcile = Mock()
        return instance

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_apply_commits_memory_only_after_disk_save(self, save, prune):
        instance = self.make_api()
        applied = {
            'enabled': True,
            'default_outbound': 'proxy',
            'proxy_providers': [],
        }
        instance.routing.apply.return_value = {
            'ok': True, 'config': applied, 'status': {}, 'warnings': []}

        result = instance.apply_routing(applied)

        self.assertTrue(result['ok'])
        self.assertEqual(instance.cfg['routing'], applied)
        save.assert_called_once()
        prune.assert_called_once_with([])

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_apply_preserves_concurrent_non_routing_changes(self, save, _prune):
        instance = self.make_api()
        applied = {
            'enabled': False,
            'default_outbound': 'physical',
            'proxy_providers': [],
        }

        def apply(_value, **_kwargs):
            instance.cfg['vpn_name'] = '并发更新后的 VPN'
            return {'ok': True, 'config': applied, 'status': {}, 'warnings': []}

        instance.routing.apply.side_effect = apply

        result = instance.apply_routing(applied)

        self.assertTrue(result['ok'])
        self.assertEqual(instance.cfg['vpn_name'], '并发更新后的 VPN')
        self.assertEqual(save.call_args.args[0]['vpn_name'], '并发更新后的 VPN')

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save', side_effect=OSError('disk full'))
    def test_save_failure_rolls_back_service_and_keeps_memory(self, save, prune):
        instance = self.make_api()
        previous = json.loads(json.dumps(instance.cfg['routing']))
        applied = {
            'enabled': True,
            'default_outbound': 'proxy',
            'proxy_providers': [],
        }
        instance.routing.apply.side_effect = [
            {'ok': True, 'config': applied, 'status': {}, 'warnings': []},
            {'ok': True, 'config': previous, 'status': {}, 'warnings': []},
        ]

        result = instance.apply_routing(applied)

        self.assertFalse(result['ok'])
        self.assertIn('已自动恢复', result['msg'])
        self.assertEqual(instance.cfg['routing'], previous)
        self.assertEqual(instance.routing.apply.call_count, 2)
        self.assertEqual(
            instance.routing.apply.call_args_list[0].kwargs,
            {'defer_standby': True, 'allow_fast_toggle': False})
        instance.routing.apply.assert_called_with(previous)
        prune.assert_not_called()

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_disable_schedules_standby_only_after_config_is_saved(
            self, save, _prune):
        instance = self.make_api()
        applied = {
            'enabled': False,
            'default_outbound': 'proxy',
            'proxy_providers': [{'id': 'alpha', 'enabled': True}],
        }
        instance.routing.apply.return_value = {
            'ok': True, 'config': applied, 'status': {}, 'warnings': [],
            'standby_pending': True,
        }

        result = instance.apply_routing(applied)

        self.assertTrue(result['ok'])
        self.assertNotIn('standby_pending', result)
        save.assert_called_once()
        instance._start_routing_standby_reconcile.assert_called_once_with(
            '代理关闭后')

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_home_toggle_uses_latest_backend_config_and_allows_fast_path(
            self, _save, _prune):
        instance = self.make_api()
        instance.cfg['routing'] = {
            'enabled': False,
            'default_outbound': 'physical',
            'schema_version': 7,
            'capture_mode': 'system-proxy',
            'controller_port': 19090,
            'controller_secret': 'stable-controller-secret',
        }
        applied = {
            'enabled': True,
            'default_outbound': 'physical',
            'traffic_mode': 'rule',
            'proxy_providers': [],
        }
        instance.routing.apply.return_value = {
            'ok': True, 'config': applied, 'status': {}, 'warnings': [],
            'standby_pending': False,
        }

        result = instance.set_routing_enabled(True, 'rule')

        self.assertTrue(result['ok'])
        submitted = instance.routing.apply.call_args.args[0]
        self.assertTrue(submitted['enabled'])
        self.assertEqual(submitted['traffic_mode'], 'rule')
        self.assertTrue(
            instance.routing.apply.call_args.kwargs['allow_fast_toggle'])

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_home_toggle_stages_capture_mode_only_when_enabling(
            self, _save, _prune):
        instance = self.make_api()
        instance.cfg['routing'] = {
            'enabled': False,
            'default_outbound': 'physical',
            'schema_version': 7,
            'capture_mode': 'system-proxy',
            'traffic_mode': 'global',
            'controller_port': 19090,
            'controller_secret': 'stable-controller-secret',
        }
        applied = {
            'enabled': True,
            'default_outbound': 'physical',
            'traffic_mode': 'global',
            'capture_mode': 'tun',
            'proxy_providers': [],
        }
        instance.routing.apply.return_value = {
            'ok': True, 'config': applied, 'status': {}, 'warnings': [],
            'standby_pending': False,
        }

        result = instance.set_routing_enabled(True, '', 'tun')

        self.assertTrue(result['ok'])
        submitted = instance.routing.apply.call_args.args[0]
        self.assertEqual(submitted['capture_mode'], 'tun')
        self.assertEqual(submitted['traffic_mode'], 'global')
        self.assertFalse(
            instance.routing.apply.call_args.kwargs['allow_fast_toggle'])

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_home_toggle_rejects_invalid_capture_mode(self, _save, _prune):
        instance = self.make_api()

        result = instance.set_routing_enabled(True, 'rule', 'bridge')

        self.assertFalse(result['ok'])
        self.assertEqual(result['msg'], '接管方式无效')
        instance.routing.apply.assert_not_called()

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_home_toggle_disable_ignores_staged_modes(self, _save, _prune):
        instance = self.make_api()
        instance.cfg['routing'] = {
            'enabled': True,
            'default_outbound': 'physical',
            'schema_version': 7,
            'capture_mode': 'system-proxy',
            'traffic_mode': 'rule',
            'controller_port': 19090,
            'controller_secret': 'stable-controller-secret',
        }
        applied = {
            'enabled': False,
            'default_outbound': 'physical',
            'proxy_providers': [],
        }
        instance.routing.apply.return_value = {
            'ok': True, 'config': applied, 'status': {}, 'warnings': [],
            'standby_pending': False,
        }

        result = instance.set_routing_enabled(False, 'global', 'tun')

        self.assertTrue(result['ok'])
        submitted = instance.routing.apply.call_args.args[0]
        self.assertFalse(submitted['enabled'])
        self.assertEqual(submitted['capture_mode'], 'system-proxy')
        self.assertEqual(submitted['traffic_mode'], 'rule')

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_default_outbound_switch_uses_latest_backend_config(self, _save, _prune):
        instance = self.make_api()
        instance.cfg['routing'] = {
            'enabled': True,
            'default_outbound': 'physical',
            'schema_version': 7,
            'capture_mode': 'system-proxy',
            'traffic_mode': 'rule',
            'controller_port': 19090,
            'controller_secret': 'stable-controller-secret',
            'proxy_providers': [{'id': 'alpha', 'enabled': True}],
        }
        applied = {
            'enabled': True,
            'default_outbound': 'proxy:alpha',
            'traffic_mode': 'rule',
            'proxy_providers': [{'id': 'alpha', 'enabled': True}],
        }
        instance.routing.apply.return_value = {
            'ok': True, 'config': applied, 'status': {}, 'warnings': [],
            'standby_pending': False,
        }

        result = instance.set_routing_default_outbound('proxy:alpha')

        self.assertTrue(result['ok'])
        submitted = instance.routing.apply.call_args.args[0]
        self.assertEqual(submitted['default_outbound'], 'proxy:alpha')
        self.assertTrue(submitted['enabled'])
        self.assertEqual(submitted['traffic_mode'], 'rule')
        self.assertEqual(submitted['capture_mode'], 'system-proxy')

    @patch('api.routing.subscription_store.prune_cache')
    @patch('api.cfgmod.save')
    def test_default_outbound_switch_rejects_invalid_value(self, _save, _prune):
        instance = self.make_api()

        result = instance.set_routing_default_outbound('outbound: biz')

        self.assertFalse(result['ok'])
        self.assertEqual(result['msg'], '默认出口无效')
        instance.routing.apply.assert_not_called()

if __name__ == '__main__':
    unittest.main()
