from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest
from jsonschema.exceptions import ValidationError
from pulse.metrics.levels import magnitude,classify_basis
from pulse.wire.validation import snapshot
from pulse.wire.identity import seal_fixture

ROOT=Path(__file__).parents[1]

class LevelContractTests(unittest.TestCase):
    def test_canonical_units_native_intervals_signs_and_ties(self):
        for definition,value,h,expected,unit in (
            ("funding.v1","0.0000125",1,"0","bps"),
            ("funding.v1","0",8,"1","bps"),
            ("funding.v1","0.0002",8,"1","bps"),
            ("funding_change.v1","-0.00005",4,"1","bps"),
            ("open_interest.v1","-0.12",None,"0.12","fraction")):
            self.assertEqual(magnitude(definition,value,hours=h),(Decimal(expected),unit))
        self.assertEqual(magnitude("long_short.v1","2"),magnitude("long_short.v1","0.5"))
        for value,level in (("0",1),("0.999",1),("1",2),("2",3),("3",4)):
            self.assertEqual(classify_basis(Decimal(value),("1","2","3")),level)
        for cuts in (("0","1","2"),("1","1","2"),("2","1","3"),("1","2")):
            with self.assertRaises(ValueError):classify_basis(Decimal(1),cuts)

    def fixture(self):return json.loads((ROOT/"docs/contracts/fixtures/snapshot.classified_all_l2.json").read_text())

    def test_all_five_metric_classifications_validate(self):snapshot(self.fixture())

    def test_wrong_basis_policy_window_and_forbidden_paths_fail_closed(self):
        for kind in ("basis","policy","window","cumulative","top","context","volatility_policy","current_delta"):
            s=self.fixture();r=s["rows"][0]
            if kind=="basis":r["funding"]["native_estimate"]["level_basis"]="999"
            if kind=="policy":r["ls"]["global_ratio"]["classification_version"]="wrong.v1"
            if kind=="window":r["oi"]["change_1h"]["classification_version"]="open_interest.v1.l2.24H.fixture"
            if kind=="cumulative":r["funding"]["cumulative_1d"]=deepcopy(r["funding"]["native_estimate"])
            if kind=="top":r["ls"]["top_ratio"]=deepcopy(r["ls"]["global_ratio"])
            if kind=="context":r["context"]["volatility_momentum"].update(classification_status="classified",classification_version="wrong",level=1)
            if kind=="volatility_policy":r["volatility"]["windows"]["1H"]["classification_version"]="wrong"
            if kind=="current_delta":r["funding_change"]["current_delta"]=deepcopy(r["funding_change"]["settled_last"])
            with self.subTest(kind=kind),self.assertRaises((ValueError,ValidationError)):
                snapshot(seal_fixture(s))
