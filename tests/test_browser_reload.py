# -*- coding: utf-8 -*-
import threading
import unittest
from unittest.mock import Mock, patch

from api import Api
from core.browser_win import NativeBrowser, PageShim


class BrowserReloadTests(unittest.TestCase):
    def test_page_reload_uses_native_browser_instead_of_eval(self):
        browser = Mock()
        browser.reload.return_value = True
        page = PageShim(browser)

        result = page.reload()

        self.assertTrue(result)
        browser.reload.assert_called_once_with()
        browser.eval_js.assert_not_called()

    def test_native_reload_waits_for_navigation_completed(self):
        browser = NativeBrowser.__new__(NativeBrowser)
        browser._core = Mock()
        browser._loaded = threading.Event()
        browser.log = Mock()
        browser._ui = lambda fn: fn()
        browser._core.Reload.side_effect = browser._loaded.set

        result = browser.reload(timeout=0.1)

        self.assertTrue(result)
        browser._core.Reload.assert_called_once_with()

    def test_native_reload_reports_timeout(self):
        browser = NativeBrowser.__new__(NativeBrowser)
        browser._core = Mock()
        browser._loaded = Mock()
        browser._loaded.wait.return_value = False
        browser.log = Mock()
        browser._ui = lambda fn: fn()

        result = browser.reload(timeout=0.1)

        self.assertFalse(result)
        browser.log.assert_called_once_with('[browser] 刷新超时 (0.1s)')

    @patch('core.browser_win.time.time', side_effect=[0.0, 1.0])
    def test_eval_stops_when_navigation_destroys_result_slot(self, now):
        browser = NativeBrowser.__new__(NativeBrowser)
        browser.log = Mock()
        browser._exec_raw = Mock(return_value=('ok', None))

        result = browser.eval_js('location.reload()', timeout=0.1)

        self.assertIsNone(result)
        self.assertEqual(browser._exec_raw.call_count, 1)
        browser.log.assert_called_once_with(
            '[browser] eval 超时 (页面导航或异步结果未返回)')

    def test_api_returns_structured_reload_failure(self):
        instance = Api.__new__(Api)
        instance.worker = Mock()
        instance.worker.browser.alive = True
        instance.worker.page.reload.return_value = False

        result = instance.browser_reload()

        self.assertFalse(result['ok'])
        self.assertIn('刷新超时', result['msg'])


if __name__ == '__main__':
    unittest.main()
