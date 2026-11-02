"""ADR-0031: direct fake dependencies, cache tiers and publication/read traces."""
import ast
import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pulse.legacy import service, cache, generation, publisher, signal_state, reader, health
from pulse.legacy import scaling, enrichment, pipeline, fast_hydrate, dispatch, refresh
from pulse.api import handlers as http_handlers
from tests.generation_trace_fixture import trace_outputs


ROOT = Path(__file__).resolve().parents[1]


class GenerationDependencyTests(unittest.TestCase):
    def test_seven_pre_move_operation_traces_are_identical(self):
        expected = json.loads((ROOT / 'tests/fixtures/generation_trace_baseline.json').read_text())
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No provider calls')):
            self.assertEqual(trace_outputs(service), expected)

    def test_cache_tier_precedence_expiry_and_corruption(self):
        now, ttl = 1_792_900_000.0, 60
        state = {'store': {}, 'lock': threading.Lock()}
        storage = Mock()
        storage.is_available.return_value = True
        storage.get.return_value = json.dumps({'source': 'redis'})
        with tempfile.TemporaryDirectory() as directory, patch.object(cache.time, 'time', return_value=now):
            path = Path(directory) / 'cache.json'
            path.write_text(json.dumps({'source': 'tmp'}))
            os.utime(path, (now, now))
            kwargs = dict(cache_state=state, storage=storage, cache_ttl=ttl, cache_path=lambda _: path)
            state['store']['key'] = {'data': {'source': 'memory'}, 'ts': now-1}
            self.assertEqual(cache._try_serve_from_cache('key', '60', '1H', **kwargs), {'source': 'memory'})
            storage.is_available.assert_not_called()
            state['store']['key']['ts'] = now-ttl
            self.assertEqual(cache._try_serve_from_cache('key', '60', '1H', **kwargs), {'source': 'redis'})
            self.assertEqual(state['store']['key']['ts'], now)
            state['store'].clear()
            storage.get.return_value = '{invalid'
            self.assertEqual(cache._try_serve_from_cache('key', '60', '1H', **kwargs), {'source': 'tmp'})
            state['store'].clear()
            os.utime(path, (now-ttl-1, now-ttl-1))
            self.assertIsNone(cache._try_serve_from_cache('key', '60', '1H', **kwargs))
            os.utime(path, (now, now))
            path.write_text('{invalid')
            self.assertIsNone(cache._try_serve_from_cache('key', '60', '1H', **kwargs))

    def test_stale_tiers_do_not_retimestamp_packaged_models(self):
        state = {'store': {}, 'lock': threading.Lock()}
        storage = Mock()
        storage.is_available.return_value = False
        fallback = Mock(return_value={'meta': {'last_rebuilt': 1}, 'source': 'packaged'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.json'
            kwargs = dict(cache_state=state, storage=storage, cache_path=lambda _: path, fallback=fallback)
            result = cache._serve_stale_or_fallback('key', '60', '1H', **kwargs)
            self.assertEqual(result['meta']['last_rebuilt'], 1)
            fallback.assert_called_once_with('60')
            path.write_text(json.dumps({'source': 'tmp'}))
            self.assertEqual(cache._serve_stale_or_fallback('key', '60', '1H', **kwargs), {'source': 'tmp'})
            storage.is_available.return_value = True
            storage.get.return_value = json.dumps({'source': 'redis'})
            self.assertEqual(cache._serve_stale_or_fallback('key', '60', '1H', **kwargs), {'source': 'redis'})
            state['store']['key'] = {'data': {'source': 'memory'}, 'ts': 1}
            self.assertEqual(cache._serve_stale_or_fallback('key', '60', '1H', **kwargs), {'source': 'memory'})

    def test_local_save_keeps_memory_when_disk_write_fails(self):
        state = {'store': {}, 'lock': threading.Lock()}
        report = {'meta': {'last_rebuilt': 1}, 'summary': []}
        with tempfile.TemporaryDirectory() as directory, patch.object(cache.time, 'time', return_value=100):
            cache._save_to_local_cache_layers('key', report, cache_state=state,
                                               cache_path=lambda _: Path(directory))
        self.assertIs(state['store']['key']['data'], report)
        self.assertEqual(state['store']['key']['ts'], 100)
        self.assertEqual(report['meta']['last_rebuilt'], 1)

    def test_cache_save_preserves_callback_then_storage_order_and_ttl(self):
        events = []
        storage = Mock()
        storage.is_available.return_value = True
        storage.set.side_effect = lambda *a, **kw: events.append(['redis', a, kw])
        save = lambda *a: events.append(['local', a])
        cache._save_to_cache_layers('key', {'value': 1}, '60', '1H', ex=77,
                                   save_local=save, storage=storage)
        self.assertEqual([event[0] for event in events], ['local', 'redis'])
        storage.set.assert_called_once_with('key', '{"value":1}', ex=77)

    def test_generation_reader_timestamp_and_generation_boundaries(self):
        now = 1_792_900_000.0
        manifest = {'schema_version': service.REPORT_SCHEMA_VERSION, 'model_version': service.MODEL_VERSION,
                    'generation_id': 'g', 'published_at': now-10,
                    'combinations': [{'resolution': tf, 'horizon': hz} for tf, hz in service.UI_COMBINATIONS]}
        report = {'meta': {'resolution': '60', 'scale_horizon': '1H', 'target_seconds': 3600,
                           'generation_id': 'g', 'last_rebuilt': now-30},
                  'summary': [{'instrument': 'B-BTC_USDT', 'volatility_score': .01}]}
        cases = (
            ('valid', -10, -30, 'g', 'ok'),
            ('manifest_edge', 300, -30, 'g', 'ok'),
            ('manifest_future', 300.01, -30, 'g', 'manifest_timestamp_in_future'),
            ('model_edge', -10, 300, 'g', 'ok'),
            ('model_future', -10, 300.01, 'g', 'model_timestamp_invalid'),
            ('generation_mismatch', -10, -30, 'other', 'report_generation_mismatch'),
        )
        for label, published_offset, model_offset, report_id, reason in cases:
            with self.subTest(case=label):
                m, r = copy.deepcopy(manifest), copy.deepcopy(report)
                m['published_at'] = now+published_offset
                r['meta'].update(last_rebuilt=now+model_offset, generation_id=report_id)
                load = Mock(side_effect=[m, r])
                actual, provenance = generation._read_generation_report('g', 'key', '60', '1H', now=now,
                                                                         load_json=load)
                self.assertEqual(provenance['reason'], reason)
                self.assertEqual(actual is not None, reason == 'ok')
                self.assertEqual(load.call_count, 2)
                if label == 'valid':
                    self.assertEqual(provenance['effective_age_seconds'], 30)

    def test_publication_composition_resolves_current_callbacks(self):
        save, reconcile, storage = Mock(), Mock(), Mock()
        with patch.object(service, '_save_to_local_cache_layers', save), \
                patch.object(service, '_best_effort_reconcile_generation_signal_states', reconcile), \
                patch.object(service, 'VercelStorage', storage), \
                patch.object(publisher, '_publish_generation', return_value='fixture') as implementation:
            self.assertEqual(service._publish_generation('g', [], trigger='fixture', started_at=100), 'fixture')
        implementation.assert_called_once_with('g', [], 'fixture', 100,
                                               storage=storage, save_local=save, reconcile_signals=reconcile)

    def test_status_detail_names_cannot_collide_with_dependencies(self):
        storage = Mock()
        storage.is_available.return_value = True
        with patch.object(service, 'VercelStorage', storage), patch.object(publisher.time, 'time', return_value=100):
            payload = service._record_refresh_status('start', 'fixture', storage='detail', save_local='other-detail')
        self.assertEqual(payload['storage'], 'detail')
        self.assertEqual(payload['save_local'], 'other-detail')
        self.assertEqual(payload['updated_at'], 100)
        self.assertIs(storage.set.call_args.args[1], payload)

    def test_extracted_modules_never_import_service_or_construct_redis(self):
        for module in (cache, generation, publisher, signal_state, reader, health, scaling, enrichment, pipeline, fast_hydrate, dispatch, refresh, http_handlers):
            tree = ast.parse(Path(module.__file__).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, 'pulse.legacy.service')
                    self.assertFalse(node.module == 'pulse.legacy' and any(alias.name == 'service' for alias in node.names))
                    self.assertFalse(node.module and node.module.startswith('pulse.store'))
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name in ('redis', 'pulse.legacy.service') for alias in node.names))


if __name__ == '__main__':
    unittest.main()
