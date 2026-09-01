# -*- coding: utf-8 -*-
import threading
import unittest

from core.browser_win import INSTALL_JS, NativeBrowser


class _FakeCtrl:
    def __init__(self):
        self.scripts = []

    def ExecuteScriptAsync(self, script):
        self.scripts.append(script)
        return None


class BrowserOverlayTest(unittest.TestCase):
    def _browser(self):
        browser = NativeBrowser.__new__(NativeBrowser)
        browser.ctrl = object()
        browser.alive = True
        browser._overlay_lock = threading.Lock()
        browser._overlay_payload = None
        browser._loaded = threading.Event()
        browser.logs = []
        browser.log = browser.logs.append
        return browser

    def test_update_reinjects_when_panel_is_missing(self):
        browser = self._browser()
        calls = []

        def execute(script, timeout):
            calls.append((script, timeout))
            return ('ok', False) if len(calls) == 1 else ('ok', True)

        browser._exec_raw = execute
        browser.update_overlay(
            {'step': 3, 'status': 'running', 'title': '等待验证'},
            [{'method': 'GET', 'url': 'https://example.test'}], 1)

        self.assertEqual(2, len(calls))
        self.assertIn('window.__cxPanel ?', calls[0][0])
        self.assertIn('cx-manager-overlay', calls[1][0])
        self.assertEqual(3, browser._overlay_payload['progress']['step'])
        self.assertEqual([], browser.logs)

    def test_navigation_reinjects_and_restores_cached_state(self):
        browser = self._browser()
        browser.ctrl = _FakeCtrl()
        browser._overlay_payload = {
            'progress': {'step': 4, 'title': '验证授权状态'},
            'rows': [],
            'net_count': 0,
        }

        browser._on_nav_completed(None, None)

        self.assertTrue(browser._loaded.is_set())
        self.assertEqual(1, len(browser.ctrl.scripts))
        self.assertIn('cx-manager-overlay', browser.ctrl.scripts[0])
        self.assertIn('window.__cxPanel.update', browser.ctrl.scripts[0])
        self.assertEqual([], browser.logs)

    def test_overlay_owns_dark_scrollbar_theme(self):
        from core.browser_win import OVERLAY_JS

        self.assertIn('.net-table::-webkit-scrollbar-thumb', OVERLAY_JS)
        self.assertIn('.net-detail::-webkit-scrollbar-thumb', OVERLAY_JS)
        self.assertIn('::-webkit-scrollbar-button', OVERLAY_JS)
        self.assertIn('scrollbar-color:', OVERLAY_JS)

    def test_overlay_collapses_progress_on_narrow_viewports(self):
        from core.browser_win import OVERLAY_JS

        self.assertIn('@media (max-width: 1100px)', OVERLAY_JS)
        self.assertIn('function syncProgressDensity()', OVERLAY_JS)
        self.assertIn("window.innerWidth <= 1100", OVERLAY_JS)
        self.assertIn('progressManuallyToggled', OVERLAY_JS)
        self.assertIn("window.addEventListener('resize', syncProgressDensity)", OVERLAY_JS)

    def test_overlay_preserves_open_request_details_across_updates(self):
        from core.browser_win import OVERLAY_JS

        self.assertIn('let openDetailKeys = new Set()', OVERLAY_JS)
        self.assertIn('let detailScrollTops = new Map()', OVERLAY_JS)
        self.assertIn('let renderedRowsSignature = null', OVERLAY_JS)
        self.assertIn('const netRowSignature = r =>', OVERLAY_JS)
        self.assertIn("openDetailKeys.has(rowKey)", OVERLAY_JS)
        self.assertIn("openDetailKeys.add(rowKey)", OVERLAY_JS)
        self.assertIn("detail.addEventListener('scroll'", OVERLAY_JS)
        self.assertIn('detail.scrollTop = detailScrollTops.get(rowKey)', OVERLAY_JS)
        self.assertIn('anchor.offsetTop - anchorOffset', OVERLAY_JS)
        self.assertIn('if (signature === renderedRowsSignature) return', OVERLAY_JS)
        self.assertIn('overflow-anchor: none', OVERLAY_JS)

    def test_overlay_drawer_supports_bounded_vertical_resize(self):
        from core.browser_win import OVERLAY_JS

        self.assertIn('--drawer-height: 186px', OVERLAY_JS)
        self.assertIn('max-height: 70vh', OVERLAY_JS)
        self.assertIn('role="separator"', OVERLAY_JS)
        self.assertIn('resizeHandle.onpointerdown', OVERLAY_JS)
        self.assertIn('resizeHandle.onpointermove', OVERLAY_JS)
        self.assertIn("e.key === 'ArrowUp'", OVERLAY_JS)

    def test_page_capture_normalizes_fetch_and_xhr_urls(self):
        self.assertIn('const absoluteUrl = u =>', INSTALL_JS)
        self.assertIn(
            "const url = absoluteUrl(typeof input === 'string' ? input : input.url)",
            INSTALL_JS)
        self.assertIn('u = absoluteUrl(uu)', INSTALL_JS)

    def test_page_capture_keeps_complete_text_bodies(self):
        self.assertIn("if (typeof b === 'string') return b", INSTALL_JS)
        self.assertIn('rt = await res.clone().text()', INSTALL_JS)
        self.assertNotIn('.slice(0, 3000)', INSTALL_JS)
        self.assertNotIn('String(v).slice', INSTALL_JS)


if __name__ == '__main__':
    unittest.main()
