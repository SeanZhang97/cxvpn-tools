# -*- coding: utf-8 -*-
import threading
import time
import unittest
from unittest import mock

from core import routing
from core.routing_speedtest import (
    RoutingTestJobs, node_healthcheck_path, test_group, test_nodes)


class RoutingSpeedTestTests(unittest.TestCase):
    def test_default_target_matches_clash_verge(self):
        self.assertEqual(
            routing.HEALTH_CHECK_URL,
            'http://cp.cloudflare.com/generate_204')

    def test_provider_node_uses_provider_healthcheck_endpoint(self):
        path = node_healthcheck_path({
            'name': '[订阅一] 香港/01',
            'provider_name': 'provider-alpha',
        }, 'https://example.test/204')

        self.assertIn(
            '/providers/proxies/provider-alpha/', path)
        self.assertIn('%2F', path)
        self.assertIn('/healthcheck?', path)
        self.assertNotIn('/delay?', path)

    def test_clash_verge_style_provider_field_uses_healthcheck_endpoint(self):
        path = node_healthcheck_path({
            'name': 'Hong Kong 01', 'provider': 'provider-alpha',
        }, 'https://example.test/204')

        self.assertIn('/providers/proxies/provider-alpha/', path)
        self.assertIn('/healthcheck?', path)

    def test_plain_proxy_keeps_legacy_delay_endpoint(self):
        path = node_healthcheck_path(
            {'name': 'DIRECT'}, 'https://example.test/204')

        self.assertTrue(path.startswith('/proxies/DIRECT/delay?'))

    def test_reports_each_node_result_and_skips_metadata(self):
        events = []

        def request(_config, path, **_kwargs):
            return {'delay': 86 if 'Hong' in path else 0}

        results = test_nodes(request, {}, [
            {'name': 'Hong Kong 01', 'display_name': 'Hong Kong 01'},
            {'name': 'Tokyo 01', 'display_name': 'Tokyo 01'},
            {'name': '剩余流量 88 GB', 'display_name': '剩余流量 88 GB'},
        ], 'https://example.test/204', events.append)

        self.assertEqual(set(results), {'Hong Kong 01', 'Tokyo 01'})
        completed = [event['node'] for event in events
                     if event['event'] == 'result']
        self.assertEqual(len(completed), 2)
        self.assertTrue(results['Hong Kong 01']['alive'])
        self.assertFalse(results['Tokyo 01']['alive'])

    def test_real_node_with_traffic_word_is_not_misclassified(self):
        results = test_nodes(
            lambda *_args, **_kwargs: {'delay': 20}, {},
            [{'name': '流量优化专线'}], 'https://example.test/204')

        self.assertIn('流量优化专线', results)

    def test_delay_value_normalizes_numeric_provider_responses(self):
        values = iter(['86.4', True, 'not-a-delay'])

        def request(_config, _path, **_kwargs):
            return {'delay': next(values)}

        results = test_nodes(request, {}, [
            {'name': 'A'}, {'name': 'B'}, {'name': 'C'},
        ], 'https://example.test/204', workers=1)

        self.assertEqual(results['A']['delay'], 86)
        self.assertFalse(results['B']['alive'])
        self.assertEqual(results['C']['delay'], 0)
        self.assertGreaterEqual(results['A']['elapsed_ms'], 0)

    def test_cancelled_group_test_persists_complete_node_list(self):
        nodes = [
            {'name': 'A', 'delay': 0, 'alive': None, 'tested': False},
            {'name': 'B', 'delay': 0, 'alive': None, 'tested': False},
            {'name': 'C', 'delay': 0, 'alive': None, 'tested': False},
        ]
        manager = mock.Mock()
        manager._group_identity.return_value = ('PROXY-alpha', '订阅一', 'select')
        manager.proxy_overview.side_effect = [
            [{'id': 'alpha', 'nodes': nodes}], []]
        partial = {'A': {
            'name': 'A', 'delay': 30, 'alive': True,
            'tested': True, 'tested_at': 1,
        }}
        persist = mock.Mock()

        with mock.patch(
                'core.routing_speedtest.test_nodes', return_value=partial):
            test_group(
                manager, {}, 'alpha', 'https://example.test/204',
                RuntimeError, persist)

        saved = persist.call_args.args[2]
        self.assertEqual([item['name'] for item in saved], ['A', 'B', 'C'])
        self.assertTrue(saved[0]['alive'])
        self.assertIsNone(saved[1]['alive'])

    def test_limits_parallelism_to_eight_workers(self):
        active = 0
        peak = 0
        guard = threading.Lock()

        def request(_config, _path, **_kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return {'delay': 20}

        nodes = [{'name': f'node-{index}'} for index in range(20)]
        test_nodes(request, {}, nodes, 'https://example.test/204', workers=8)

        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 8)

    def test_cancel_returns_only_completed_results_without_starting_more_nodes(self):
        cancel = threading.Event()
        started = threading.Barrier(2)

        def request(_config, _path, **_kwargs):
            started.wait(timeout=1)
            cancel.set()
            return {'delay': 25}

        results = test_nodes(request, {}, [
            {'name': 'A'}, {'name': 'B'}, {'name': 'C'},
        ], 'https://example.test/204', cancel_event=cancel, workers=2)

        self.assertTrue(set(results) <= {'A', 'B'})

    def test_background_job_exposes_incremental_counts(self):
        jobs = RoutingTestJobs()

        def runner(progress, _cancel):
            progress({'event': 'init', 'nodes': [
                {'name': 'A'}, {'name': 'B'}]})
            progress({'event': 'testing', 'name': 'A'})
            progress({'event': 'result', 'node': {
                'name': 'A', 'tested': True, 'alive': True, 'delay': 30}})
            progress({'event': 'result', 'node': {
                'name': 'B', 'tested': True, 'alive': False, 'delay': 0}})
            return {'ok': True}

        started = jobs.start(runner)
        deadline = time.monotonic() + 2
        state = started['job']
        while state['status'] not in {'completed', 'error'}:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
            state = jobs.get(started['job_id'])['job']

        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['completed'], 2)
        self.assertEqual(state['alive'], 1)
        self.assertEqual(state['failed'], 1)


if __name__ == '__main__':
    unittest.main()
