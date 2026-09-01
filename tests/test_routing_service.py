# -*- coding: utf-8 -*-
import base64
import os
import tempfile
import unittest

from core import routing_service


class RoutingServiceClientTests(unittest.TestCase):
    def test_current_user_sid_is_valid_for_pipe_acl(self):
        sid = routing_service.current_user_sid()

        self.assertTrue(sid.startswith('S-1-'))
        self.assertTrue(all(char.isdigit() or char in '-S' for char in sid))

    def test_apply_transfers_config_and_provider_with_hashes(self):
        client = routing_service.RoutingServiceClient('service.exe')
        captured = {}

        def request(payload, **_kwargs):
            captured.update(payload)
            return {'transaction_id': 'tx'}

        client.request = request
        with tempfile.TemporaryDirectory() as root:
            config = os.path.join(root, 'config.json')
            provider = os.path.join(root, 'provider.yaml')
            with open(config, 'wb') as stream:
                stream.write(b'{"mode":"rule"}')
            with open(provider, 'wb') as stream:
                stream.write(b'proxies: []')

            result = client.apply(
                config,
                [{'name': 'provider-alpha.yaml', 'path': provider}],
                system_proxy_bypass_domains=[
                    'chaoxing.com', 'dashscope.aliyuncs.com'])

        self.assertEqual(result['transaction_id'], 'tx')
        self.assertEqual(captured['op'], 'apply')
        self.assertEqual(
            base64.b64decode(captured['config_b64']), b'{"mode":"rule"}')
        self.assertEqual(
            base64.b64decode(captured['providers'][0]['content_b64']),
            b'proxies: []')
        self.assertEqual(len(captured['config_sha256']), 64)
        self.assertEqual(len(captured['providers'][0]['sha256']), 64)
        self.assertEqual(captured['runtime_mode'], 'active')
        self.assertFalse(captured['fast_toggle_ready'])
        self.assertEqual(captured['system_proxy_bypass_domains'], [
            'chaoxing.com', 'dashscope.aliyuncs.com'])

    def test_apply_supports_standby_and_provider_cache_round_trip(self):
        client = routing_service.RoutingServiceClient('service.exe')
        captured = []

        def request(payload, **_kwargs):
            captured.append(payload)
            if payload['op'] == 'read_provider':
                return {
                    'content_b64': base64.b64encode(
                        'proxies:\n  - name: 日本 🇯🇵\n'.encode('utf-8')
                    ).decode('ascii'),
                    'modified_at': 1_800_000_000,
                }
            return {'transaction_id': 'standby-tx'}

        client.request = request
        with tempfile.TemporaryDirectory() as root:
            config = os.path.join(root, 'config.json')
            with open(config, 'wb') as stream:
                stream.write(b'{"mode":"rule"}')
            client.apply(config, [], runtime_mode='standby')
        cache = client.read_provider('provider-alpha.yaml')

        self.assertEqual(captured[0]['runtime_mode'], 'standby')
        self.assertEqual(captured[0]['system_proxy_bypass_domains'], [])
        self.assertEqual(captured[1], {
            'op': 'read_provider', 'provider_name': 'provider-alpha.yaml'})
        self.assertIn('日本 🇯🇵', cache['content'].decode('utf-8'))
        self.assertEqual(cache['modified_at'], 1_800_000_000)

    def test_compatible_requires_protocol_version_and_binary_hash(self):
        with tempfile.TemporaryDirectory() as root:
            binary = os.path.join(root, 'service.exe')
            with open(binary, 'wb') as stream:
                stream.write(b'service-v1')
            client = routing_service.RoutingServiceClient(binary)
            digest = routing_service.sha256_file(binary)
            self.assertTrue(client._compatible({
                'protocol_version': routing_service.PROTOCOL_VERSION,
                'service_version': routing_service.SERVICE_VERSION,
                'service_sha256': digest,
            }))
            self.assertFalse(client._compatible({
                'protocol_version': routing_service.PROTOCOL_VERSION,
                'service_version': routing_service.SERVICE_VERSION,
                'service_sha256': '0' * 64,
            }))

    def test_activate_system_proxy_uses_utf8_transaction_id(self):
        client = routing_service.RoutingServiceClient('service.exe')
        captured = {}
        client.request = lambda payload, **_kwargs: captured.update(payload) or {}
        client.activate_system_proxy('事务-🇨🇳')
        self.assertEqual(captured, {
            'op': 'activate_system_proxy', 'transaction_id': '事务-🇨🇳'})

    def test_fast_system_proxy_toggle_uses_boolean_protocol_field(self):
        client = routing_service.RoutingServiceClient('service.exe')
        captured = []
        client.request = lambda payload, **_kwargs: captured.append(payload) or {}

        client.set_system_proxy_enabled(True)
        client.set_system_proxy_enabled(False)

        self.assertEqual(captured, [
            {'op': 'set_system_proxy_enabled', 'enabled': True},
            {'op': 'set_system_proxy_enabled', 'enabled': False},
        ])


if __name__ == '__main__':
    unittest.main()
