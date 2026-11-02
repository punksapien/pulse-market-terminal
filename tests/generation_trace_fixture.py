"""Fixed-clock fake-storage traces, captured before ADR-0031 extraction."""
import copy
import hashlib
import json
from unittest.mock import patch


def trace_outputs(service):
    now = 1_792_900_000.0
    outputs = {}

    def frozen(value):
        return copy.deepcopy(value)

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def reports():
        result = []
        for tf, hz in service.UI_COMBINATIONS:
            result.append((tf, hz, f'USDT_{tf}_{hz}_True_', {
                'meta': {'resolution': tf, 'scale_horizon': hz,
                         'target_seconds': service._parse_horizon_to_seconds(hz),
                         'last_rebuilt': now-30, 'oi_coverage_ratio': .9},
                'summary': [{'instrument': 'B-BTC_USDT', 'volatility_score': .01}],
                'errors': {}}))
        return result

    for mode in ('published', 'atomic_failed', 'redis_down', 'local_only'):
        events = []
        class Store:
            @staticmethod
            def is_available():
                return mode not in ('redis_down', 'local_only')

            @staticmethod
            def get(key):
                events.append(['get', key])
                return 'previous'

            @staticmethod
            def set_many_atomic(entries):
                events.append(['atomic', frozen(entries)])
                return mode != 'atomic_failed'

        generated = reports()
        with patch.object(service, 'VercelStorage', Store), \
                patch.object(service.time, 'time', return_value=now), \
                patch.dict('os.environ', {'VERCEL': '0' if mode == 'local_only' else '1',
                                         'VERCEL_GIT_COMMIT_SHA': 'fixture-sha'}, clear=True), \
                patch.object(service, '_save_to_local_cache_layers',
                             side_effect=lambda key, value: events.append(['local', key, frozen(value)])), \
                patch.object(service, '_best_effort_reconcile_generation_signal_states',
                             side_effect=lambda *args, **kw: events.append(['signals', frozen(args), frozen(kw)])):
            result = service._publish_generation('fixture-generation', generated, trigger='fixture', started_at=now-10)
        outputs[mode] = digest([result, events, generated])

    for mode in ('fresh', 'recovered', 'expired'):
        events = []
        generation_id = 'fixture-generation'
        generated = reports()
        if mode == 'expired':
            for _, _, _, report in generated:
                report['meta']['last_rebuilt'] = now-service.REPORT_MAX_STALE-1
        manifest = {'schema_version': service.REPORT_SCHEMA_VERSION, 'model_version': service.MODEL_VERSION,
                    'generation_id': generation_id, 'published_at': now-30,
                    'combinations': [{'resolution': tf, 'horizon': hz} for tf, hz in service.UI_COMBINATIONS]}
        store = {service.ACTIVE_GENERATION_KEY: None if mode == 'recovered' else generation_id,
                 service.LAST_GOOD_GENERATION_KEY: generation_id,
                 service._generation_manifest_key(generation_id): json.dumps(manifest)}
        for tf, hz, key, report in generated:
            report['meta']['generation_id'] = generation_id
            store[service._generation_report_key(generation_id, key)] = json.dumps(report)

        class Store:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def get(key):
                events.append(['get', key])
                return store.get(key)

            @staticmethod
            def set_many_atomic(entries):
                events.append(['atomic', frozen(entries)])
                return True

            @staticmethod
            def get_signal_states(key, instruments):
                events.append(['states', key, instruments])
                return {}

        with patch.object(service, 'VercelStorage', Store), \
                patch.object(service.time, 'time', return_value=now), \
                patch.dict('os.environ', {'VERCEL': '1'}, clear=True), \
                patch.object(service, 'load_precomputed_report', return_value=None), \
                patch.object(service, '_save_to_local_cache_layers',
                             side_effect=lambda key, value: events.append(['local', key, frozen(value)])):
            result = service._resolve_target_report('USDT_60_1H_True_', '60', '1H')
        outputs[mode] = digest([result, events])
    return outputs
