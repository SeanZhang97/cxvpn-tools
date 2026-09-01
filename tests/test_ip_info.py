import unittest
from unittest import mock

from core import ip_info


class IpInfoTests(unittest.TestCase):
    def test_parse_domestic_ipip_payload(self):
        result = ip_info._parse_domestic({
            'data': {
                'ip': '171.212.136.186',
                'location': ['中国', '四川', '成都市', '中国电信'],
            }
        })
        self.assertTrue(result['ok'])
        self.assertEqual(result['ip'], '171.212.136.186')
        self.assertEqual(result['location'], '中国 四川 成都市')
        self.assertEqual(result['provider'], '中国电信')

    @mock.patch('core.ip_info._request_json')
    def test_domestic_bypasses_proxy_and_follows_default_route(self, request):
        request.return_value = {
            'data': {
                'ip': '1.2.3.4',
                'location': ['中国', '北京', '北京市', '中国联通'],
            }
        }

        result = ip_info.query_domestic()

        self.assertEqual(result['location'], '中国 北京 北京市')
        request.assert_called_once_with(
            'https://myip.ipip.net/json', bypass_proxy=True)

    def test_parse_overseas_payload(self):
        result = ip_info._parse_overseas({
            'ip': '133.18.42.132',
            'country_name': 'Japan',
            'region': 'Aichi',
            'city': 'Nagoya',
            'org': 'KAGOYA JAPAN Inc.',
        })
        self.assertTrue(result['ok'])
        self.assertEqual(result['location'], 'Japan · Aichi · Nagoya')
        self.assertEqual(result['provider'], 'KAGOYA JAPAN Inc.')

    def test_parse_cloudflare_trace(self):
        result = ip_info._parse_cloudflare_trace(
            'ip=171.212.137.219\nloc=CN\ncolo=LAX\n')
        self.assertTrue(result['ok'])
        self.assertEqual(result['ip'], '171.212.137.219')
        self.assertEqual(result['location'], 'CN')
        self.assertEqual(result['detail'], 'LAX')

    def test_parse_cloudflare_trace_rejects_ipv6(self):
        with self.assertRaisesRegex(ValueError, '有效 IPv4'):
            ip_info._parse_cloudflare_trace('ip=2001:db8::1\nloc=US\n')

    @mock.patch('core.ip_info._system_proxy_for_active_tunnel',
                return_value=False)
    @mock.patch('core.ip_info._request_json')
    @mock.patch('core.ip_info._request_text')
    def test_overseas_uses_direct_trace_and_explicit_ip_metadata(
            self, request_text, request_json, _proxy_mode):
        request_text.return_value = (
            'ip=171.212.137.219\nloc=CN\ncolo=LAX\n')
        request_json.return_value = {
            'ip': '171.212.137.219',
            'country_name': 'China',
            'region': 'Sichuan',
            'city': 'Chengdu',
            'org': 'Chinanet',
        }

        result = ip_info.query_overseas()

        self.assertEqual(result['ip'], '171.212.137.219')
        self.assertEqual(result['location'], 'China · Sichuan · Chengdu')
        self.assertEqual(result['provider'], 'Chinanet')
        request_text.assert_called_once_with(
            ip_info._OVERSEAS_TRACE_URLS[0], bypass_proxy=True)
        request_json.assert_called_once_with(
            'https://ipapi.co/171.212.137.219/json/')

    @mock.patch('core.ip_info._system_proxy_for_active_tunnel',
                return_value=False)
    @mock.patch('core.ip_info._request_json')
    @mock.patch('core.ip_info._request_text')
    def test_overseas_keeps_ip_when_metadata_fails(
            self, request_text, request_json, _proxy_mode):
        request_text.return_value = 'ip=203.0.113.9\nloc=JP\ncolo=NRT\n'
        request_json.side_effect = OSError('metadata unavailable')

        result = ip_info.query_overseas()

        self.assertTrue(result['ok'])
        self.assertEqual(result['ip'], '203.0.113.9')
        self.assertEqual(result['location'], 'JP')
        self.assertIn('位置查询失败', result['error'])

    @mock.patch('core.ip_info._system_proxy_for_active_tunnel',
                return_value=False)
    @mock.patch('core.ip_info._request_json')
    @mock.patch('core.ip_info._request_text')
    def test_overseas_falls_back_to_second_trace_host(
            self, request_text, request_json, _proxy_mode):
        request_text.side_effect = [
            OSError('primary unavailable'),
            'ip=198.51.100.8\nloc=US\ncolo=LAX\n',
        ]
        request_json.return_value = {
            'ip': '198.51.100.8', 'country_name': 'United States',
            'region': 'California', 'city': 'Los Angeles',
            'org': 'Example ISP',
        }

        result = ip_info.query_overseas()

        self.assertEqual(result['ip'], '198.51.100.8')
        self.assertEqual(request_text.call_count, 2)
        self.assertEqual(
            request_text.call_args_list[1],
            mock.call(ip_info._OVERSEAS_TRACE_URLS[1], bypass_proxy=True))

    @mock.patch('core.ip_info._system_proxy_for_active_tunnel',
                return_value=True)
    @mock.patch('core.ip_info._request_json')
    @mock.patch('core.ip_info._request_text')
    def test_overseas_uses_system_proxy_for_active_tunnel(
            self, request_text, request_json, _proxy_mode):
        request_text.return_value = 'ip=133.18.42.132\nloc=JP\ncolo=NRT\n'
        request_json.return_value = {
            'ip': '133.18.42.132', 'country_name': 'Japan',
            'region': 'Wakayama', 'city': 'Kinokawa',
            'org': 'KAGOYA JAPAN Inc.',
        }

        result = ip_info.query_overseas()

        self.assertEqual(result['ip'], '133.18.42.132')
        self.assertEqual(result['location'], 'Japan · Wakayama · Kinokawa')
        request_text.assert_called_once_with(
            ip_info._OVERSEAS_TRACE_URLS[0], bypass_proxy=False)

    @mock.patch('core.ip_info.vpn_os._ps')
    @mock.patch('core.ip_info.urllib.request.getproxies')
    def test_active_tunnel_enables_configured_system_proxy(
            self, getproxies, powershell):
        getproxies.return_value = {'https': 'http://127.0.0.1:7892'}
        powershell.return_value = (True, '{"active":true}', '')

        self.assertTrue(ip_info._system_proxy_for_active_tunnel())
        self.assertEqual(powershell.call_args.kwargs['timeout'], 8)

    @mock.patch('core.ip_info.vpn_os._ps')
    @mock.patch('core.ip_info.urllib.request.getproxies')
    def test_residual_proxy_is_ignored_without_active_tunnel(
            self, getproxies, powershell):
        getproxies.return_value = {'https': 'http://127.0.0.1:7892'}
        powershell.return_value = (True, '{"active":false}', '')

        self.assertFalse(ip_info._system_proxy_for_active_tunnel())

    @mock.patch('core.ip_info.vpn_os._ps')
    def test_query_local_uses_default_route_adapter(self, powershell):
        powershell.return_value = (
            True, '{"ip":"192.168.1.113","adapter":"以太网"}', '')
        result = ip_info.query_local()
        self.assertTrue(result['ok'])
        self.assertEqual(result['ip'], '192.168.1.113')
        self.assertEqual(result['detail'], '以太网')
        self.assertEqual(powershell.call_args.kwargs['timeout'], 8)

    @mock.patch('core.ip_info.query_overseas')
    @mock.patch('core.ip_info.query_domestic')
    @mock.patch('core.ip_info.query_local')
    def test_collect_keeps_three_independent_results(
            self, local, domestic, overseas):
        local.return_value = {'ok': True, 'ip': '192.168.1.2'}
        domestic.return_value = {'ok': False, 'error': 'timeout'}
        overseas.return_value = {'ok': True, 'ip': '203.0.113.9'}
        result = ip_info.collect_ip_info()
        self.assertTrue(result['local']['ok'])
        self.assertFalse(result['domestic']['ok'])
        self.assertTrue(result['overseas']['ok'])


if __name__ == '__main__':
    unittest.main()
