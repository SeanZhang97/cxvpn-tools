# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import api


class ApiStateStreamTests(unittest.TestCase):
    def test_ui_config_and_routing_results_hide_controller_fields(self):
        value = {
            'vpn_name': '工作 VPN',
            'routing': {
                'controller_port': 19090,
                'controller_secret': '秘密🇯🇵',
                'physical_interface': '以太网 e\u0301',
            },
        }
        safe = api.Api._config_for_ui(value)
        self.assertNotIn('controller_port', safe['routing'])
        self.assertNotIn('controller_secret', safe['routing'])
        self.assertEqual(safe['routing']['physical_interface'], '以太网 e\u0301')

        result = api.Api._routing_result_for_ui({
            'config': value['routing'],
            'nested': [{'controller_secret': '不能出现'}],
        })
        self.assertNotIn('controller_port', result['config'])
        self.assertNotIn('controller_secret', result['config'])
        self.assertNotIn('controller_secret', result['nested'][0])

    def test_ui_draft_reuses_backend_controller_identity(self):
        target = api.Api.__new__(api.Api)
        target._cfg_get = mock.Mock(return_value={'routing': {
            'controller_port': 23456,
            'controller_secret': '后端私有密钥🇯🇵',
        }})
        merged = target._routing_with_private_fields({
            'enabled': False, 'physical_interface': '以太网',
        })
        self.assertEqual(merged['controller_port'], 23456)
        self.assertEqual(merged['controller_secret'], '后端私有密钥🇯🇵')

    def test_state_stream_bridge_delegates_cursor_and_activity(self):
        target = api.Api.__new__(api.Api)
        target._ui_state_stream = mock.Mock()
        target._ui_state_stream.current.return_value = {'version': 3}
        target._ui_state_stream.wait.return_value = {'version': 4}
        target.routing_telemetry = mock.Mock()

        self.assertEqual(target.get_ui_state_snapshot(), {'version': 3})
        self.assertEqual(target.wait_ui_state(3, 12), {'version': 4})
        self.assertTrue(target.set_routing_telemetry_active(True))
        target._ui_state_stream.wait.assert_called_once_with(3, 12)
        target.routing_telemetry.set_active.assert_called_once_with(True)


if __name__ == '__main__':
    unittest.main()
