from decimal import Decimal
import unittest
from pulse.wire.live_price import compose_live_price
from pulse.wire.identity import digest

NOW=1800000000000
UNIVERSE=[dict(id=0,pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=8)]

class DecimalPriceTests(unittest.TestCase):
    def row(self,value):
        return compose_live_price(UNIVERSE,{"ts":NOW,"prices":{"B-BTC_USDT":{"mkt":"BTCUSDT","ls":value,"btST":NOW-1000}}},
            mapping_version=digest(UNIVERSE),batch_version="sha256:"+"a"*64,sequence=1,now_ms=NOW,
            price_max_age_ms=60000,validate=lambda value:None)["rows"][0]

    def test_exact_decimal_preserves_numeric_source_and_clock(self):
        for value in (Decimal("100.1250"),Decimal("1e-8"),Decimal("80460")):
            row=self.row(value)
            self.assertEqual(row["status"],"available")
            self.assertEqual(row["price"],str(value));self.assertEqual(row["source_ms"],NOW-1000)
            self.assertEqual(row["expires_ms"],NOW-1000+60000)

    def test_float_bool_nonfinite_and_precision_loss_stay_unavailable(self):
        for value in (100.125,True,Decimal("NaN"),Decimal("Infinity"),Decimal("100.00000000000000001")):
            row=self.row(value);self.assertEqual(row["status"],"unavailable");self.assertIsNone(row["price"])
