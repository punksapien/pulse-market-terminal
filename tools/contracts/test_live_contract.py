from copy import deepcopy
import unittest
from jsonschema import Draft202012Validator,ValidationError
from live_spec import live_schema,live_fixtures,validate_live
from build import outputs


class LiveContractTests(unittest.TestCase):
    def setUp(self):
        self.main=outputs()["docs/contracts/fixtures/snapshot.partial.json"]
        self.fixtures=live_fixtures(self.main)

    def test_shapes_and_known_mapping(self):
        for name,value in self.fixtures.items():
            kind="funding" if name.startswith("live_funding") else "price"
            Draft202012Validator(live_schema(kind)).validate(value)
            if ".mapping_mismatch." in name:
                with self.assertRaisesRegex(ValueError,"mapping_mismatch"): self.validate(value)
            else: self.validate(value)

    def validate(self,value):
        validate_live(value,self.main["universe"],self.main["mapping_version"])

    def test_zero_funding_valid_price_zero_rejected(self):
        value=deepcopy(self.fixtures["live_funding.valid.json"])
        value["rows"][0]["efr"]="0"; self.validate(value)
        price=deepcopy(self.fixtures["live_price.valid.json"])
        price["rows"][0]["price"]="0"
        with self.assertRaises(ValueError): self.validate(price)

    def test_live_price_positive_fixture_is_inside_selected_ladder(self):
        from decimal import Decimal
        value=self.fixtures["live_price.in_ladder.json"]
        self.validate(value)
        for live,row in zip(value["rows"],self.main["rows"]):
            ladder=row["references"]["ladder"]
            self.assertGreaterEqual(live["source_ms"],ladder["start_ms"])
            self.assertLess(live["source_ms"],ladder["valid_until_ms"])
            slot=(live["source_ms"]-ladder["start_ms"])//60000
            for point in ladder["slots"][slot]:
                self.assertEqual((Decimal(live["price"])-Decimal(point["open_price"]))/Decimal(point["open_price"]),Decimal("0.03125"))

    def test_invalid_identity_expiry_and_unavailable_number(self):
        for kind in ("duplicate","expiry","future","input","interval","float","unavailable"):
            value=deepcopy(self.fixtures["live_funding.valid.json"])
            row=value["rows"][0]
            if kind=="duplicate": value["rows"][1]["id"]=0
            if kind=="expiry": row["expires_ms"]=row["source_ms"]+120001
            if kind=="future": row["source_ms"]=value["generated_ms"]+5001
            if kind=="input": row["input_refs"]=["missing"]
            if kind=="interval": row["h"]=8 if row["h"]==1 else 1
            if kind=="float": row["efr"]=0.001
            if kind=="unavailable": row.update(status="unavailable",reason="source_unavailable")
            with self.subTest(kind=kind),self.assertRaises((ValueError,ValidationError)):
                Draft202012Validator(live_schema("funding")).validate(value)
                self.validate(value)


if __name__=="__main__": unittest.main()
