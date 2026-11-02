from copy import deepcopy
from decimal import Decimal
import unittest
from pulse.wire.live_price import compose_live_price
from pulse.wire.identity import digest
from pulse.wire.validation import live
from jsonschema.exceptions import ValidationError

NOW=1800000000000
UNIVERSE=[dict(id=0,pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=8)]
MAPPING=digest(UNIVERSE)

class FastVolumeTests(unittest.TestCase):
    def document(self, volume, *, price="100", stamp=NOW, batch=NOW):
        return compose_live_price(UNIVERSE,{"ts":batch,"prices":{"B-BTC_USDT":{
            "mkt":"BTCUSDT","ls":price,"btST":stamp,"v":volume}}},
            mapping_version=MAPPING,batch_version="sha256:"+"a"*64,sequence=NOW,
            now_ms=NOW,price_max_age_ms=60000,validate=lambda v:live(v,UNIVERSE,MAPPING))

    def test_volume_independent_of_price_and_trade_clock(self):
        for price,stamp in (("invalid",NOW),("100",NOW-60000)):
            row=self.document(Decimal("123456.789"),price=price,stamp=stamp)["rows"][0]
            self.assertEqual(row["status"],"unavailable")
            self.assertEqual(row["volume"]["value"],"123456.789")
            self.assertEqual(row["volume"]["source_ms"],NOW)
            self.assertEqual(row["volume"]["expires_ms"],NOW+300000)
            self.assertEqual(row["volume"]["definition_version"],"volume.rt.v2")

    def test_zero_and_invalid_volume_are_not_confused(self):
        self.assertEqual(self.document(0)["rows"][0]["volume"]["value"],"0")
        for raw in (None,-1,True,1.5,Decimal("NaN"),Decimal("Infinity"),"no"):
            row=self.document(raw)["rows"][0]
            self.assertEqual(row["status"],"available")
            self.assertEqual(row["volume"]["status"],"unavailable")
            self.assertIsNone(row["volume"]["value"])

    def test_batch_expiry_future_and_witness_fail_closed(self):
        self.assertEqual(self.document(1,batch=NOW-300000)["rows"][0]["volume"]["reason"],"stale_volume")
        self.assertEqual(self.document(1,batch=NOW-299999)["rows"][0]["volume"]["status"],"available")
        with self.assertRaisesRegex(ValueError,"future_live_price_batch"):
            self.document(1,batch=NOW+5001)
        doc=self.document(1)
        for key,value in (("source_ms",NOW-1),("expires_ms",NOW+300001),("value","-1"),("definition_version","unknown")):
            bad=deepcopy(doc);bad["rows"][0]["volume"][key]=value
            with self.assertRaises((ValueError,ValidationError)):live(bad,UNIVERSE,MAPPING)

    def test_old_documents_without_optional_volume_remain_valid(self):
        doc=self.document(1);del doc["rows"][0]["volume"]
        live(doc,UNIVERSE,MAPPING)
