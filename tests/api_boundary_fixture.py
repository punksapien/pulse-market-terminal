"""Offline HTTP wire baselines captured before ADR-0030 extraction."""
import hashlib
import io
import json
from unittest.mock import Mock, patch


def wire_outputs(index):
    cases = [
        ('shell', 'GET', '/api', {}, b''),
        ('retired', 'GET', '/api?legacy=1', {}, b''),
        ('invalid_window', 'GET', '/api?json=1&timeframe=bad', {}, b''),
        ('cron_unauthorized', 'GET', '/api/cron', {}, b''),
        ('cron_authorized', 'GET', '/api/cron?force=1', {'Authorization': 'Bearer test-only'}, b''),
        ('admin_unauthorized', 'GET', '/api?clear_cache=1', {}, b''),
        ('health', 'GET', '/api/health', {}, b''),
        ('options', 'OPTIONS', '/api', {}, b''),
        ('telemetry_get', 'GET', '/api/telemetry', {}, b''),
        ('telemetry_oversize', 'POST', '/api/telemetry', {'Content-Length': '4097'}, b''),
        ('telemetry_malformed', 'POST', '/api/telemetry', {'Content-Length': '1'}, b'{'),
    ]
    payload = {'timestamp': '2026-10-25T12:00:00', 'event': 'detail_sheet_open',
               'utm_source': 'entry', 'utm_medium': 'market_row', 'utm_campaign': 'campaign',
               'utm_content': 'volatility', 'utm_term': 'BTC'}
    body = json.dumps(payload).encode()
    cases.append(('telemetry_post', 'POST', '/api/telemetry', {'Content-Length': str(len(body))}, body))
    for resolution, horizon in (('60', '1H'), ('240', '4H'), ('1D', '24H')):
        cases.append((resolution, 'GET', f'/api?json=1&timeframe={resolution}&horizon={horizon}', {}, b''))
    outputs = {}
    env = {'VERCEL': '1', 'CRON_SECRET': 'test-only', 'ADMIN_SECRET': 'test-admin',
           'VERCEL_GIT_COMMIT_SHA': 'fixture-sha'}
    with patch.dict('os.environ', env, clear=True), \
            patch.object(index, '_generation_health_snapshot', return_value={'healthy': True}), \
            patch.object(index.VercelStorage, 'is_available', return_value=True), \
            patch.object(index.VercelStorage, 'rpush'), patch.object(index.VercelStorage, 'ltrim'), \
            patch('requests.sessions.Session.request', side_effect=AssertionError('Offline routes only')):
        for name, method, path, headers, request_body in cases:
            instance = index.handler.__new__(index.handler)
            instance.path, instance.headers = path, headers
            instance.rfile, instance.wfile = io.BytesIO(request_body), io.BytesIO()
            instance.send_response = Mock()
            response_headers = []
            instance.send_header = lambda key, value: response_headers.append((key, value))
            instance.end_headers = Mock()
            instance._get_report_with_auto_heal = Mock(return_value=({
                'meta': {'generation_id': 'fixture-generation', 'freshness_status': 'fresh',
                         'model_age_seconds': 12}, 'summary': [{'instrument': 'B-BTC_USDT', 'volatility_score': .01}]}, None))
            instance.warm_all_combinations = Mock(return_value={'status': 'fresh_noop', 'published': False})
            getattr(instance, 'do_' + method)()
            wire = json.dumps([instance.send_response.call_args.args[0], response_headers,
                               instance.wfile.getvalue().decode()], separators=(',', ':'))
            outputs[name] = hashlib.sha256(wire.encode()).hexdigest()
    return outputs
