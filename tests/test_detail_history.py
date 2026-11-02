from decimal import Decimal
import unittest
from unittest.mock import Mock,patch

from pulse.collectors.preview_history import ring_points,row_history
from pulse.collectors import preview_pair as pair
from pulse.collectors.oi_history import history_spec
from pulse.store.bucket_ring import BucketRing
from pulse.store.capsule_model import footer
from pulse.http.wire_reader import route

NOW=1800000000000
META=dict(id=0,pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=4)

class HistoryTests(unittest.TestCase):
    def test_compressed_only_history_witness_is_exact_and_public_bytes_unchanged(self):
        import json,gzip
        from types import SimpleNamespace
        from pulse.collectors.preview_history import prepare_histories
        client=Mock();client.pipeline.return_value.execute.return_value=[b"",b""]
        registered=[]
        pub=SimpleNamespace(mapping_version="sha256:"+"b"*64,
            register_source=lambda name,body:(registered.append(body) or (1,"w")))
        compressed=prepare_histories(client,"a"*32,[META],{},pub,now_ms=NOW,
            retention_ms=720000,include_identity=False)[0]
        descriptor=json.loads(compressed.pointer)
        encoded=dict(compressed.blobs)[descriptor["variants"]["gzip"]["sha256"]]
        self.assertEqual(registered[-1],encoded)
        public=gzip.decompress(encoded)
        identity=prepare_histories(client,"a"*32,[META],{},pub,now_ms=NOW,
            retention_ms=720000,include_identity=True)[0]
        self.assertEqual(registered[-1],public)
        d=json.loads(identity.pointer)
        for variant in ('gzip','br11'):
            h=descriptor['variants'][variant]['sha256']
            self.assertEqual(h,d['variants'][variant]['sha256'])
            self.assertEqual(dict(identity.blobs)[h],dict(compressed.blobs)[h])

    def test_real_python_publication_is_accepted_by_browser_validator_and_hash(self):
        import json,subprocess
        from pathlib import Path
        from types import SimpleNamespace
        from pulse.collectors.preview_history import prepare_histories
        from pulse.wire.identity import digest
        ss=pair.stores(None,"a"*32,META)
        frames=[]
        for store in (ss[1],ss[2]):
            spec=store.spec;ring=BucketRing(spec.step_ms,spec.capacity,spec.columns)
            for stamp in (NOW-600000,NOW-300000):
                ring.write(stamp,(100.,) if spec.columns==1 else (1.5,.6,.4),now_ms=NOW)
            frames.append(self.frame(ring,spec))
        pipe=Mock();pipe.execute.return_value=frames
        client=Mock();client.pipeline.return_value=pipe
        publisher=SimpleNamespace(mapping_version=digest([META]),register_source=lambda *args:(1,"witness"))
        packages=prepare_histories(client,"a"*32,[META],{},publisher,now_ms=NOW)
        descriptor=json.loads(packages[0].pointer)
        body=dict(packages[0].blobs)[descriptor["variants"]["identity"]["sha256"]]
        script="""const H=require('./frontend/src/pulse/pulse_history.js'),W=require('./frontend/src/pulse/pulse_wire.js');
const fs=require('fs'),{webcrypto}=require('crypto');const d=JSON.parse(fs.readFileSync(0,'utf8'));
H.validate(d,d.mapping_version,0,d.generated_ms);const g=d.generation;delete d.generation;
W.digest(d,webcrypto.subtle).then(h=>{if(h!==g)throw Error('hash mismatch');console.log('passed')});"""
        result=subprocess.run(["node","-e",script],input=body,capture_output=True,cwd=Path(__file__).resolve().parents[1],timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual(result.stdout.strip(),b"passed")

    def test_nonzero_history_slice_keeps_actual_release_bucket(self):
        import json
        from types import SimpleNamespace
        from pulse.collectors.preview_history import prepare_histories
        client=Mock();client.pipeline.return_value.execute.return_value=[b"",b""]
        pub=SimpleNamespace(mapping_version="sha256:"+"b"*64,register_source=lambda *a:(1,"w"))
        meta={**META,"id":349}
        packages=prepare_histories(client,"a"*32,[meta],{},pub,now_ms=NOW,retention_ms=720000)
        self.assertEqual(packages[0].channel,"history:34")
        descriptor=json.loads(packages[0].pointer)
        body=dict(packages[0].blobs)[descriptor["variants"]["identity"]["sha256"]]
        doc=json.loads(body);self.assertEqual(doc["bucket"],34);self.assertEqual(doc["rows"][0]["id"],349)
        self.assertEqual(doc["expires_ms"]-doc["generated_ms"],720000)

    def frame(self,ring,spec):
        return ring.to_bytes()+footer(spec,bytes(spec.derived_bytes),bytes(spec.coverage_bytes))

    def test_ring_retained_values_are_not_percent_changes_and_conflicts_are_omitted(self):
        spec=history_spec(META["pair_id"],META["symbol"])
        ring=BucketRing(spec.step_ms,spec.capacity,spec.columns)
        ring.write(NOW-600000,(100.,),now_ms=NOW)
        ring.write(NOW-300000,(110.,),now_ms=NOW)
        self.assertEqual(ring_points(self.frame(ring,spec),spec,now_ms=NOW),[[NOW-600000,"100.0"],[NOW-300000,"110.0"]])
        with self.assertRaises(ValueError):ring.write(NOW-300000,(120.,),now_ms=NOW)
        self.assertEqual(len(ring_points(self.frame(ring,spec),spec,now_ms=NOW)),1)
        with self.assertRaisesRegex(ValueError,"identity"):
            ring_points(self.frame(ring,spec)[:-1],spec,now_ms=NOW)

    def test_signed_funding_changes_and_empty_history_are_explicit(self):
        record={"funding_history":[[NOW-14400000,"0.0001"],[NOW,"-0.0002"]]}
        row=row_history(META,record,[],[])
        self.assertEqual(row["series"]["funding_change"]["points"],[[NOW,"-0.0003"]])
        self.assertEqual(row["series"]["open_interest"]["unit"],"quantity")
        self.assertEqual(row["series"]["long_short"]["status"],"unavailable")

    def test_only_bounded_history_routes(self):
        for id,bucket in (("0","0"),("9","0"),("10","1"),("49","4"),("50","5"),("349","34")):
            self.assertEqual(route({"history":[id]})[1],"history:"+bucket)
        for value in ("350","-1","01","1.0","../foo"):
            with self.assertRaises(ValueError):route({"history":[value]})

    def test_oi_missing_baseline_repair_and_top_tip_do_not_wait_for_candles(self):
        source=Mock();source.clock.return_value=NOW
        own,cold,global_,top=(Mock() for _ in range(4))
        own.read.return_value=dict(status="available",source_ms=NOW)
        own.baseline.return_value=dict(status="unavailable",reason="baseline_missing")
        cold.baseline.side_effect=[dict(status="available"),dict(status="available"),dict(status="unavailable",reason="baseline_missing")]
        point=Mock(source_ms=NOW)
        with patch.object(pair,"stores",return_value=(own,cold,global_,top)),patch.object(pair,"parse_ls",return_value=(point,)),patch.object(pair,"parse_history",return_value=()),patch.object(pair,"save_points"):
            result=pair.collect_detail(None,"run",META,source,{"last_collected_ms":1})
        self.assertEqual(result["top_refreshed_ms"],NOW)
        self.assertEqual(result["last_collected_ms"],1)
        self.assertEqual(source.request.call_count,2)
        self.assertEqual(source.request.call_args.args[1],{"symbol":"BTCUSDT","period":"5m","limit":320})

    def test_pressure_maintenance_refreshes_top_without_history_backfill(self):
        source=Mock();source.clock.return_value=NOW
        own,cold,global_,top=(Mock() for _ in range(4));point=Mock(source_ms=NOW)
        own.read.return_value={"status":"unavailable"}
        with patch.object(pair,"stores",return_value=(own,cold,global_,top)),patch.object(pair,"parse_ls",return_value=(point,)),patch.object(pair,"save_points") as save:
            result=pair.collect_detail(None,"run",META,source,{},top_refresh_ms=900000,
                repair=False,maintenance_only=True)
        self.assertEqual(result["top_source_ms"],NOW)
        source.request.assert_called_once_with("/futures/data/topLongShortAccountRatio",
            {"symbol":"BTCUSDT","period":"5m","limit":3})
        own.read.assert_called_once();cold.baseline.assert_not_called()
        save.assert_called_once_with(top,(point,),require_existing=True)

    def test_pressure_repair_uses_one_admitted_window_and_only_existing_cold_ring(self):
        source=Mock();source.clock.return_value=NOW
        own,cold,global_,top=(Mock() for _ in range(4))
        own.read.return_value=dict(status="available",source_ms=NOW)
        own.baseline.return_value=dict(status="unavailable",reason="baseline_missing")
        cold.baseline.return_value=dict(status="unavailable",reason="baseline_missing")
        point=Mock(source_ms=NOW-300000)
        with patch.object(pair,"stores",return_value=(own,cold,global_,top)),\
                patch.object(pair,"parse_history",return_value=(point,)),patch.object(pair,"save_points") as save:
            result=pair.collect_detail(None,"run",META,source,{"top_refreshed_ms":NOW},maintenance_only=True)
        source.request.assert_called_once_with("/futures/data/openInterestHist",
            {"symbol":"BTCUSDT","period":"5m","limit":320})
        self.assertEqual(own.baseline.call_count,3)
        save.assert_called_once_with(cold,(point,),require_existing=True)
        self.assertEqual(result["cold_source_ms"],point.source_ms)

    def test_existing_only_save_refusal_releases_lease_without_writing(self):
        store=Mock();store.initialise.side_effect=RuntimeError("existing_ring_required")
        with self.assertRaisesRegex(RuntimeError,"existing_ring_required"):
            pair.save_points(store,(Mock(),),require_existing=True)
        store.initialise.assert_called_once_with(require_existing=True)
        store.write_many.assert_not_called();store.release.assert_called_once()

    def test_existing_repair_keeps_sixteen_slot_batch_bound(self):
        store=Mock();points=tuple(range(320))
        pair.save_points(store,points,require_existing=True)
        store.initialise.assert_called_once_with(require_existing=True)
        self.assertEqual(store.write_many.call_count,20)
        self.assertTrue(all(len(c.args[0])==16 for c in store.write_many.call_args_list))
        store.release.assert_called_once()

    def test_phased_reference_refresh_does_not_get_suppressed_by_old_cache_guard(self):
        source=Mock();source.clock.return_value=NOW
        source.request.return_value={'s':'ok','data':[]}
        prior={'opens':[[NOW-86400000,'1']],'references_prefetch_until_ms':NOW+22*60000}
        bulk={'prices':{META['pair_id']:{'mkt':META['symbol'],'ls':'1','btST':NOW}}}
        self.assertEqual(pair.collect_references(META,source,bulk,prior),prior)
        source.request.assert_not_called()
        result=pair.collect_references(META,source,bulk,prior,force_prefetch=True)
        self.assertEqual(source.request.call_count,3)
        self.assertEqual(result['references_prefetch_until_ms'],NOW//60000*60000+60000)
        self.assertEqual(result['opens'],[[NOW-86400000,'1']])
        with self.assertRaisesRegex(ValueError,'reference_prefetch_policy'):
            pair.collect_references(META,source,bulk,prior,force_prefetch=1)

    def test_all_three_missing_horizons_share_one_bounded_history_request(self):
        source=Mock();source.clock.return_value=NOW
        own,cold,global_,top=(Mock() for _ in range(4))
        own.read.return_value=dict(status="available",source_ms=NOW)
        own.baseline.return_value=dict(status="unavailable",reason="baseline_missing")
        cold.baseline.return_value=dict(status="unavailable",reason="baseline_missing")
        point=Mock(source_ms=NOW-300000)
        with patch.object(pair,"stores",return_value=(own,cold,global_,top)),\
                patch.object(pair,"parse_history",return_value=(point,)),patch.object(pair,"save_points") as save:
            result=pair.collect_detail(None,"run",META,source,{"top_refreshed_ms":NOW})
        self.assertEqual(source.request.call_count,1)
        self.assertEqual(source.request.call_args.args[1]["limit"],320)
        self.assertEqual(result["cold_source_ms"],NOW-300000)
        save.assert_called_once_with(cold,(point,))

    def test_conflicting_baseline_does_not_trigger_replacement(self):
        for maintenance in (False,True):
            for reason in ("exact_timestamp_conflict","discarded_baseline_candidate","ring_integrity_failed"):
                with self.subTest(maintenance=maintenance,reason=reason):
                    source=Mock();source.clock.return_value=NOW
                    own,cold,global_,top=(Mock() for _ in range(4))
                    own.read.return_value=dict(status="available",source_ms=NOW)
                    own.baseline.return_value=dict(status="unavailable",reason="baseline_missing")
                    cold.baseline.return_value=dict(status="unavailable",reason=reason)
                    with patch.object(pair,"stores",return_value=(own,cold,global_,top)):
                        pair.collect_detail(None,"run",META,source,{"top_refreshed_ms":NOW},maintenance_only=maintenance)
                    source.request.assert_not_called()
