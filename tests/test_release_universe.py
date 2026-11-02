from copy import deepcopy
import unittest
import json
from unittest.mock import Mock,patch
from decimal import Decimal
from pathlib import Path

from pulse.collectors.release_universe import freeze_top_volume,capsule_run,load,bootstrap_refused
from pulse.collectors.release_setup import seed_record,run as setup
from pulse.collectors import release_setup

NOW=1800000000000


class ReleaseUniverseTests(unittest.TestCase):
    def setUp(self):
        self.rows=[dict(id=i,pair_id=f"B-P{i}_USDT",symbol=f"P{i}USDT",funding_hours=(1,4,8)[i%3]) for i in range(360)]
        self.bulk=dict(ts=NOW,prices={r["pair_id"]:dict(mkt=r["symbol"],v=str(i)) for i,r in enumerate(self.rows)})

    def test_recorded_release_manifest_has_exact_dense_350_mapping(self):
        from pulse.wire.identity import digest
        value=json.loads(Path('docs/fixtures/release_top350_2026-10-28.json').read_text())
        self.assertEqual(len(value['universe']),350)
        self.assertEqual(digest(value['universe']),value['mapping_version'])
        self.assertEqual([r['id'] for r in value['universe']],list(range(350)))
        self.assertEqual(value['cutoff']['pair_id'],'B-SKR_USDT')

    def test_top350_exact_quote_volume_dense_mapping_no_reserved_slots(self):
        original=deepcopy(self.rows)
        result=freeze_top_volume(self.rows,self.bulk,now_ms=NOW)
        self.assertEqual(len(result["universe"]),350)
        self.assertEqual({r["pair_id"] for r in result["universe"]},{r["pair_id"] for r in self.rows[10:]})
        self.assertEqual([r["id"] for r in result["universe"]],list(range(350)))
        self.assertEqual(self.rows,original)
        self.assertEqual(result,freeze_top_volume(list(reversed(self.rows)),self.bulk,now_ms=NOW))

    def test_exact_decimal_order_and_deterministic_tie(self):
        for point in self.bulk["prices"].values():point["v"]="0"
        self.bulk["prices"]["B-P9_USDT"]["v"]="1.0000000000000000000000000001"
        self.bulk["prices"]["B-P8_USDT"]["v"]="1"
        result=freeze_top_volume(self.rows,self.bulk,now_ms=NOW)
        self.assertEqual([r["pair_id"] for r in result["ranking"][:2]],["B-P9_USDT","B-P8_USDT"])
        self.assertEqual(result["ranking"][2]["pair_id"],"B-P0_USDT")
        self.bulk["prices"]["B-P9_USDT"]["v"]=Decimal("1.0000000000000000000000000001")
        self.assertEqual(freeze_top_volume(self.rows,self.bulk,now_ms=NOW),result)

    def test_invalid_candidate_never_silently_excluded_or_zero_filled(self):
        for value in (None,True,1.2,"NaN","Infinity","-1","bad"):
            with self.subTest(value=value):
                bulk=deepcopy(self.bulk);bulk["prices"]["B-P0_USDT"]["v"]=value
                with self.assertRaises(ValueError):freeze_top_volume(self.rows,bulk,now_ms=NOW)
        for delta in (-60000,5001):
            bulk=deepcopy(self.bulk);bulk["ts"]=NOW+delta
            with self.assertRaises(ValueError):freeze_top_volume(self.rows,bulk,now_ms=NOW)
        with self.assertRaises(ValueError):freeze_top_volume(self.rows[:349],self.bulk,now_ms=NOW)
        rows=deepcopy(self.rows);rows[-1]=rows[0]
        with self.assertRaises(ValueError):freeze_top_volume(rows,self.bulk,now_ms=NOW)

    def test_frozen_load_uses_its_mapping_not_new_volume_rank(self):
        value=freeze_top_volume(self.rows,self.bulk,now_ms=NOW)
        doc=Mock();doc.read.return_value=dict(status="available",payload=json.dumps(value))
        with patch('pulse.store.runtime_document.RuntimeDocument',return_value=doc):
            self.assertEqual(load(None,'a'*32,value['mapping_version'],now_ms=NOW),value['universe'])
            with self.assertRaisesRegex(ValueError,'release_manifest_identity'):
                load(None,'a'*32,'sha256:'+'0'*64,now_ms=NOW)
            value['universe'].pop();doc.read.return_value['payload']=json.dumps(value)
            with self.assertRaises(ValueError):load(None,'a'*32,value['mapping_version'],now_ms=NOW)
            doc.read.return_value=dict(status='unavailable')
            with self.assertRaisesRegex(RuntimeError,'release_manifest_unavailable'):
                load(None,'a'*32,'sha256:'+'0'*64,now_ms=NOW)

    def test_raw_capsule_reuse_is_preview_only_and_requires_release_flag(self):
        self.assertEqual(capsule_run({},'a'*32),'a'*32)
        env=dict(VERCEL_ENV='preview',PULSE_RELEASE_TOP350='1',PULSE_CAPSULE_RUN_ID='b'*32)
        self.assertEqual(capsule_run(env,'a'*32),'b'*32)
        for change in (dict(VERCEL_ENV='production'),dict(PULSE_RELEASE_TOP350='0'),dict(PULSE_CAPSULE_RUN_ID='bad')):
            with self.assertRaises(ValueError):capsule_run({**env,**change},'a'*32)
        with self.assertRaisesRegex(RuntimeError,'release_manifest_operator_required'):bootstrap_refused()

    def test_seed_preserves_original_witness_clocks_not_mapping_bound_results(self):
        prior=dict(derived={'mapping_version':'old'},funding_report='old',last_collected_ms=NOW,
            funding_refreshed_ms=NOW,nft=NOW,legacy_source_ms=NOW-1000,
            cold_refreshed_ms=NOW-2000,funding_history=[[NOW-3000,'0.01']],opens=[[NOW-4000,'1']])
        result=seed_record(prior)
        self.assertNotIn('derived',result);self.assertNotIn('funding_report',result)
        self.assertNotIn('last_collected_ms',result);self.assertNotIn('nft',result)
        for key in ('legacy_source_ms','cold_refreshed_ms','funding_history','opens'):
            self.assertEqual(result[key],prior[key])
        result['opens'].clear();self.assertTrue(prior['opens'])

    def test_setup_refuses_production_before_connection(self):
        with patch('pulse.collectors.release_setup.connect') as connect:
            with self.assertRaisesRegex(ValueError,'release_setup_preview_only'):
                setup(run_id='a'*32,mapping_version='sha256:'+'b'*64,duration_ms=55000,
                    env=dict(VERCEL_ENV='production',PULSE_RELEASE_TOP350='1'))
            connect.assert_not_called()

    def setup_rig(self):
        client=Mock();client.time.return_value=(NOW//1000,0);client.eval.return_value=1
        manifest=Mock(owner=b'owner');manifest.publish.return_value=[b'published']
        manifest.read.return_value=dict(status='unavailable',reason='document_missing',revision=0)
        old=Mock();publisher=Mock()
        state=Mock();state.read_many.return_value={r['symbol']:None for r in self.rows[10:]}
        prior=Mock();prior.read_many.return_value={r['symbol']:dict(last_collected_ms=NOW,derived={'old':1},cold_refreshed_ms=NOW-100) for r in self.rows[10:]}
        source=Mock();source.clock.return_value=NOW
        env=dict(VERCEL_ENV='preview',PULSE_RELEASE_TOP350='1',PULSE_CAPSULE_RUN_ID='b'*32,
            PULSE_RELEASE_SOURCE_MAPPING='sha256:'+'c'*64)
        return client,manifest,old,publisher,state,prior,source,env

    def test_setup_seals_once_copies_only_selected_raw_records_and_never_uses_new_mapping_for_old_reports(self):
        c,m,o,p,s,old,source,env=self.setup_rig()
        with patch.multiple(release_setup,connect=Mock(return_value=c),RuntimeDocument=Mock(return_value=m),
                ArtifactPublisher=Mock(side_effect=[o,p]),WorkerState=Mock(side_effect=[s,old]),
                Sources=Mock(return_value=source),load_source=Mock(return_value=self.rows),fetch_bulk=Mock(return_value=self.bulk)):
            result=setup(run_id='a'*32,mapping_version='sha256:'+'c'*64,duration_ms=55000,env=env)
        self.assertEqual(result['pairs'],350);self.assertEqual(result['copied_records'],350)
        self.assertEqual(result['status'],'published');self.assertEqual(result['deletions'],0)
        self.assertTrue(all('derived' not in call.args[1] and 'last_collected_ms' not in call.args[1] for call in s.write.call_args_list))
        self.assertEqual({call.args[0] for call in s.write.call_args_list},{r['symbol'] for r in self.rows[10:]})
        m.publish.assert_called_once();p.release.assert_called_once();c.close.assert_called_once()

    def test_setup_active_old_writer_refuses_before_exchange_or_copy(self):
        c,m,o,p,s,old,source,env=self.setup_rig();c.eval.return_value=0
        with patch.multiple(release_setup,connect=Mock(return_value=c),RuntimeDocument=Mock(return_value=m),
                ArtifactPublisher=Mock(return_value=o),Sources=Mock(return_value=source)):
            with self.assertRaisesRegex(RuntimeError,'release_source_writer_active'):
                setup(run_id='a'*32,mapping_version='sha256:'+'c'*64,duration_ms=55000,env=env)
        source.initialise.assert_not_called();m.acquire.assert_not_called();c.close.assert_called_once()
