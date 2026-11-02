from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock,patch
import time
from pulse.collectors.all_pair_policy import ensure_ready,TRANSITION,OLD_CONTROL,READY_KEY
from pulse.collectors import preview_sources as sources
from pulse.collectors.preview_pair import legacy_oi_rows,collect_funding
from pulse.store.funding_cache import encode
from tests.test_runtime_funding import fixture,NOW


class AllPolicyTests(TestCase):
    def test_transition_is_waited_and_not_silently_reset(self):
        client=Mock();client.eval.return_value=[b"waiting",b"1800000300000",b"0"]
        self.assertEqual(ensure_ready(client),(False,1800000300000))
        client.eval.assert_called_once_with(TRANSITION,2,OLD_CONTROL,READY_KEY)
        client.eval.return_value=[b"ready",b"1800000300000",b"0"]
        self.assertEqual(ensure_ready(client),(True,1800000300000))
        client.eval.return_value=[b"policy_gate_invalid"]
        with self.assertRaises(RuntimeError):ensure_ready(client)

    def test_all_policy_caps_and_wait_precede_every_source_dispatch(self):
        client=Mock()
        src=sources.Sources(client,scope="vercel-bom1-all-v1",deadline=time.monotonic()+100)
        self.assertEqual(src.binance.limits["history"].cap,850)
        self.assertEqual(src.binance.limits["weight"].cap,1680)
        self.assertEqual(src.pacer.binance_gap,.084)
        self.assertEqual(sources.Sources(client,scope="vercel-bom1-c1",deadline=100).pacer.binance_gap,.15)
        with patch("pulse.collectors.all_pair_policy.ensure_ready",return_value=(False,1)),\
                patch.object(sources,"fetch") as fetch,patch.object(src.binance,"preflight"):
            with self.assertRaises(sources.AdmissionDenied):src.initialise()
            fetch.assert_not_called()
            src.dcx.initialise=Mock()
            with self.assertRaises(sources.AdmissionDenied):src.request("/fixture",{})
            fetch.assert_not_called()
        self.assertEqual(sources.Sources(client,scope="vercel-bom1-c1",deadline=100).binance.limits["history"].cap,700)
        for gap in (.049,.151,True):
            with self.assertRaises(ValueError):sources.DispatchPacer(binance_gap=gap)

    def test_hourly_oi_exact_reuse_old_bucket_refresh_and_invalid_cache(self):
        source=Mock();source.clock.return_value=NOW
        bucket=NOW//3600000*3600000
        rows=[dict(symbol="BTCUSDT",timestamp=bucket,sumOpenInterest="1.2300")]
        source.request.return_value=rows;record={}
        self.assertEqual(legacy_oi_rows(source,"BTCUSDT",record,reuse=True),rows)
        source.request.reset_mock()
        self.assertEqual(legacy_oi_rows(source,"BTCUSDT",record,reuse=True),rows)
        source.request.assert_not_called()
        source.clock.return_value=bucket+3600000
        legacy_oi_rows(source,"BTCUSDT",record,reuse=True)
        source.request.assert_called_once()
        for bad in (["bad"],[dict(rows[0],symbol="ETHUSDT")],[dict(rows[0],timestamp=bucket+1)]):
            with self.assertRaises(ValueError):legacy_oi_rows(source,"BTCUSDT",{"legacy_oi_history":bad},reuse=True)

    def test_same_settlement_reuses_beyond_fifteen_minutes_without_retimestamping(self):
        report=fixture(4);source=Mock();source.clock.return_value=NOW+1000000
        record=dict(instrument={},instrument_ms=NOW+1000000,funding_report=encode(report).payload.decode(),
            funding_history=[[report.latest.time_ms,str(report.latest.rate)]],funding_refreshed_ms=NOW,
            nft=report.metadata.next_funding_ms)
        fresh=replace(report.metadata,source_ms=NOW+1000000)
        with patch("pulse.collectors.preview_pair.inputs",return_value=(fresh,None)):
            result=collect_funding(None,"run",{"pair_id":"B-BTC_USDT","symbol":"BTCUSDT","funding_hours":4,
                "mapping_version":"mapping"},source,{},dict(data={},source_ms=NOW+1000000),
                dict(data={"BTCUSDT":{}}),record,settlement_only=True)
        self.assertEqual(result,record)
        source.request.assert_not_called()
