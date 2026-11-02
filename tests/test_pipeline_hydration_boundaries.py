"""ADR-0033: offline tests executed before and after pipeline extraction."""
import copy
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pulse.legacy import service, scaling, enrichment, pipeline, fast_hydrate


NOW = 1_792_900_000.0


def base_report(age=30):
    return {'meta': {'last_rebuilt': NOW-age, 'target_seconds': 3600, 'generation_id': 'g'},
            'instruments': {'B-BTC_USDT': {'instrument': 'B-BTC_USDT', 'price_last': 100,
                                          'volatility_score': .01, 'timeseries': {'x': [1]}, 'oi_history': [1]}},
            'summary': [{'instrument': 'B-BTC_USDT', 'price_last': 100, 'trade_signal': 'Range',
                         'volatility_score': .01, 'timeseries': {'x': [1]}, 'oi_history': [1]}]}


class PipelineHydrationBoundaryTests(unittest.TestCase):
    def test_scaling_native_windows_copies_inputs_and_preserves_units(self):
        for resolution, horizon, factor in [('60', '1H', 1), ('240', '4H', 1), ('1D', '24H', 1), ('60', '4H', 2)]:
            source = {'meta': {'resolution': resolution}, 'instruments': {
                'BTC': {'sigma_yz': .1, 'sigma_ewma': .2, 'bvol_window': .3,
                        'price_last': 100, 'volume_24h': 500, 'vol_sparkline_24h': [.1, .2],
                        'timeseries': {'yz': {'y': [.1, .2]}, 'price': {'y': [100, 101]}}},
                'BAD': {'error': 'missing'}}}
            before = copy.deepcopy(source)
            score = Mock(return_value=([{'instrument': 'BTC', 'volume_24h': 5_000_000}], {'count': 1}))
            with self.subTest(resolution=resolution, horizon=horizon):
                result = scaling.scale_and_score_report(source, horizon, SimpleNamespace(min_volume_usd=0), score=score)
                self.assertEqual(source, before)
                token = result['instruments']['BTC']
                self.assertAlmostEqual(token['sigma_yz'], .1*factor)
                self.assertAlmostEqual(token['sigma_ewma'], .2*factor)
                self.assertEqual(token['vol_sparkline_24h'], [.1*factor, .2*factor])
                self.assertEqual(token['timeseries']['price']['y'], [100, 101])
                self.assertEqual(token['price_last'], 100)
                self.assertEqual(token['volume_24h'], 500)
                self.assertEqual(result['meta']['scale_factor'], factor)
                self.assertEqual(result['instruments']['BAD'], {'error': 'missing'})
                self.assertIs(score.call_args.args[0], result['instruments'])
        score.return_value = ([{'instrument': 'A', 'volume_24h': 999_999},
                               {'instrument': 'B', 'volume_24h': 1_000_000}], {})
        result = scaling.scale_and_score_report(source, '1H', SimpleNamespace(min_volume_usd=1), score=score)
        self.assertEqual([row['instrument'] for row in result['summary']], ['B'])

    def test_enrichment_direct_dependencies_keep_summary_coverage_and_quote_units(self):
        source = base_report()
        source['instruments']['BAD'] = {'error': 'missing'}
        response = Mock(status_code=200)
        response.json.return_value = {'prices': {'B-BTC_USDT': {
            'ls': '125', 'pc': '2.5', 'efr': '.0001', 'v': '1000', 'mkt': 'BTCUSDT'}}}
        session, http = Mock(), Mock()
        session.get.return_value = response
        oi = Mock(return_value={'oi_available': True, 'oi_usd': 1000})
        classify = Mock(side_effect=lambda token, *args: token.update(trade_signal='Range'))
        market = Mock(return_value={'test': 'context'})
        result = enrichment._enrich_with_signals(source, session,
            http=http, get_oi=oi, classify=classify, market_context=market,
            provider_order=lambda: ['fixture'], unavailable=Mock(side_effect=AssertionError('Unexpected missing OI')))
        self.assertIs(result, source)  # Original full enrichment is intentionally in-place.
        http.Session.assert_not_called()
        session.get.assert_called_once_with('https://public.coindcx.com/market_data/v3/current_prices/futures/rt', timeout=8)
        oi.assert_called_once_with('B-BTC_USDT', market_symbol='BTCUSDT', price_hint='125', session=session)
        classify.assert_called_once()
        row = result['summary'][0]
        self.assertEqual((row['price_change_24h'], row['volume_24h'], row['dollar_vol_24h']), (.025, 8, 1000))
        self.assertEqual(result['meta']['oi_coverage_ratio'], 1)
        self.assertEqual(result['meta']['oi_provider_order'], ['fixture'])
        self.assertEqual(result['market_context'], {'test': 'context'})

    def test_wrappers_inject_current_compute_http_and_cache_dependencies(self):
        for module, name, invocation, dependency, source_name in [
            (scaling, 'scale_and_score_report', lambda: service.scale_and_score_report({}, '1H', None), 'score', 'score_cross_section'),
            (enrichment, '_get_cached_oi', lambda: service._get_cached_oi('BTC'), 'cache_state', '_OI_CACHE'),
            (enrichment, '_enrich_with_signals', lambda: service._enrich_with_signals({}), 'get_oi', '_get_cached_oi'),
            (pipeline, '_perform_optimized_pipeline', lambda: service._perform_optimized_pipeline({}, 'key', '60', '1H', 'USDT', True, [], False, False), 'enrich', '_enrich_with_signals'),
            (fast_hydrate, '_fast_hydrate', lambda: service._fast_hydrate({}, 'key', '60', '1H'), 'http', 'requests'),
        ]:
            with self.subTest(name=name), patch.object(service, source_name) as dependency_value, patch.object(module, name) as implementation:
                self.assertIs(invocation(), implementation.return_value)
                self.assertIs(implementation.call_args.kwargs[dependency], dependency_value)

    def pipeline(self, base, *, cron=True, rebuild=False, persist=True, failure=None, vercel='1', tf='60', hz='1H'):
        events = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(service.time, 'time', return_value=NOW))
            stack.enter_context(patch.dict('os.environ', {'VERCEL': vercel}))
            bootstrap = stack.enter_context(patch.object(service, 'bootstrap_cache', side_effect=lambda: events.append('bootstrap')))
            cached = stack.enter_context(patch.object(service, '_try_serve_from_cache', return_value=base))
            fallback = stack.enter_context(patch.object(service, 'load_precomputed_report', return_value=None))
            analysis = stack.enter_context(patch.object(service, 'run_lightweight_analysis', return_value=base_report(), side_effect=failure))
            scale = stack.enter_context(patch.object(service, 'scale_and_score_report', side_effect=lambda value, *args: copy.deepcopy(value)))
            enrich = stack.enter_context(patch.object(service, '_enrich_with_signals', side_effect=lambda value: events.append('enrich') or value))
            save = stack.enter_context(patch.object(service, '_save_to_cache_layers', side_effect=lambda *args, **kw: events.append(('save', args, kw))))
            try:
                result = service._perform_optimized_pipeline(
                    {'_cron': ['1' if cron else '0'], '_generation_id': ['next']}, 'target', tf, hz,
                    'USDT', True, [], True, rebuild, persist)
            except Exception as exc:
                result = exc
        return result, events, {'cached': cached, 'fallback': fallback, 'analysis': analysis, 'save': save,
                                'scale': scale, 'enrich': enrich, 'bootstrap': bootstrap}

    def test_pipeline_native_windows_stages_without_target_save(self):
        for tf, hz in service.UI_COMBINATIONS:
            source = base_report()
            before = copy.deepcopy(source)
            with self.subTest(tf=tf):
                result, events, mocks = self.pipeline(source, persist=False, tf=tf, hz=hz)
                self.assertIsInstance(result, dict)
                mocks['cached'].assert_called_once_with(f'base_USDT_{tf}_True_', tf, hz)
                mocks['analysis'].assert_not_called()
                mocks['save'].assert_not_called()
                self.assertEqual(events, ['bootstrap', 'enrich'])
                self.assertEqual(source, before)
                self.assertEqual(result['meta']['generation_id'], 'next')
                self.assertEqual(result['meta']['last_rebuilt'], NOW-30)
                self.assertEqual(result['meta']['last_hydrated'], NOW)
                self.assertNotIn('timeseries', result['summary'][0])
                self.assertNotIn('oi_history', result['summary'][0])
                self.assertEqual(result['instruments']['B-BTC_USDT'], {'instrument': 'B-BTC_USDT'})

    def test_pipeline_rebuild_base_then_target_and_exact_ttls(self):
        result, events, mocks = self.pipeline(None, persist=True)
        self.assertIsInstance(result, dict)
        mocks['analysis'].assert_called_once()
        config = mocks['analysis'].call_args.args[0]
        self.assertEqual(config.base.request_timeout, 20)
        self.assertEqual(config.base.retries, 2)
        self.assertEqual(config.base.min_bars, 80)
        self.assertEqual([event[0] if isinstance(event, tuple) else event for event in events],
                         ['bootstrap', 'save', 'enrich', 'save'])
        self.assertEqual(mocks['save'].call_args_list[0].kwargs, {'ex': 600})
        self.assertEqual(mocks['save'].call_args_list[1].kwargs, {'ex': service.CACHE_TTL})

    def test_pipeline_rebuild_failure_stale_guard_and_missing_base(self):
        with patch('builtins.print'):
            result, _, mocks = self.pipeline(None, failure=RuntimeError('fixture'))
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('fresh base rebuild failed', str(result))
        mocks['fallback'].assert_not_called()
        mocks['enrich'].assert_not_called()
        result, _, mocks = self.pipeline(base_report(901))
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('refusing stale base', str(result))
        mocks['analysis'].assert_not_called()
        mocks['enrich'].assert_not_called()
        result, _, mocks = self.pipeline(None, cron=False)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('no base report', str(result))
        mocks['fallback'].assert_called_once()
        mocks['analysis'].assert_not_called()

    def test_pipeline_vercel_viewer_cannot_force_rebuild(self):
        result, _, mocks = self.pipeline(base_report(), cron=False, rebuild=True)
        self.assertIsInstance(result, dict)
        mocks['analysis'].assert_not_called()

    def test_oi_cache_exact_expiry_single_state_and_failure_nulls(self):
        state = {'store': {}, 'lock': threading.Lock()}
        with patch.object(service, '_OI_CACHE', state), patch.object(service.time, 'time', return_value=NOW), \
                patch.object(service, 'fetch_oi_metrics', return_value={'oi_available': True}) as fetch:
            first = service._get_cached_oi('BTC', 'BTCUSDT', 100, 'session')
            self.assertIs(service._get_cached_oi('BTC'), first)
            fetch.assert_called_once_with('BTC', market_symbol='BTCUSDT', price_hint=100,
                                          period='1h', limit=25, session='session')
            state['store']['BTC']['ts'] = NOW-service.OI_CACHE_TTL
            service._get_cached_oi('BTC')
            self.assertEqual(fetch.call_count, 2)
        with patch.object(service, '_OI_CACHE', state), patch.object(service, 'fetch_oi_metrics', side_effect=RuntimeError('fixture')):
            unavailable = service._get_cached_oi('ETH')
        self.assertFalse(unavailable['oi_available'])
        self.assertIsNone(unavailable['oi_usd'])

    def hydrate(self, payload=None, *, available=True, failure=None, status=200, reconcile_reason='accepted'):
        source, events = base_report(), []
        before = copy.deepcopy(source)
        response = Mock(status_code=status)
        response.json.return_value = {'prices': {'B-BTC_USDT': payload or {}}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(service.time, 'time', return_value=NOW))
            resolve = stack.enter_context(patch.object(service, '_resolve_target_report',
                side_effect=lambda *args, **kw: events.append('resolve') or (source if available else None, {'reason': 'missing'})))
            http = stack.enter_context(patch.object(service.requests, 'get', return_value=response, side_effect=failure))
            classify = stack.enter_context(patch('pulse.legacy.signal_engine.enrich_token',
                side_effect=lambda row, *args: row.update(trade_signal='MomentumLong')))
            reconcile = stack.enter_context(patch.object(service.VercelStorage, 'reconcile_signal_states',
                return_value={'reason': reconcile_reason, 'states': {}}))
            with patch('builtins.print'):
                result = service._fast_hydrate({}, 'key', '60', '1H')
        self.assertEqual(source, before)
        return result, resolve, http, classify, reconcile

    def test_hydrate_unavailable_and_http_failure_do_not_reconcile(self):
        result, _, http, classify, reconcile = self.hydrate(available=False)
        self.assertEqual(result['status'], 'unavailable')
        for callback in [http, classify, reconcile]:
            callback.assert_not_called()
        result, _, http, classify, reconcile = self.hydrate(failure=RuntimeError('fixture'))
        self.assertEqual(result['delta'], {})
        http.assert_called_once_with('https://public.coindcx.com/market_data/v3/current_prices/futures/rt', timeout=5)
        classify.assert_not_called()
        reconcile.assert_not_called()

    def test_hydrate_price_change_volume_units_watermark_and_mismatch_clear(self):
        result, resolve, _, classify, reconcile = self.hydrate(
            {'ls': '125', 'pc': '2.5', 'efr': '.0001', 'v': '1000', 'h': '130', 'l': '90'})
        row = result['delta']['B-BTC_USDT']
        self.assertEqual(row['price_last'], 125)
        self.assertEqual(row['price_change_24h'], .025)
        self.assertEqual(row['dollar_vol_24h'], 1000)
        self.assertEqual(row['volume_24h'], 8)
        self.assertEqual(row['funding_rate'], .0001)
        self.assertIsNone(row['signal_since_price'])
        self.assertIsNone(row['signal_since_at'])
        resolve.assert_called_once_with('key', '60', '1H', allow_stale=True)
        self.assertEqual(reconcile.call_args.args[2], NOW)
        self.assertEqual(reconcile.call_args.kwargs, {'generation_id': 'g'})
        self.assertEqual(reconcile.call_args.args[1],
                         [{'instrument': 'B-BTC_USDT', 'signal': 'MomentumLong', 'price': 125}])
        classify.assert_called_once()
        superseded, *_ = self.hydrate({'ls': '125'}, reconcile_reason='superseded')
        self.assertEqual(superseded['delta'], {})

    def test_hydrate_invalid_numbers_omitted_and_non_200_retains_empty_observations(self):
        result, _, _, classify, reconcile = self.hydrate(
            {'ls': 'nan', 'pc': 'inf', 'efr': 'nan', 'v': '-1', 'h': '0', 'l': '-1'})
        self.assertEqual(result['delta'], {})
        classify.assert_not_called()
        self.assertEqual(reconcile.call_args.args[1], [])
        result, _, _, classify, reconcile = self.hydrate(status=503)
        self.assertEqual(result['delta'], {})
        classify.assert_not_called()
        self.assertEqual(reconcile.call_args.args[1], [])
