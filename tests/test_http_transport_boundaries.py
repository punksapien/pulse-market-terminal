"""ADR-0035: offline HTTP contracts run before and after transport extraction."""
import io
import ast
import inspect
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from pulse.legacy import service
from pulse.api import handlers as http_handlers
from tests.api_boundary_parity import METHOD_BINDINGS, original_definition


def request(path, method='GET', *, headers=None, body=b''):
    receiver = service.handler.__new__(service.handler)
    receiver.path = path
    receiver.headers = headers or {}
    receiver.rfile, receiver.wfile = io.BytesIO(body), io.BytesIO()
    receiver.send_response = Mock()
    receiver.send_header = Mock()
    receiver.end_headers = Mock()
    getattr(receiver, 'do_'+method)()
    return receiver, receiver.send_response.call_args.args[0], json.loads(receiver.wfile.getvalue())


class HttpTransportBoundaryTests(unittest.TestCase):
    def test_transport_functions_work_without_service_handler(self):
        receiver = SimpleNamespace(path='/api/health', _send_json=Mock())
        health = Mock(return_value={'healthy': True, 'freshness_status': 'fresh'})
        with patch.dict('os.environ', {'VERCEL': '1'}):
            http_handlers.do_GET(receiver, health_snapshot=health, authorise=Mock(),
                                 storage=Mock(), cache_state={}, shell=Mock(), render=Mock())
        health.assert_called_once_with(verify_reports=True)
        self.assertEqual(receiver._send_json.call_args.args[0], 200)
        self.assertEqual(receiver._send_json.call_args.args[1]['status'], 'ok')
        receiver = SimpleNamespace(wfile=io.BytesIO(), send_response=Mock(), send_header=Mock(), end_headers=Mock())
        serialise = Mock(side_effect=lambda value: value)
        http_handlers._send_json(receiver, 201, {'test': True}, serialise=serialise)
        self.assertEqual(receiver.wfile.getvalue(), b'{"test":true}')
        receiver.send_response.assert_called_once_with(201)
        self.assertEqual(receiver.send_header.call_args_list[0].args,
                         ('Content-Type', 'application/json; charset=utf-8'))

    def test_wrappers_inject_current_dependencies(self):
        receiver = service.handler.__new__(service.handler)
        with patch.object(service, '_generation_health_snapshot') as health, \
                patch.object(http_handlers, 'do_GET') as implementation:
            self.assertIs(receiver.do_GET(), implementation.return_value)
        self.assertIs(implementation.call_args.args[0], receiver)
        self.assertIs(implementation.call_args.kwargs['health_snapshot'], health)
        with patch.object(service, 'VercelStorage') as storage, patch.object(http_handlers, 'do_POST') as implementation:
            self.assertIs(receiver.do_POST(), implementation.return_value)
        self.assertIs(implementation.call_args.kwargs['storage'], storage)

    def test_static_helper_parity_cannot_hide_an_added_decorator(self):
        canonical = ast.parse(inspect.getsource(http_handlers._report_headers)).body[0]
        canonical.decorator_list = [ast.Name(id='unexpected', ctx=ast.Load())]
        with self.assertRaisesRegex(AssertionError, 'undecorated'):
            original_definition(canonical, METHOD_BINDINGS['_report_headers'])

    def test_health_http_status_is_independent_of_local_fallback(self):
        for vercel in ['1', '0']:
            for freshness in ['fresh', 'stale_recoverable', 'expired', 'unavailable']:
                healthy = freshness == 'fresh'
                with self.subTest(vercel=vercel, freshness=freshness), patch.dict('os.environ', {'VERCEL': vercel}), \
                        patch.object(service, '_generation_health_snapshot', return_value={'healthy': healthy, 'freshness_status': freshness}):
                    _, status, result = request('/api/health')
                self.assertEqual(status, 200 if healthy or vercel == '0' else 503)
                self.assertEqual(result['status'], 'ok' if healthy else 'degraded' if freshness == 'stale_recoverable' else 'critical')

    def test_cron_auth_and_result_status_mapping(self):
        for outcome, expected in [({'published': True}, 200), ({'status': 'fresh_noop'}, 200),
                                  ({'status': 'locked'}, 202), ({'status': 'publish_failed'}, 500)]:
            with self.subTest(outcome=outcome), patch.dict('os.environ', {'CRON_SECRET': 'test-only'}, clear=True), \
                    patch.object(service.handler, 'warm_all_combinations', return_value=outcome) as warm:
                _, status, result = request('/api/cron?force=1', headers={'Authorization': 'Bearer test-only'})
            self.assertEqual(status, expected)
            self.assertEqual(result, outcome)
            warm.assert_called_once_with(trigger='authenticated_cron', force=True)
        with patch.dict('os.environ', {'VERCEL': '1'}, clear=True), patch.object(service.handler, 'warm_all_combinations') as warm:
            _, status, result = request('/api/cron')
        self.assertEqual(status, 503)
        self.assertEqual(result['error'], 'missing_secret')
        warm.assert_not_called()

    def test_admin_uses_bounded_scan_batches_and_clears_only_supplied_memory(self):
        client = Mock()
        client.scan_iter.side_effect = lambda **kw: iter([f'{kw["match"]}:{i}' for i in range(201)])
        client.delete.side_effect = lambda *keys: len(keys)
        storage = Mock()
        storage.get_client.return_value = client
        state = {'store': {'existing': 1}, 'lock': threading.Lock()}
        with patch.dict('os.environ', {'ADMIN_SECRET': 'test-admin'}, clear=True), \
                patch.object(service, 'VercelStorage', storage), patch.object(service, '_CACHE', state):
            _, status, result = request('/api?clear_cache=1', headers={'Authorization': 'Bearer test-admin'})
        self.assertEqual(status, 200)
        self.assertIn('804 keys', result['message'])
        self.assertEqual(state['store'], {})
        self.assertEqual([call.kwargs['match'] for call in client.scan_iter.call_args_list],
                         ['base_*', 'USDT_*', 'vol_lock_*', 'vol:v2:*'])
        self.assertEqual([len(call.args) for call in client.delete.call_args_list], [100, 100, 1]*4)

    def test_admin_unavailable_or_failed_storage_preserves_results(self):
        for failed, expected in [(False, 200), (True, 500)]:
            client = Mock()
            client.scan_iter.side_effect = RuntimeError('fixture')
            storage = Mock()
            storage.get_client.return_value = client if failed else None
            with self.subTest(failed=failed), patch.dict('os.environ', {'ADMIN_SECRET': 'test-admin'}, clear=True), \
                    patch.object(service, 'VercelStorage', storage):
                _, status, result = request('/api?clear_cache=1', headers={'Authorization': 'Bearer test-admin'})
            self.assertEqual(status, expected)
            self.assertEqual(result['status'], 'error' if failed else 'ok')

    def test_retry_after_and_freshness_headers(self):
        error = service.ReportUnavailableError('expired', {'backoff': {'retry_after': 1061}})
        with patch.object(service.time, 'time', return_value=1000), \
                patch.object(service.handler, '_get_report_with_auto_heal', side_effect=error):
            receiver, status, result = request('/api?json=1')
        self.assertEqual(status, 503)
        self.assertEqual(result['retry_after_seconds'], 61)
        self.assertIn(('Retry-After', '61'), [call.args for call in receiver.send_header.call_args_list])
        headers = service.handler._report_headers({'meta': {'freshness_status': 'stale_revalidating', 'generation_id': 'g'}}, {'status': 'locked'})
        self.assertEqual(headers['X-Volatility-Auto-Heal'], 'locked')
        self.assertEqual(headers['Warning'], '110 - "Volatility model refresh in progress"')

    def test_telemetry_tmp_fallback_retains_cap_and_unknown_route(self):
        payload = {'event': 'detail_open', 'timestamp': '2026-10-25T12:00:00', 'utm_source': 'test'}
        body = json.dumps(payload).encode()
        storage = Mock()
        storage.is_available.return_value = False
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(service.tempfile, 'gettempdir', return_value=directory), \
                patch.object(service, 'VercelStorage', storage):
            path = Path(directory) / 'vol_utm_interactions.json'
            path.write_text(json.dumps([{'event': 'old'}]*1000))
            _, status, result = request('/api/telemetry', 'POST', headers={'Content-Length': str(len(body))}, body=body)
            logs = json.loads(path.read_text())
        self.assertEqual(status, 200)
        self.assertEqual(len(logs), 1000)
        self.assertEqual(logs[-1]['event'], 'detail_open')
        storage.rpush.assert_not_called()
        _, status, _ = request('/api/other', 'POST')
        self.assertEqual(status, 404)

    def test_telemetry_missing_event_and_redis_cap(self):
        storage = Mock()
        storage.is_available.return_value = True
        for payload, expected in [({}, 400), ({'event': 'view', 'timestamp': '2026-10-25T12:00:00'}, 200)]:
            body = json.dumps(payload).encode()
            with self.subTest(payload=payload), patch.object(service, 'VercelStorage', storage):
                _, status, _ = request('/api/metrics', 'POST', headers={'Content-Length': str(len(body))}, body=body)
            self.assertEqual(status, expected)
        storage.rpush.assert_called_once()
        storage.ltrim.assert_called_once_with('volatility_utm_interactions', -1000, -1)
