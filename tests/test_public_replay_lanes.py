import importlib.util
from pathlib import Path
from threading import Event
from unittest import TestCase
from unittest.mock import patch,Mock
import sys

path=Path(__file__).parents[1]/'tools/probes/pulse_public_replay.py'
sys.path.insert(0,str(path.parent))
spec=importlib.util.spec_from_file_location('public_replay_lanes',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ReplayLaneTests(TestCase):
    def test_blocked_snapshot_cannot_hold_live_lane_or_its_lease(self):
        release=Event();seen=[];publishers={'snapshot':object(),'live':object()}
        def snapshot(pub,horizon,cursor):
            self.assertIs(pub,publishers['snapshot']);self.assertTrue(release.wait(1))
        def live(pub,universe,name):
            self.assertIs(pub,publishers['live']);seen.append(name);release.set()
        stats=module.run_lanes(publishers,[],.1,snapshot_work=snapshot,live_work=live)
        self.assertEqual(seen,['price','funding'])
        self.assertEqual(stats['snapshot']['failed_cycles'],0)
        self.assertGreaterEqual(stats['snapshot']['completed_cycles'],1)

    def test_failed_generation_is_not_marked_seen(self):
        manifest={'mapping_version':module.MAPPING,'generation':'g','parts':{}};seen={}
        with patch.object(module,'fetch',return_value=manifest),patch.object(module,'decode',return_value={'universe':[{}]*350}),patch.object(module,'publish',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):module.snapshot_cycle(Mock(),'24H',seen)
        self.assertEqual(seen,{})
        with patch.object(module,'fetch',return_value=manifest),patch.object(module,'decode') as decode:
            module.snapshot_cycle(Mock(),'24H',{'24H':'g'});decode.assert_not_called()

    def test_inconsistent_source_clock_is_refused_without_mutating_input(self):
        from copy import deepcopy
        value={'sequence':7,'generated_ms':1000,'rows':[{'id':0,'price':'1','source_ms':900,'volume':{'status':'available','source_ms':800,'value':'3'}}],
               'inputs':{'price:batch':{'source_ms':1000}}};original=deepcopy(value)
        with self.assertRaisesRegex(ValueError,'volume_clock'):module.price_document(value,[{'id':0,'pair_id':'B-BTC_USDT','symbol':'BTCUSDT'}])
        self.assertEqual(value,original)
        with self.assertRaisesRegex(ValueError,'membership'):module.price_document(value,[])

    def test_gap_logs_only_fixed_internal_reason_or_http_status_never_urls(self):
        import io,json
        from urllib.error import HTTPError
        for exc in (RuntimeError('publication_lease_busy'),RuntimeError('redis://user:secret@host'),HTTPError('https://private',429,'secret',{},None)):
            output=io.StringIO()
            with patch('sys.stdout',output):module.gap(exc,lane='live')
            record=json.loads(output.getvalue())
            self.assertNotIn('secret',output.getvalue());self.assertNotIn('private',output.getvalue())
            if isinstance(exc,HTTPError):self.assertEqual(record['http'],429)

    def test_final_telemetry_timeout_cannot_discard_the_run_receipt(self):
        import io,json
        clients=[Mock(),Mock()];clients[1].info.side_effect=TimeoutError()
        output=io.StringIO();stats={'price':{'publications':2,'failed_cycles':1}}
        with patch.object(module,'connect',side_effect=clients),patch.object(module,'run_lanes',return_value=stats),patch('sys.stdout',output):
            module.run('not-used',1)
        receipt=json.loads(output.getvalue())
        self.assertEqual(receipt['stats'],stats);self.assertEqual(receipt['final_memory_error_type'],'TimeoutError')
        self.assertEqual(receipt['status'],'replay_observed','completion is not an availability pass')
        for client in clients:client.close.assert_called_once()
