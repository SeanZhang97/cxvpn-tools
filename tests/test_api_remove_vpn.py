# -*- coding: utf-8 -*-
import threading
import unittest
from unittest.mock import Mock, patch

from api import Api


class ApiRemoveVpnTests(unittest.TestCase):
    def make_api(self):
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance.cfg = {
            'vpn_name': '目标 VPN',
            'creds': {
                '目标 VPN': {'user': 'alice', 'pass': 'secret'},
                '其他 VPN': {'user': 'bob', 'pass': 'other'},
            },
        }
        instance.worker = Mock()
        instance.log = Mock()
        return instance

    @patch('api.cfgmod.save')
    @patch('api.vpn_os.remove_vpn', return_value=(True, '已删除'))
    def test_success_clears_target_and_matching_credential(self, remove, save):
        instance = self.make_api()

        result = instance.remove_vpn('目标 VPN')

        self.assertTrue(result['ok'])
        self.assertEqual(instance.cfg['vpn_name'], '')
        self.assertNotIn('目标 VPN', instance.cfg['creds'])
        self.assertIn('其他 VPN', instance.cfg['creds'])
        save.assert_called_once_with(instance.cfg)
        remove.assert_called_once_with('目标 VPN')
        instance.worker.reset_backoff.assert_called_once_with()

    @patch('api.cfgmod.save')
    @patch('api.vpn_os.remove_vpn', return_value=(False, '删除失败'))
    def test_failure_keeps_config_unchanged(self, remove, save):
        instance = self.make_api()

        result = instance.remove_vpn('目标 VPN')

        self.assertFalse(result['ok'])
        self.assertEqual(instance.cfg['vpn_name'], '目标 VPN')
        self.assertIn('目标 VPN', instance.cfg['creds'])
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
