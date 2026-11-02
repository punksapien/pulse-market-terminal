"""ADR-0032: offline contracts for explicit transition/read/health dependencies."""
import copy
import unittest
from unittest.mock import Mock, patch

from pulse import config
from pulse.legacy import service, signal_state, reader, health


NOW = 1_792_900_000.0


def report(age=30):
    return {'meta': {'resolution': '60', 'scale_horizon': '1H', 'target_seconds': 3600,
                     'last_rebuilt': NOW-age, 'generation_id': 'g'},
            'summary': [{'instrument': 'B-BTC_USDT', 'volatility_score': .01,
                         'trade_signal': 'MomentumLong', 'price_last': 100}]}


def manifest(generation_id, age=30):
    return {'generation_id': generation_id, 'schema_version': config.REPORT_SCHEMA_VERSION,
            'model_version': config.MODEL_VERSION, 'published_at': NOW-age,
            'combinations': [{'resolution': tf, 'horizon': hz} for tf, hz in config.UI_COMBINATIONS]}


class SignalReaderHealthTests(unittest.TestCase):
    def test_state_validation_all_signals_and_invalid_numbers(self):
        for signal in config.TRACKED_TRADE_SIGNALS:
            state = {'signal': signal, 'since_price': '123.5', 'since_at': NOW-60}
            with self.subTest(signal=signal):
                result = signal_state._matching_signal_state_fields(state, signal)
                expected = {'signal_since_price': 123.5, 'signal_since_at': NOW-60}
                self.assertEqual(result, expected if signal in config.DISPLAYABLE_SIGNAL_STATES else None)
        for field, values in [('since_price', [None, 0, -1, 'bad', float('nan'), float('inf')]),
                              ('since_at', [None, 0, -1, 'bad', float('nan'), float('inf')])]:
            for value in values:
                state = {'signal': 'Range', 'since_price': 100, 'since_at': NOW}
                state[field] = value
                with self.subTest(field=field, value=value):
                    self.assertIsNone(signal_state._matching_signal_state_fields(state, 'Range'))
        for state in [None, [], {'signal': 'MomentumShort', 'since_price': 100, 'since_at': NOW}]:
            self.assertIsNone(signal_state._matching_signal_state_fields(state, 'MomentumLong'))

    def test_reconciliation_forwards_source_generation_and_pruning(self):
        storage = Mock()
        storage.reconcile_signal_states.return_value = {'accepted': True, 'states': {}}
        source = report()
        source['summary'] += [None, {}, {'instrument': 'bad', 'trade_signal': 'Range', 'price_last': 0}]
        before = copy.deepcopy(source)
        result = signal_state._reconcile_report_signal_states(source, '60', '1H', NOW, 'g', True, storage=storage)
        self.assertTrue(result['accepted'])
        storage.reconcile_signal_states.assert_called_once_with(
            f'{config.SIGNAL_STATE_KEY_PREFIX}:60:1H',
            [{'instrument': 'B-BTC_USDT', 'signal': 'MomentumLong', 'price': 100.0}],
            NOW, generation_id='g', prune=True)
        self.assertEqual(source, before)
        storage.reset_mock()
        for empty in [None, {}, {'summary': []}]:
            self.assertEqual(signal_state._reconcile_report_signal_states(
                empty, '60', '1H', NOW, 'g', storage=storage)['reason'], 'no_observations')
        storage.reconcile_signal_states.assert_not_called()

    def test_attachment_is_immutable_and_omits_mismatched_state(self):
        storage = Mock()
        source = report()
        source['summary'] += [{'instrument': 'B-ETH_USDT', 'trade_signal': 'Range'}, None]
        before = copy.deepcopy(source)
        storage.get_signal_states.return_value = {
            'B-BTC_USDT': {'signal': 'MomentumLong', 'since_price': 90, 'since_at': NOW-60},
            'B-ETH_USDT': {'signal': 'MomentumShort', 'since_price': 50, 'since_at': NOW-60}}
        result = signal_state._attach_signal_states(source, '60', '1H', storage=storage)
        self.assertEqual(source, before)
        self.assertIsNot(result, source)
        self.assertEqual(result['summary'][0]['signal_since_price'], 90)
        self.assertNotIn('signal_since_price', result['summary'][1])
        self.assertIsNone(result['summary'][2])
        storage.get_signal_states.assert_called_once_with(
            f'{config.SIGNAL_STATE_KEY_PREFIX}:60:1H', ['B-BTC_USDT', 'B-ETH_USDT'])
        storage.get_signal_states.return_value = {}
        self.assertIs(signal_state._attach_signal_states(source, '60', '1H', storage=storage), source)
        for empty in [None, {}, {'summary': []}]:
            storage.reset_mock()
            self.assertIs(signal_state._attach_signal_states(empty, '60', '1H', storage=storage), empty)
            storage.get_signal_states.assert_not_called()

    def test_best_effort_stops_on_storage_failure_and_preserves_watermark(self):
        first, second = report(), report()
        first['meta']['last_hydrated'] = NOW-5
        reports = [('60', '1H', 'key', first), ('240', '4H', 'other', second)]
        for failure in [RuntimeError('fixture'), {'reason': 'redis_error'}, {'reason': 'redis_unavailable'}]:
            callback = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            with self.subTest(failure=str(failure)), patch('builtins.print'):
                signal_state._best_effort_reconcile_generation_signal_states('g', reports, NOW, reconcile_report=callback)
            callback.assert_called_once_with(first, '60', '1H', source_at=NOW-5, generation_id='g', prune=True)
        callback = Mock(return_value={'accepted': True})
        signal_state._best_effort_reconcile_generation_signal_states('g', reports, NOW, reconcile_report=callback)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(callback.call_args.kwargs['source_at'], NOW)

    def test_reader_boundary_age_and_pointer_recovery_order(self):
        for allow_stale, limit in [(False, config.REPORT_REFRESH_AFTER), (True, config.REPORT_MAX_STALE)]:
            for age in [limit, limit+.001]:
                with self.subTest(allow_stale=allow_stale, age=age):
                    source, events = report(age), []
                    storage = Mock()
                    storage.is_available.return_value = True
                    storage.get.return_value = 'older'
                    storage.set_many_atomic.side_effect = lambda entries: events.append(('atomic', entries)) or True
                    provenance = {'generation_id': 'g', 'effective_age_seconds': age,
                                  'model_age_seconds': age, 'generation_age_seconds': age}
                    read = Mock(return_value=(source, provenance))
                    save = Mock(side_effect=lambda *args: events.append(('local', args)))
                    attach = Mock(side_effect=lambda *args: events.append(('attach', args)) or args[0])
                    fallback = Mock(return_value=None)
                    before = copy.deepcopy(source)
                    with patch.object(reader.time, 'time', return_value=NOW), patch.dict('os.environ', {'VERCEL': '1'}):
                        result, details = reader._resolve_target_report('key', '60', '1H', allow_stale,
                            storage=storage, candidate_ids=lambda: ['g'], read_report=read,
                            save_local=save, attach_states=attach, fallback=fallback)
                    self.assertEqual(source, before)
                    read.assert_called_once_with('g', 'key', '60', '1H', now=NOW)
                    if age <= limit:
                        self.assertEqual([event[0] for event in events], ['atomic', 'local', 'attach'])
                        self.assertEqual(events[0][1], {config.ACTIVE_GENERATION_KEY: ('g', None),
                                                       config.LAST_GOOD_GENERATION_KEY: ('g', None)})
                        self.assertEqual(details['source'], 'recovered_generation')
                        self.assertEqual(result['meta']['last_rebuilt'], NOW-age)
                        fallback.assert_not_called()
                    else:
                        self.assertIsNone(result)
                        self.assertEqual(details['reason'], 'no_acceptable_generation')
                        self.assertEqual(events, [])

    def test_reader_packaged_fallback_production_vs_local(self):
        source = report(config.REPORT_MAX_STALE+1)
        storage = Mock()
        storage.is_available.return_value = False
        for vercel in ['1', '0']:
            attach = Mock(side_effect=lambda *args: args[0])
            read, save, candidates = Mock(), Mock(), Mock()
            with patch.object(reader.time, 'time', return_value=NOW), patch.dict('os.environ', {'VERCEL': vercel}):
                result, details = reader._resolve_target_report('key', '60', '1H', True,
                    storage=storage, candidate_ids=candidates, read_report=read,
                    save_local=save, attach_states=attach, fallback=lambda _: source)
            self.assertEqual(result is None, vercel == '1')
            for callback in [read, save, candidates]:
                callback.assert_not_called()
            if vercel == '0':
                self.assertEqual(details['source'], 'packaged_fallback')
                self.assertEqual(result['meta']['last_rebuilt'], source['meta']['last_rebuilt'])
            else:
                attach.assert_not_called()

    def health_snapshot(self, age, pointer='active', missing=False, verify=True):
        storage = Mock()
        storage.is_available.return_value = True
        storage.get.side_effect = lambda key: 'g' if key == {
            'active': config.ACTIVE_GENERATION_KEY, 'last_good': config.LAST_GOOD_GENERATION_KEY}[pointer] else None
        load = Mock(side_effect=lambda key: manifest('g') if key.endswith(':manifest') else None)
        read = Mock(return_value=(None if missing else report(), {'reason': 'missing_report' if missing else 'ok',
                                                                  'model_age_seconds': age}))
        with patch.object(health.time, 'time', return_value=NOW):
            result = health._generation_health_snapshot(verify, storage=storage, load_json=load, read_report=read)
        return result, read

    def test_health_exact_freshness_boundaries_and_completeness(self):
        for age, status in [(config.REPORT_REFRESH_AFTER, 'fresh'),
                            (config.REPORT_REFRESH_AFTER+.001, 'stale_recoverable'),
                            (config.REPORT_MAX_STALE, 'stale_recoverable'),
                            (config.REPORT_MAX_STALE+.001, 'expired')]:
            with self.subTest(age=age):
                snapshot, read = self.health_snapshot(age)
                self.assertEqual(snapshot['freshness_status'], status)
                self.assertEqual(snapshot['healthy'], status == 'fresh')
                self.assertTrue(snapshot['complete'])
                self.assertEqual(read.call_count, len(config.UI_COMBINATIONS))
                self.assertEqual(snapshot['max_model_age_seconds'], round(age, 3))
        snapshot, _ = self.health_snapshot(30, pointer='last_good')
        self.assertFalse(snapshot['healthy'])
        self.assertIn('active_pointer_recovery_required', snapshot['reasons'])
        snapshot, _ = self.health_snapshot(30, missing=True)
        self.assertFalse(snapshot['complete'])
        self.assertEqual(snapshot['freshness_status'], 'unavailable')
        snapshot, read = self.health_snapshot(30, verify=False)
        read.assert_not_called()
        self.assertTrue(snapshot['complete'])  # Retain the original lightweight-health semantics.
        self.assertEqual(snapshot['combinations'], [])

    def test_health_unavailable_invalid_manifest_and_newest_pointer(self):
        storage, load, read = Mock(), Mock(), Mock()
        storage.is_available.return_value = False
        result = health._generation_health_snapshot(storage=storage, load_json=load, read_report=read)
        self.assertEqual(result['reasons'], ['redis_unavailable'])
        storage.get.assert_not_called()
        load.assert_not_called()
        read.assert_not_called()
        storage.is_available.return_value = True
        storage.get.return_value = 'g'
        load.return_value = None
        result = health._generation_health_snapshot(storage=storage, load_json=load, read_report=read)
        self.assertIn('no_valid_generation_manifest', result['reasons'])
        self.assertEqual(sum(call.args[0].endswith(':manifest') for call in load.call_args_list), 1)
        storage.get.side_effect = lambda key: 'old' if key == config.ACTIVE_GENERATION_KEY else 'new'
        load.side_effect = lambda key: (manifest('old', 60) if ':old:' in key else manifest('new', 30)) if key.endswith(':manifest') else None
        with patch.object(health.time, 'time', return_value=NOW):
            result = health._generation_health_snapshot(False, storage=storage, load_json=load, read_report=read)
        self.assertEqual(result['selected_generation'], 'new')
        self.assertEqual(result['pointer_source'], 'last_good')
        self.assertFalse(result['healthy'])

    def test_service_wrappers_use_current_callbacks(self):
        for callback_name, invocation, module, function, dependency in [
            ('_reconcile_report_signal_states', lambda: service._best_effort_reconcile_generation_signal_states('g', [], NOW),
             signal_state, '_best_effort_reconcile_generation_signal_states', 'reconcile_report'),
            ('_read_generation_report', lambda: service._resolve_target_report('key', '60', '1H'),
             reader, '_resolve_target_report', 'read_report'),
            ('_read_generation_report', lambda: service._generation_health_snapshot(),
             health, '_generation_health_snapshot', 'read_report')]:
            with self.subTest(function=function), patch.object(service, callback_name) as callback, patch.object(module, function) as implementation:
                self.assertIs(invocation(), implementation.return_value)
                self.assertIs(implementation.call_args.kwargs[dependency], callback)
