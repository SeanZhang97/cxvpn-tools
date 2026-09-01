# -*- coding: utf-8 -*-
import tempfile
import unittest
from unittest.mock import patch

from core.worker import Worker, _log_text


class WorkerNetlogTest(unittest.TestCase):
    def test_log_text_keeps_complete_content(self):
        content = 'a' * 400 + '\n  ' + 'b' * 400

        rendered = _log_text(content)

        self.assertEqual(content, rendered)
        self.assertNotIn('…', rendered)

    def test_drain_net_logs_complete_xhr_content(self):
        url = 'https://example.test/' + 'path-' * 40
        request_body = 'request-' * 40
        response_body = 'response-' * 40
        logs = []
        worker = Worker(lambda: {}, logs.append)

        class Browser:
            alive = True

            @staticmethod
            def drain_net():
                return [{'src': 'xhr', 'method': 'POST', 'status': 200,
                         'url': url, 'req': request_body,
                         'res': response_body}]

        worker.browser = Browser()
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch('core.config.BASE', temp_dir):
            worker._drain_net()

        self.assertEqual(
            f'[net] POST 200 {url} 参数={request_body} 返回={response_body}',
            logs[0])
        self.assertNotIn('…', logs[0])


if __name__ == '__main__':
    unittest.main()
