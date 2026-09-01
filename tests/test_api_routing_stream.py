# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import api


class ApiRoutingStreamTests(unittest.TestCase):
    def instance(self):
        target = api.Api.__new__(api.Api)
        target.routing = mock.Mock()
        target._cfg_get = mock.Mock(return_value={
            'routing': {'controller_secret': '不应进入日志🇯🇵'},
        })
        target.log = mock.Mock()
        target._routing_stream_state = ''
        target._routing_observability_available = None
        return target

    def test_observability_returns_only_manager_summary(self):
        target = self.instance()
        summary = {
            'available': True, 'active': 2, 'upload': 10,
            'download': 20, 'rule_hits': [],
        }
        target.routing.connection_observability.return_value = summary

        result = target.get_routing_observability()

        self.assertEqual(result, {'ok': True, 'observability': summary})
        target.routing.connection_observability.assert_called_once_with(
            {'controller_secret': '不应进入日志🇯🇵'})
        logged = ' '.join(call.args[0] for call in target.log.call_args_list)
        self.assertNotIn('不应进入日志', logged)
        self.assertNotIn('controller_secret', logged)

    def test_stream_state_is_whitelisted_deduplicated_and_bounded(self):
        target = self.instance()

        self.assertFalse(target.report_routing_stream_state('token=bad'))
        self.assertTrue(target.report_routing_stream_state('reconnecting', 99))
        self.assertTrue(target.report_routing_stream_state('reconnecting', 99))

        target.log.assert_called_once_with(
            '[routing-stream] 实时流量通道状态=reconnecting，15 秒后重试')


if __name__ == '__main__':
    unittest.main()
