"""ADR-0034: offline dispatch/refresh contracts run before and after extraction."""
import copy
import ast
import inspect
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from pulse.legacy import service, dispatch, refresh
from tests.api_boundary_parity import METHOD_BINDINGS, original_definition, verify_wrapper


class DispatchRefreshBoundaryTests(unittest.TestCase):
    def test_public_dispatch_read_and_filter_never_builds_or_locks(self):
        source = {'meta': {}, 'summary': [{'instrument': 'B-BTC_USDT'}, {'instrument': 'B-ETH_USDT'}],
                  'instruments': {'B-BTC_USDT': {}, 'B-ETH_USDT': {}}}
        before = copy.deepcopy(source)
        with patch.object(service, 'VercelStorage') as storage, \
                patch.object(service, '_perform_optimized_pipeline') as build, \
                patch.object(service, '_resolve_target_report', return_value=(source, {})) as read:
            result = service._get_cached_or_compute({'symbols': ['BTC'], 'timeframe': ['60'], 'horizon': ['1H']})
        self.assertEqual(source, before)
        self.assertEqual(result['summary'], [{'instrument': 'B-BTC_USDT'}])
        read.assert_called_once_with('USDT_60_1H_True_', '60', '1H', allow_stale=False)
        build.assert_not_called()
        self.assertEqual(storage.mock_calls, [])

    def test_dispatch_fast_bypasses_read_compute_and_lock(self):
        query = {'fast': ['1'], 'timeframe': ['240'], 'horizon': ['4H']}
        with patch.object(service, '_fast_hydrate', return_value={'delta': {}}) as fast, \
                patch.object(service, '_resolve_target_report') as read, patch.object(service, 'VercelStorage') as storage:
            self.assertEqual(service._get_cached_or_compute(query), {'delta': {}})
        fast.assert_called_once_with(query, 'USDT_240_4H_True_', '240', '4H')
        read.assert_not_called()
        self.assertEqual(storage.mock_calls, [])

    def test_dispatch_token_release_on_cache_hit_build_failure_and_sync_stage(self):
        for mode in ['cached', 'failure', 'sync_stage']:
            storage = Mock()
            storage.is_available.return_value = True
            storage.setnx.return_value = True
            query = {'_cron': ['1']}
            if mode == 'sync_stage':
                query.update(_sync_locked=['1'], _stage_only=['1'], refresh=['1'])
            with self.subTest(mode=mode), patch.object(service, 'VercelStorage', storage), \
                    patch.object(service.uuid, 'uuid4', return_value=Mock(hex='fixture-token')), \
                    patch.object(service, '_try_serve_from_cache', return_value={'cached': True} if mode == 'cached' else None), \
                    patch.object(service, '_perform_optimized_pipeline', side_effect=RuntimeError('fixture') if mode == 'failure' else None) as build:
                if mode == 'failure':
                    with self.assertRaisesRegex(RuntimeError, 'fixture'):
                        service._get_cached_or_compute(query)
                else:
                    service._get_cached_or_compute(query)
            if mode == 'sync_stage':
                storage.setnx.assert_not_called()
                storage.delete_if_value.assert_not_called()
                self.assertFalse(build.call_args.kwargs['persist_target'])
            else:
                key = service.LOCK_KEY_PREFIX+'USDT_60_1H_True_'
                storage.setnx.assert_called_once_with(key, 'fixture-token', ex=service.LOCK_TTL)
                storage.delete_if_value.assert_called_once_with(key, 'fixture-token')
                if mode == 'cached':
                    build.assert_not_called()

    def test_dispatch_contended_lock_production_rejects_local_uses_fallback(self):
        for vercel in ['1', '0']:
            storage = Mock()
            storage.is_available.return_value = True
            storage.setnx.return_value = False
            with self.subTest(vercel=vercel), patch.dict('os.environ', {'VERCEL': vercel}), \
                    patch.object(service, 'VercelStorage', storage), patch('builtins.print'), \
                    patch.object(service, '_serve_stale_or_fallback', return_value={'fallback': True}) as fallback:
                if vercel == '1':
                    with self.assertRaises(service.ReportUnavailableError):
                        service._get_cached_or_compute({'_cron': ['1']})
                    fallback.assert_not_called()
                else:
                    self.assertEqual(service._get_cached_or_compute({'_cron': ['1']}), {'fallback': True})
            storage.delete_if_value.assert_not_called()

    def warmer(self, mode, force=False):
        events = []
        storage = Mock()
        storage.is_available.return_value = mode != 'redis_down'
        storage.setnx.side_effect = lambda *args, **kw: events.append(('lock', args, kw)) or mode != 'locked'
        storage.delete_if_value.side_effect = lambda *args: events.append(('release', args))
        storage.set_many_atomic.side_effect = lambda entries: events.append(('recover', entries)) or True
        stale = {'complete': False, 'generation_age_seconds': 4000, 'max_model_age_seconds': 4000}
        fresh = {'complete': True, 'generation_age_seconds': 10, 'max_model_age_seconds': 10,
                 'selected_generation': 'g', 'pointer_source': 'last_good', 'combinations': []}
        healths = [fresh, fresh] if mode == 'fresh' else [stale, fresh if mode == 'race' else stale]
        def compute(query):
            events.append(('compute', copy.deepcopy(query)))
            if mode == 'compute_failure':
                raise RuntimeError('fixture')
            return {'summary': [{'instrument': 'BTC'}], 'meta': {}}
        with ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', {'VERCEL': '1'}))
            stack.enter_context(patch.object(service, 'VercelStorage', storage))
            stack.enter_context(patch.object(service.time, 'time', return_value=1_792_900_000))
            stack.enter_context(patch.object(service.uuid, 'uuid4', return_value=Mock(hex='fixture-token')))
            stack.enter_context(patch('builtins.print'))
            health = stack.enter_context(patch.object(service, '_generation_health_snapshot', side_effect=healths))
            stack.enter_context(patch.object(service, '_get_cached_or_compute', side_effect=compute))
            stack.enter_context(patch.object(service, '_validate_generated_report', return_value=(mode != 'invalid', 'fixture')))
            stack.enter_context(patch.object(service, '_validate_generation_set', side_effect=lambda *args, **kw: events.append(('validate_set',)) or (mode != 'invalid_set', 'fixture')))
            publish = stack.enter_context(patch.object(service, '_publish_generation', side_effect=lambda *args, **kw: events.append(('publish',)) or (mode != 'publish_failure', {'published_at': 1_792_900_000}, 'fixture')))
            backoff = stack.enter_context(patch.object(service, '_set_refresh_backoff', side_effect=lambda *args: events.append(('backoff',))))
            status = stack.enter_context(patch.object(service, '_record_refresh_status', side_effect=lambda state, *args, **kw: events.append(('status', state))))
            result = service.handler.warm_all_combinations(None, force=force)
        return result, events, storage, health, publish, backoff, status

    def test_warmer_unavailable_contended_fresh_and_post_lock_race(self):
        for mode, expected in [('redis_down', 'redis_unavailable'), ('locked', 'locked'), ('fresh', 'fresh_noop'), ('race', 'fresh_noop')]:
            with self.subTest(mode=mode):
                result, events, storage, health, publish, backoff, status = self.warmer(mode)
                self.assertEqual(result['status'], expected)
                publish.assert_not_called()
                backoff.assert_not_called()
                status.assert_not_called()
                if mode == 'race':
                    self.assertEqual([event[0] for event in events], ['lock', 'release'])
                    self.assertEqual(health.call_count, 2)
                else:
                    storage.delete_if_value.assert_not_called()
                if mode == 'fresh':
                    storage.setnx.assert_not_called()
                    storage.set_many_atomic.assert_called_once_with({service.ACTIVE_GENERATION_KEY: ('g', None),
                                                                     service.LAST_GOOD_GENERATION_KEY: ('g', None)})

    def test_warmer_success_stages_three_windows_then_validates_publishes_releases(self):
        result, events, storage, _, publish, backoff, status = self.warmer('success')
        self.assertTrue(result['published'])
        self.assertEqual([event[0] for event in events],
                         ['lock', 'status', 'compute', 'compute', 'compute', 'validate_set', 'publish', 'status', 'release'])
        queries = [event[1] for event in events if event[0] == 'compute']
        self.assertEqual([(q['timeframe'][0], q['horizon'][0]) for q in queries], list(service.UI_COMBINATIONS))
        for query in queries:
            for flag in ['_cron', '_stage_only', '_sync_locked', 'refresh']:
                self.assertEqual(query[flag], ['1'])
            self.assertEqual(query['_generation_id'], [result['generation_id']])
        storage.setnx.assert_called_once_with(service.SYNC_LOCK_KEY, 'fixture-token', ex=service.SYNC_LOCK_TTL)
        storage.delete_if_value.assert_called_once_with(service.SYNC_LOCK_KEY, 'fixture-token')
        storage.delete.assert_called_once_with(service.REFRESH_BACKOFF_KEY)
        publish.assert_called_once()
        backoff.assert_not_called()

    def test_warmer_failure_paths_backoff_then_record_then_release(self):
        for mode, expected in [('compute_failure', 'generation_failed'), ('invalid', 'validation_failed'),
                               ('invalid_set', 'generation_validation_failed'), ('publish_failure', 'publish_failed')]:
            with self.subTest(mode=mode):
                result, events, storage, _, publish, backoff, status = self.warmer(mode)
                self.assertEqual(result['status'], expected)
                self.assertFalse(result['published'])
                self.assertEqual([event[0] for event in events[-3:]], ['backoff', 'status', 'release'])
                self.assertEqual(events[-2], ('status', 'failed'))
                backoff.assert_called_once()
                storage.delete.assert_not_called()
                if mode != 'publish_failure':
                    publish.assert_not_called()

    def test_autoheal_backoff_preserves_query_and_failure_diagnostics(self):
        instance = service.handler.__new__(service.handler)
        query = {'timeframe': ['60']}
        original = copy.deepcopy(query)
        initial = service.ReportUnavailableError('initial', {'source': 'initial'})
        stale = service.ReportUnavailableError('expired', {'source': 'stale'})
        with patch.object(service, '_get_cached_or_compute', side_effect=[initial, stale]) as read, \
                patch.object(service, '_refresh_backoff_status', return_value={'retry_after': 90}), \
                patch.object(instance, 'warm_all_combinations') as warm:
            with self.assertRaises(service.ReportUnavailableError) as caught:
                instance._get_report_with_auto_heal(query)
        self.assertEqual(query, original)
        self.assertEqual(read.call_args.args[0]['_allow_stale'], ['1'])
        self.assertEqual(caught.exception.details['initial'], initial.details)
        self.assertEqual(caught.exception.details['stale'], stale.details)
        self.assertEqual(caught.exception.details['backoff'], {'retry_after': 90})
        warm.assert_not_called()

    def test_force_warmer_rebuilds_even_when_health_is_fresh(self):
        result, events, storage, health, publish, backoff, status = self.warmer('fresh', force=True)
        self.assertTrue(result['published'])
        self.assertEqual(health.call_count, 2)
        storage.setnx.assert_called_once()
        storage.set_many_atomic.assert_not_called()
        publish.assert_called_once()
        self.assertEqual(sum(event[0] == 'compute' for event in events), 3)

    def test_autoheal_direct_without_handler_and_current_instance_callback(self):
        for status in ['fresh_noop', 'ok', 'locked']:
            report = {'meta': {'generation_id': 'g'}}
            initial = service.ReportUnavailableError('missing', {'source': 'fixture'})
            compute = Mock(side_effect=[initial, report])
            warm = Mock(return_value={'status': status, 'published': status == 'ok', 'generation_id': 'g'})
            result, heal = refresh._get_report_with_auto_heal(
                {'timeframe': ['60']}, compute=compute, backoff_status=lambda: None, warm=warm)
            warm.assert_called_once_with(trigger='request_autoheal', force=True)
            self.assertIs(result, report)
            expected = 'locked' if status == 'locked' else 'recovered'
            self.assertEqual(result['meta']['auto_heal']['status'], expected)
            self.assertEqual('_allow_stale' in compute.call_args.args[0], status == 'locked')
        instance = service.handler.__new__(service.handler)
        instance.warm_all_combinations = Mock()
        with patch.object(refresh, '_get_report_with_auto_heal', return_value=('report', None)) as implementation:
            self.assertEqual(instance._get_report_with_auto_heal({}), ('report', None))
        self.assertIs(implementation.call_args.kwargs['warm'], instance.warm_all_combinations)

    def test_dispatch_and_warmer_wrappers_resolve_current_dependencies(self):
        with patch.object(service, '_perform_optimized_pipeline') as build, \
                patch.object(dispatch, '_get_cached_or_compute') as implementation:
            self.assertIs(service._get_cached_or_compute({}), implementation.return_value)
        self.assertIs(implementation.call_args.kwargs['build'], build)
        with patch.object(service, '_get_cached_or_compute') as compute, \
                patch.object(refresh, 'warm_all_combinations') as implementation:
            self.assertIs(service.handler.warm_all_combinations(None), implementation.return_value)
        self.assertIs(implementation.call_args.kwargs['compute'], compute)
        self.assertEqual(implementation.call_args.args, ('cron', False))

    def test_method_parity_rejects_extra_wrapper_logic_or_receiver_calls(self):
        canonical = ast.parse(inspect.getsource(refresh._get_report_with_auto_heal)).body[0]
        spec = METHOD_BINDINGS['_get_report_with_auto_heal']
        original = original_definition(canonical, spec)
        handler = ast.parse(inspect.getsource(service.handler)).body[0]
        wrapper = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == canonical.name)
        verify_wrapper(wrapper, original, spec)
        changed_wrapper = copy.deepcopy(wrapper)
        changed_wrapper.body.append(ast.parse('unexpected()').body[0])
        with self.assertRaises(AssertionError):
            verify_wrapper(changed_wrapper, original, spec)
        changed = copy.deepcopy(canonical)
        changed.body.append(ast.parse('_warm()').body[0])
        with self.assertRaisesRegex(AssertionError, 'exactly one'):
            original_definition(changed, spec)
        changed = copy.deepcopy(canonical)
        changed.args.kw_defaults[-1] = ast.Constant(value=None)
        with self.assertRaisesRegex(AssertionError, 'explicit'):
            original_definition(changed, spec)
