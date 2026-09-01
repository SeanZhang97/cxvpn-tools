# -*- coding: utf-8 -*-
import unittest
import urllib.error
import io
from unittest import mock

from core import captcha_handler


class CaptchaManualFallbackTest(unittest.TestCase):
    def test_vlm_requires_all_three_fields(self):
        self.assertFalse(captcha_handler.vlm_configured({'vlm': {}}))
        self.assertFalse(captcha_handler.vlm_configured({
            'vlm': {'base': 'https://example.test/v1', 'key': '',
                    'model': 'vision'}}))
        self.assertTrue(captcha_handler.vlm_configured({
            'vlm': {'base': 'https://example.test/v1', 'key': 'secret',
                    'model': 'vision'}}))

    def test_missing_vlm_skips_ai_and_opens_manual_flow(self):
        manual_calls = []
        logs = []

        def manual(sprite, timeout, tip):
            manual_calls.append((sprite, timeout, tip))
            return [(10, 20), (30, 40), (50, 60)]

        with mock.patch.multiple(
                captcha_handler,
                _click_retry_if_blocked=mock.DEFAULT,
                get_sprite_bytes=mock.DEFAULT,
                get_tip_icons=mock.DEFAULT,
                _click_sprite=mock.DEFAULT,
                _wait_result=mock.DEFAULT,
                vlm_solve=mock.DEFAULT):
            captcha_handler.get_sprite_bytes.return_value = b'sprite'
            captcha_handler.get_tip_icons.return_value = None
            captcha_handler._wait_result.return_value = 'pass'
            captcha_handler.vlm_solve.side_effect = AssertionError(
                '未配置模型时不应调用 AI')
            vlm_mock = captcha_handler.vlm_solve
            with mock.patch.object(captcha_handler.time, 'sleep'):
                result = captcha_handler.handle_captcha(
                    object(), {'vlm': {'base': '', 'key': '', 'model': ''}},
                    log=logs.append, manual_provider=manual)

        self.assertTrue(result)
        self.assertEqual(1, len(manual_calls))
        self.assertEqual(b'sprite', manual_calls[0][0])
        vlm_mock.assert_not_called()
        self.assertTrue(any('直接转人工' in line for line in logs))

    def test_ai_network_failure_immediately_opens_manual_flow(self):
        logs = []
        manual_calls = []

        def manual(sprite, timeout, tip):
            manual_calls.append((sprite, timeout, tip))
            return [(10, 20), (30, 40), (50, 60)]

        with mock.patch.multiple(
                captcha_handler,
                _click_retry_if_blocked=mock.DEFAULT,
                get_sprite_bytes=mock.DEFAULT,
                get_tip_icons=mock.DEFAULT,
                compose_tip_strip=mock.DEFAULT,
                _save_sprite=mock.DEFAULT,
                hybrid_solve=mock.DEFAULT,
                vlm_solve=mock.DEFAULT,
                _click_sprite=mock.DEFAULT,
                _wait_result=mock.DEFAULT):
            captcha_handler.get_sprite_bytes.return_value = b'sprite'
            captcha_handler.get_tip_icons.return_value = [b'a', b'b', b'c']
            captcha_handler.compose_tip_strip.return_value = b'tip'
            captcha_handler.hybrid_solve.side_effect = (
                captcha_handler.VlmUnavailableError('connection refused'))
            captcha_handler._wait_result.return_value = 'pass'
            hybrid_mock = captcha_handler.hybrid_solve
            vlm_mock = captcha_handler.vlm_solve
            with mock.patch.object(captcha_handler.time, 'sleep'):
                result = captcha_handler.handle_captcha(
                    object(), {'captcha_max_attempts': 10, 'vlm': {
                        'base': 'https://example.test/v1',
                        'key': 'secret', 'model': 'vision'}},
                    log=logs.append, manual_provider=manual)

        self.assertTrue(result)
        self.assertEqual(1, len(manual_calls))
        hybrid_mock.assert_called_once()
        vlm_mock.assert_not_called()
        self.assertTrue(any('立即转人工点选' in line for line in logs))

    def test_vlm_connection_error_is_normalized_for_manual_fallback(self):
        with mock.patch.object(
                captcha_handler.urllib.request, 'urlopen',
                side_effect=urllib.error.URLError('connection refused')):
            with self.assertRaises(captcha_handler.VlmUnavailableError):
                captcha_handler._vlm_call(
                    b'image', {'base': 'https://example.test/v1',
                               'key': 'secret', 'model': 'vision'},
                    timeout=1)

    def test_captcha_download_retries_without_system_proxy(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        opener = mock.Mock()
        opener.open.return_value = Response(b'captcha')
        with mock.patch.object(
                captcha_handler.urllib.request, 'urlopen',
                side_effect=urllib.error.URLError('proxy refused')):
            with mock.patch.object(
                    captcha_handler.urllib.request, 'build_opener',
                    return_value=opener) as build_opener:
                result = captcha_handler._download_url(
                    'https://example.test/captcha.png')

        self.assertEqual(b'captcha', result)
        build_opener.assert_called_once()
        opener.open.assert_called_once_with(mock.ANY, timeout=15)


if __name__ == '__main__':
    unittest.main()
