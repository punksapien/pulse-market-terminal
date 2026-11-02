from decimal import Decimal
import unittest

from pulse.collectors.price_references import requests_for, parse_opens, collect
from pulse.metrics.price_change import PriceTick, fallback_change, price_change

NOW=1800000030000


class SparseReferencesTests(unittest.TestCase):
    def test_queries_select_eighteen_exact_minutes_without_history_ring(self):
        tick=PriceTick("B-BTC_USDT",NOW-60000,Decimal("110"))
        plans=requests_for(tick,now_ms=NOW)
        self.assertEqual(len(plans),3)
        self.assertEqual(sum(len(p["needed"]) for p in plans),18)
        self.assertTrue(all(p["to"]-p["from"]<=480 for p in plans))
        def request(path,params,**kwargs):
            self.assertEqual(path,"/market_data/candlesticks")
            return {"data":[{"time":t*1000,"open":Decimal("100.0")}
                            for t in range(params["from"],params["to"],60)]}
        view=collect(tick,request,now_ms=NOW,mapping_version="mapping",generation_id="generation")
        self.assertEqual(view["selected_open_count"],18)
        args=dict(horizon="24H",mapping_version="mapping",definition_version="price_change.v1",
                  generation_id="generation",now_ms=NOW)
        fallback=fallback_change(view["fallback"],pair_id=tick.pair_id,expires_at_ms=NOW+60000,**args)
        self.assertEqual(fallback.value_fraction,Decimal(".1"))
        latest=PriceTick(tick.pair_id,NOW,Decimal("110"))
        self.assertEqual(price_change(latest,view["ladder"],numerator_expires_at_ms=NOW+60000,**args).value_fraction,Decimal(".1"))

    def test_sparse_fallback_is_bounded_and_missing_never_filled(self):
        tick=PriceTick("B-BTC_USDT",NOW-3600000,Decimal("110"))
        plans=requests_for(tick,now_ms=NOW)
        self.assertEqual(len(plans),6)
        self.assertTrue(all(p["to"]-p["from"]<=480 for p in plans))
        view=collect(tick,lambda *a,**k:{"data":[]},now_ms=NOW,mapping_version="m",generation_id="g")
        self.assertTrue(all(c is None for row in view["ladder"].slots for c in row))
        plan=plans[0]; stamp=plan["needed"][0]
        for rows in ([{"time":stamp,"open":100.0}],
                     [{"time":stamp+1,"open":"100"}],
                     [{"time":stamp,"open":"100"},{"time":stamp,"open":"101"}],
                     [{"time":stamp,"open":"1.00000000000000001"}], [{}]*17):
            with self.assertRaises(ValueError): parse_opens({"data":rows},plan)
        self.assertEqual(parse_opens({"data":[]},plan),{})


if __name__=="__main__": unittest.main()
