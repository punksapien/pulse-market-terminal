from decimal import Decimal
from unittest.mock import Mock,patch
import unittest
from pulse.metrics.levels import magnitude
from pulse.wire.rows import empty_row,available
from pulse.wire.pilot_policy import apply
from pulse.wire.level_contract import validate_levels
from pulse.collectors import preview_pair as pair
from pulse.collectors.binance_ls import history_spec

NOW=1800000000000

class WindowPilotTests(unittest.TestCase):
    def test_window_funding_anchor_boundaries_and_native_independence(self):
        for window,scale in (("1D",3),("7D",21)):
            for native in (1,4,8):
                for rate,expected in (("0.0001",1),("0.000299",1),("0.0003",2),("0.0005",3),("0.001",4),("-0.0001",2)):
                    r=empty_row({"id":0,"funding_hours":native},"sha256:"+"a"*64)
                    key="cumulative_"+window.lower()
                    r["funding"][key]=available("fraction","funding.v1",str(Decimal(rate)*scale),
                        source_ms=NOW-1000,expires_ms=NOW+60000,input_refs=["raw"],now_ms=NOW)
                    apply(r);validate_levels(r)
                    self.assertEqual(r["funding"][key]["level"],expected)
                    self.assertIn(".l2."+window+".",r["funding"][key]["classification_version"])

    def test_settled_window_changes_use_native_rate_units_not_window_duration(self):
        for native in (1,4,8):
            for window in ("1D","7D"):
                for basis,expected in (("0",1),("0.999",1),("1",2),("5",3),("10",4),("-10",4)):
                    r=empty_row({"id":0,"funding_hours":native},"sha256:"+"a"*64)
                    key="settled_"+window.lower()
                    value=Decimal(basis)*native/80000
                    r["funding_change"][key]=available("fraction","funding_change.v1",str(value),
                        source_ms=NOW-1000,expires_ms=NOW+60000,input_refs=["raw"],now_ms=NOW)
                    apply(r);validate_levels(r)
                    self.assertEqual(r["funding_change"][key]["level"],expected)
        for definition in ("funding.v1","funding_change.v1"):
            with self.assertRaisesRegex(ValueError,"l2_funding_window"):magnitude(definition,"1",hours=8,window="invalid")

    def test_missing_window_stays_unclassified(self):
        r=empty_row({"id":0,"funding_hours":8},"sha256:"+"a"*64);apply(r)
        self.assertIsNone(r["funding"]["cumulative_7d"]["level"])
        self.assertIsNone(r["funding_change"]["settled_7d"]["level"])

    def test_full_classified_snapshot_roundtrips_with_window_bands(self):
        import json
        from pathlib import Path
        from pulse.wire.validation import snapshot
        from pulse.wire.identity import seal_fixture
        from pulse.wire.compact import encode,decode
        value=json.loads((Path(__file__).parents[1]/"docs/contracts/fixtures/snapshot.valid.1H.json").read_text())
        apply(value["rows"][0])
        value=seal_fixture(value)
        snapshot(value)
        self.assertEqual(decode(encode(value)),value)

class NativePeriodCollectionTests(unittest.TestCase):
    def source(self):
        source=Mock();source.clock.return_value=NOW
        ratios={"5m":("1","0.5","0.5",300000),"1h":("1.2","0.5455","0.4545",3600000),
            "4h":("2","0.6667","0.3333",14400000),"1d":("3","0.75","0.25",86400000)}
        def request(path,query):
            ratio,long,short,step=ratios[query["period"]]
            return [dict(symbol="BTCUSDT",timestamp=NOW//step*step,longShortRatio=ratio,longAccount=long,shortAccount=short)]
        source.request.side_effect=request
        return source

    def collect(self,source):
        store=Mock();store.spec=history_spec("B-BTC_USDT","BTCUSDT","global")
        with patch.object(pair,"stores",return_value=(None,None,store,None)),patch.object(pair,"save_points"):
            return pair.collect_ls_tip(None,"run",dict(pair_id="B-BTC_USDT",symbol="BTCUSDT"),source,
                {"last_collected_ms":NOW-12345},periods=True)

    def test_independent_period_values_and_clocks(self):
        source=self.source();r=self.collect(source)
        self.assertEqual([r["ls_periods"][w]["value"] for w in ("1H","4H","24H")],["1.2","2","3"])
        for window,step in (("1H",3600000),("4H",14400000),("24H",86400000)):
            self.assertEqual(r["ls_periods"][window]["expires_ms"],NOW//step*step+step+900000)
        self.assertEqual(r["last_collected_ms"],NOW-12345)
        self.assertEqual(source.request.call_count,4)

    def test_period_invalid_rows_fail_closed(self):
        for kind in ("alignment","future","wrong_symbol","shares","empty"):
            source=self.source();original=source.request.side_effect
            def request(path,query):
                rows=original(path,query)
                if query["period"]=="1h":
                    if kind=="alignment":rows[0]["timestamp"]-=300000
                    elif kind=="future":rows[0]["timestamp"]=NOW+3600000
                    elif kind=="wrong_symbol":rows[0]["symbol"]="ETHUSDT"
                    elif kind=="shares":rows[0]["longAccount"]="0.1"
                    elif kind=="empty":rows=[]
                return rows
            source.request.side_effect=request
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.collect(source)
