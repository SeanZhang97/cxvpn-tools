# -*- coding: utf-8 -*-
import threading
import time
import unittest
from unittest.mock import patch

from core import config
from core import web_flow
from core.worker import Worker


class RenewCancelTest(unittest.TestCase):
    def test_default_renew_interval_is_seven_hours(self):
        self.assertEqual(config.DEFAULT['renew_hours'], 7)

    @patch('core.worker.time.time', return_value=10_800)
    def test_persisted_authorization_does_not_renew_three_hours_after_start(
            self, now):
        worker = self._worker()
        cfg = {
            'auto_renew': True, 'renew_hours': 7,
            'authorization': {
                'last_success_at': 0.1,
                'expires_at': 28_800,
                'source': 'manual',
            },
        }

        self.assertIsNone(worker._claim_renew(cfg))
        self.assertEqual(worker.state['authorization']['status'], 'valid')

    @patch('core.worker.time.time', return_value=25_201)
    def test_persisted_authorization_renews_at_configured_interval(
            self, now):
        worker = self._worker()
        cfg = {
            'auto_renew': True, 'renew_hours': 7,
            'authorization': {
                'last_success_at': 0.1,
                'expires_at': 28_800,
                'source': 'manual',
            },
        }

        self.assertEqual(worker._claim_renew(cfg), 'automatic')

    @patch('core.worker.time.time', return_value=25_201)
    def test_unknown_authorization_does_not_trigger_startup_renewal(self, now):
        worker = self._worker()

        self.assertIsNone(worker._claim_renew({
            'auto_renew': True, 'renew_hours': 7,
            'authorization': {},
        }))

    @patch('core.worker.time.time', return_value=1_000)
    def test_authorization_result_persists_server_expiry(self, now):
        saved = []
        worker = Worker(lambda: {}, lambda _msg: None,
                        authorization_save=saved.append)

        result = worker._save_authorization_result(
            {'11': 20_000, '12': 19_000}, 'manual')

        self.assertEqual(result['expires_at'], 19_000)
        self.assertEqual(result['last_success_at'], 1_000)
        self.assertEqual(saved[0]['vpn_expiries']['11'], 20_000)

    def test_web_flow_extracts_only_authoritative_expiry_map(self):
        result = web_flow._response_expiries({
            'code': 200,
            'data': {
                'expTime': {'11': '2026-08-29 18:55:32'},
                'vpnPubIPAndExpTime': {'sensitive-key': 'ignored'},
            },
        })

        self.assertEqual(result, {'11': '2026-08-29 18:55:32'})

    def test_worker_can_stop_and_join_cleanly(self):
        worker = Worker(
            lambda: {'auto_renew': False, 'vpn_name': ''},
            lambda _message: None)

        worker.start()
        worker.stop()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())

    def _worker(self):
        return Worker(lambda: {}, lambda _msg: None)

    def test_queued_renew_can_be_cancelled(self):
        worker = self._worker()

        self.assertTrue(worker.request_renew('manual'))
        self.assertFalse(worker.request_renew('manual'))
        self.assertTrue(worker.cancel_renew())

        self.assertFalse(worker.renew_requested.is_set())
        self.assertFalse(worker.state['renewing'])
        self.assertEqual('cancelled',
                         worker.state['browser_progress']['status'])
        self.assertGreater(worker._renew_retry_after, time.time())

    def test_running_renew_receives_cancel_signal(self):
        worker = self._worker()
        self.assertTrue(worker.request_renew('manual'))
        source = worker._claim_renew(
            {'auto_renew': False, 'renew_hours': 7})

        self.assertEqual('manual', source)
        self.assertTrue(worker.state['renewing'])
        self.assertTrue(worker.cancel_renew())
        self.assertTrue(worker.renew_cancel_requested.is_set())
        self.assertTrue(worker.state['renew_cancel_pending'])

        worker._finish_renew()
        self.assertFalse(worker.state['renewing'])
        self.assertFalse(worker.renew_cancel_requested.is_set())

    def test_web_flow_stops_before_navigation_when_cancelled(self):
        class Page:
            called = False

            def goto(self, *_args, **_kwargs):
                self.called = True

        page = Page()
        cancel = threading.Event()
        cancel.set()

        with self.assertRaises(web_flow.RenewCancelled):
            web_flow.ensure_authorized(
                page, {'phone': '13800000000'},
                cancel=cancel.is_set)
        self.assertFalse(page.called)


if __name__ == '__main__':
    unittest.main()
