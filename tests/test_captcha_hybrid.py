# -*- coding: utf-8 -*-
import io
import json
import unittest
from unittest import mock

from core import captcha_handler


class CaptchaHybridTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {'captcha_max_attempts': 1, 'vlm': {
            'base': 'https://example.test/v1',
            'key': 'secret',
            'model': 'vision'}}

    def _run(self, hybrid, vlm):
        logs = []
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
                _wait_result=mock.DEFAULT,
                _refresh=mock.DEFAULT):
            captcha_handler.get_sprite_bytes.return_value = b'sprite'
            captcha_handler.get_tip_icons.return_value = [b'a', b'b', b'c']
            captcha_handler.compose_tip_strip.return_value = b'tip'
            captcha_handler._save_sprite.return_value = 'saved.jpg'
            captcha_handler.hybrid_solve.return_value = hybrid
            captcha_handler.vlm_solve.return_value = vlm
            captcha_handler._wait_result.return_value = 'pass'
            with mock.patch.object(captcha_handler.time, 'sleep'):
                result = captcha_handler.handle_captcha(
                    object(), self.cfg, log=logs.append, manual_timeout=0,
                    manual_provider=lambda *_args: None)
            return (result, logs, captcha_handler.hybrid_solve,
                    captcha_handler.vlm_solve,
                    captcha_handler._click_sprite)

    def test_hybrid_consensus_result_skips_endpoint_path(self):
        hybrid = [(10, 20), (30, 40), (50, 60)]
        result, _, hybrid_mock, vlm_mock, click_mock = self._run(hybrid, None)

        self.assertTrue(result)
        hybrid_mock.assert_called_once()
        vlm_mock.assert_not_called()
        self.assertEqual(3, click_mock.call_count)

    def test_missing_hybrid_consensus_uses_endpoint_result(self):
        vlm = [(12, 21), (29, 42), (52, 59)]
        result, logs, _, vlm_mock, click_mock = self._run(None, vlm)

        self.assertTrue(result)
        vlm_mock.assert_called_once()
        self.assertEqual([mock.call(mock.ANY, 12, 21, 1),
                          mock.call(mock.ANY, 29, 42, 2),
                          mock.call(mock.ANY, 52, 59, 3)],
                         click_mock.call_args_list)
        self.assertTrue(any('回退端到端' in line for line in logs))

    def test_ai_order_rejects_duplicate_or_out_of_range_candidates(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def response(order, targets=None, candidates=None):
            content = {'order': order}
            if targets is not None:
                content['target_names'] = targets
                content['candidate_names'] = candidates
            payload = {'choices': [{'message': {
                'content': json.dumps(content)}}]}
            return Response(json.dumps(payload).encode())

        guides = [(b'png', 'png')] * 3
        with mock.patch.object(captcha_handler.urllib.request, 'urlopen',
                               return_value=response([1, 1, 2])):
            self.assertIsNone(captcha_handler._ai_name_order(
                'data:image/png;base64,AA==', guides, self.cfg['vlm'],
                log=None, candidate_count=3))
        with mock.patch.object(captcha_handler.urllib.request, 'urlopen',
                               return_value=response([1, 2, 4])):
            self.assertIsNone(captcha_handler._ai_name_order(
                'data:image/png;base64,AA==', guides, self.cfg['vlm'],
                log=None, candidate_count=3))
        with mock.patch.object(
                captcha_handler.urllib.request, 'urlopen',
                return_value=response(
                    [1, 2, 3], ['car', 'camera', 'moon'],
                    {'1': 'cross', '2': 'camera', '3': 'moon'})):
            self.assertIsNone(captcha_handler._ai_name_order(
                'data:image/png;base64,AA==', guides, self.cfg['vlm'],
                log=None, candidate_count=3, require_name_match=True))

    def test_hybrid_uses_semantically_validated_candidate_order(self):
        comps = [
            {'x': 10, 'y': 20}, {'x': 30, 'y': 40}, {'x': 50, 'y': 60}]
        with mock.patch.multiple(
                captcha_handler,
                _extract_hybrid_candidates=mock.DEFAULT,
                _compose_candidate_gallery=mock.DEFAULT,
                _build_raw_hybrid_guides=mock.DEFAULT,
                _ai_name_order=mock.DEFAULT,
                _hybrid_shape_matrix=mock.DEFAULT):
            captcha_handler._extract_hybrid_candidates.return_value = comps
            captcha_handler._compose_candidate_gallery.return_value = 'gallery'
            captcha_handler._build_raw_hybrid_guides.return_value = [(b'b', 'png')] * 3
            captcha_handler._ai_name_order.return_value = ['2', '1', '3']
            captcha_handler._hybrid_shape_matrix.return_value = [
                [0.1, 0.8, 0.2], [0.7, 0.2, 0.1], [0.2, 0.1, 0.9]]
            self.assertEqual([(30, 40), (10, 20), (50, 60)],
                             captcha_handler.hybrid_solve(
                                 object(), b'sprite', None, self.cfg['vlm'],
                                 log=None, tries=1))

    def test_click_sprite_supports_playwright_and_pageshim(self):
        calls = []

        def native_click(_self, selector, position=None):
            calls.append(('native', selector, position))

        NativePage = type('NativePage', (), {'click': native_click})
        NativePage.__module__ = 'playwright.sync_api._generated'

        class ShimPage:
            def click(self, selector, position=None, mark=None):
                calls.append(('shim', selector, position, mark))

        with mock.patch.object(captcha_handler.time, 'sleep'):
            captcha_handler._click_sprite(NativePage(), 10, 20, mark=1)
            captcha_handler._click_sprite(ShimPage(), 30, 40, mark=2)

        self.assertEqual(
            [('native', '#cx_imgBg', {'x': 10, 'y': 20}),
             ('shim', '#cx_imgBg', {'x': 30, 'y': 40}, 2)], calls)


if __name__ == '__main__':
    unittest.main()
