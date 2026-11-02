"""ADR-0028: frozen pre-move outputs and retained API/CLI contracts."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import build_cached
from api import index
from frontend import render
from pulse.legacy.scoring import LWConfig, save_lw_outputs, vol_signal_template


class FrontendConsolidationTests(unittest.TestCase):
    def test_retained_documents_match_pre_move_bytes(self):
        cases = (
            (build_cached.build_document(), 'd01aeb6c5db5de98b74426c9251a37354940514dbb77864cc51dd8b34d1d9dab'),
            (build_cached.build_document('{"summary":[]}'), 'bc563ad4f4dc61a6672077c6210de5c1273b683aaa9dfc9fc795967b9fdde871'),
            (index._vol_signal_shell_template(), '2bc035125730a252e1739de7b57d447d41e1d8d9dc6201f1ee2980be2c38c3f1'),
            (vol_signal_template({'summary': [], 'instruments': {}}), '97edee6c84a6190d16149f2cb3865fe83596af9dbab22ecf9d6d19be23651582'),
        )
        for document, expected in cases:
            self.assertEqual(hashlib.sha256(document.encode()).hexdigest(), expected)

    def test_generated_entry_is_current(self):
        self.assertEqual((build_cached.ROOT / 'index.html').read_text(), build_cached.build_document())

    def test_only_canonical_assets_exist(self):
        for name in ('vs.html', 'vs.css', 'vs.js', 'lw_tpl.html', 'lw_tpl.css', 'lw_tpl.js'):
            self.assertFalse((build_cached.API_DIR / name).exists())
            self.assertFalse((build_cached.ROOT / name).exists())
        for name in ('vs.html', 'vs.css', 'vs.js'):
            self.assertTrue(render.read_volatility_source(name))
        with self.assertRaises(ValueError):
            render.read_volatility_source('../../api/index.py')

    def test_private_fallbacks_are_unchanged_without_redis(self):
        expected = {
            '60': 'f19f9bc77f00eb05b063bca6e4e6efcfc9e9e3e2eaa16046ac2efe3204d77b10',
            '240': '1524290f3e02815020356565e8e3831474926aa646e4e8f8a6d303b060b4d2d9',
            '1D': '4090d47dbbe76558fb62180a177a9495c652722a411345a48c00e5fe9e343dc2',
        }
        with patch.object(index.VercelStorage, 'get_client', side_effect=AssertionError('No Redis')):
            for resolution, digest in expected.items():
                source = build_cached.API_DIR / f'volatility_lw_report_{resolution}.json'
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
                self.assertEqual(index.load_precomputed_report(resolution), json.loads(source.read_text()))

    def request(self, query, accept=''):
        instance = index.handler.__new__(index.handler)
        instance.path = '/api?' + query
        instance.headers = {'Accept': accept}
        instance.wfile = io.BytesIO()
        instance._send_json = Mock()
        instance._get_report_with_auto_heal = Mock(return_value=({'meta': {}, 'summary': []}, None))
        instance.warm_all_combinations = Mock()
        instance.do_GET()
        return instance

    def test_retired_html_does_not_fetch_or_heal(self):
        for query in ('legacy=1', 'legacy=1&timeframe=invalid', 'legacy=true'):
            instance = self.request(query)
            instance._send_json.assert_called_once_with(410, {'error': 'legacy_dashboard_retired'})
            instance._get_report_with_auto_heal.assert_not_called()
            instance.warm_all_combinations.assert_not_called()

    def test_json_precedence_is_preserved(self):
        for query, accept in (('legacy=1&json=1', ''), ('legacy=1&signal=1', ''), ('legacy=1', 'application/json')):
            instance = self.request(query, accept)
            self.assertEqual(instance._send_json.call_args.args[0], 200)
            instance._get_report_with_auto_heal.assert_called_once()

    def test_admin_and_cron_auth_still_precede_retirement(self):
        with patch.dict('os.environ', {'CRON_SECRET': 'test-only', 'ADMIN_SECRET': 'test-admin'}):
            for query in ('legacy=1&_cron=1', 'legacy=1&clear_cache=1'):
                instance = self.request(query)
                self.assertEqual(instance._send_json.call_args.args[0], 401)
                instance._get_report_with_auto_heal.assert_not_called()
                instance.warm_all_combinations.assert_not_called()

    def test_cli_save_uses_supported_ui_and_preserves_report(self):
        report = {'summary': [{'instrument': 'B-BTC_USDT', 'volatility_score': 0.01}],
                  'instruments': {'B-BTC_USDT': {'timeseries': [1, 2]}}, 'meta': {'fixture': True}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = LWConfig(base=index.VolatilityConfig(), output_json=str(root / 'report.json'), output_csv=str(root / 'report.csv'),
                           output_html=str(root / 'report.html'))
            save_lw_outputs(report, cfg)
            self.assertEqual(json.loads(Path(cfg.output_json).read_text()), report)
            self.assertTrue(Path(cfg.output_csv).is_file())
            html = Path(cfg.output_html).read_text()
            self.assertEqual(html, vol_signal_template(report))
            self.assertIn('id="app"', html)
            self.assertNotIn('timeseries', html.split('<script id="vd"')[1].split('</script>')[0])
            self.assertEqual(report['instruments']['B-BTC_USDT']['timeseries'], [1, 2])

    def test_utm_telemetry_fields_and_bounded_storage_are_preserved(self):
        payload = {'timestamp': '2026-10-25T12:00:00', 'event': 'detail_sheet_open',
                   'utm_source': 'entry', 'utm_medium': 'market_row', 'utm_campaign': 'campaign',
                   'utm_content': 'volatility', 'utm_term': 'BTC'}
        body = json.dumps(payload).encode()
        instance = index.handler.__new__(index.handler)
        instance.path = '/api/telemetry'
        instance.headers = {'Content-Length': str(len(body))}
        instance.rfile = io.BytesIO(body)
        instance._send_json = Mock()
        with patch.object(index.VercelStorage, 'is_available', return_value=True), \
                patch.object(index.VercelStorage, 'rpush') as push, \
                patch.object(index.VercelStorage, 'ltrim') as trim:
            instance.do_POST()
        self.assertEqual(instance._send_json.call_args.args[0], 200)
        self.assertEqual(push.call_args.args[0], 'volatility_utm_interactions')
        self.assertEqual(json.loads(push.call_args.args[1]), payload)
        trim.assert_called_once_with('volatility_utm_interactions', -1000, -1)


if __name__ == '__main__':
    unittest.main()
