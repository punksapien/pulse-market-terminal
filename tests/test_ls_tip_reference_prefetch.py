from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from pulse.collectors import preview_pair as pair
from pulse.collectors.price_references import requests_for,parse_opens
from pulse.metrics.price_change import PriceTick,CandleOpen,build_ladder

NOW=1800000000000

class PrefetchTests(unittest.TestCase):
    def test_reference_repair_is_independent_bounded_and_reuses_known_opens(self):
        source=Mock();source.clock.return_value=NOW
        meta=dict(pair_id="B-BTC_USDT",symbol="BTCUSDT")
        bulk={"prices":{"B-BTC_USDT":{"mkt":"BTCUSDT","ls":"100","btST":NOW}}}
        plans=requests_for(PriceTick(meta["pair_id"],NOW,Decimal(100)),now_ms=NOW,prefetch_minutes=26)
        old=[[stamp,"100"] for plan in plans for stamp in plan["needed"]]
        prior={"opens":old+[[NOW-99*86400000,"2"]],"last_collected_ms":NOW-1000000}
        source.request.return_value={"data":[]}
        result=pair.collect_references(meta,source,bulk,prior)
        self.assertEqual(source.request.call_count,3)
        self.assertEqual(len(result["opens"]),78)
        self.assertEqual(result["last_collected_ms"],prior["last_collected_ms"])
        self.assertEqual(result["references_prefetch_until_ms"],NOW+22*60000)
        source.request.reset_mock()
        self.assertEqual(pair.collect_references(meta,source,bulk,result),result)
        source.request.assert_not_called()
        source.clock.return_value=NOW+20*60000
        pair.collect_references(meta,source,bulk,result)
        self.assertEqual(source.request.call_count,6,"three forward ranges plus three old-tick fallback minutes")

    def test_twenty_six_historical_minutes_cover_twenty_two_ladder_starts(self):
        tick=PriceTick("B-BTC_USDT",NOW,Decimal(100))
        plans=requests_for(tick,now_ms=NOW,prefetch_minutes=26)
        self.assertEqual(len(plans),3)
        opens={}
        for plan in plans:
            self.assertEqual(len(plan["needed"]),26)
            self.assertLess(max(plan["needed"]),NOW)
            payload={"data":[{"time":stamp,"open":"100"} for stamp in plan["needed"]]}
            opens.update(parse_opens(payload,plan))
        for minute in (0,1,21):
            ladder=build_ladder(opens,pair_id=tick.pair_id,mapping_version="fixture",definition_version="price_change.v1",
                generation_id="fixture",now_ms=NOW+minute*60000)
            self.assertTrue(all(point is not None for row in ladder.slots for point in row))
        ladder=build_ladder(opens,pair_id=tick.pair_id,mapping_version="fixture",definition_version="price_change.v1",
            generation_id="fixture",now_ms=NOW+22*60000)
        self.assertTrue(any(point is None for row in ladder.slots for point in row))
        with self.assertRaisesRegex(ValueError,"reference_prefetch_bound"):requests_for(tick,now_ms=NOW,prefetch_minutes=1000)
        with self.assertRaisesRegex(ValueError,"reference_candle_bound"):parse_opens({"data":[{}]*33},plans[0])

    def test_tip_updates_current_only_not_full_history_or_context(self):
        source=Mock();source.clock.return_value=NOW
        store=Mock();meta=dict(pair_id="B-BTC_USDT",symbol="BTCUSDT")
        prior=dict(global_refreshed_ms=NOW-999999,last_collected_ms=NOW-12345)
        point=SimpleNamespace(source_ms=NOW-1000)
        with patch.object(pair,"stores",return_value=(None,None,store,None)),patch.object(pair,"parse_ls",return_value=(point,)),patch.object(pair,"save_points") as save:
            result=pair.collect_ls_tip(None,"run",meta,source,prior)
        self.assertEqual(result["global_tip_refreshed_ms"],NOW)
        self.assertEqual(result["global_refreshed_ms"],prior["global_refreshed_ms"])
        self.assertEqual(result["last_collected_ms"],prior["last_collected_ms"])
        self.assertEqual(source.request.call_args.args[1]["limit"],3);save.assert_called_once()
        for points,reason in (((),"ls_tip_empty"),((SimpleNamespace(source_ms=NOW-900000),),"ls_tip_expired")):
            with patch.object(pair,"stores",return_value=(None,None,store,None)),patch.object(pair,"parse_ls",return_value=points),patch.object(pair,"save_points") as save:
                with self.assertRaisesRegex(ValueError,reason):pair.collect_ls_tip(None,"run",meta,source,prior)
                save.assert_not_called()
