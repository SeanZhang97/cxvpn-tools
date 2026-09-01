# -*- coding: utf-8 -*-
import threading
import time
import unittest
from unittest import mock

from core.ui_state_stream import UiStateStream


class UiStateStreamTests(unittest.TestCase):
    def test_versions_change_only_when_snapshot_changes(self):
        value = {'text': '中文 e\u0301 🇯🇵', 'count': 1}
        stream = UiStateStream(lambda: dict(value), mock.Mock(), interval=0.02)
        stream.start()
        try:
            first = stream.wait(0, 1)
            self.assertTrue(first['changed'])
            version = first['version']
            unchanged = stream.wait(version, 0.05)
            self.assertFalse(unchanged['changed'])
            value['count'] = 2
            stream.poke()
            changed = stream.wait(version, 1)
            self.assertTrue(changed['changed'])
            self.assertGreater(changed['version'], version)
            self.assertEqual(changed['snapshot']['count'], 2)
        finally:
            stream.stop()

    def test_wait_wakes_when_stream_stops(self):
        stream = UiStateStream(lambda: {'ready': True}, mock.Mock(), interval=1)
        stream.start()
        initial = stream.wait(0, 1)
        result = {}

        def waiter():
            result.update(stream.wait(initial['version'], 10))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.03)
        stream.stop()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
