from dataclasses import replace
from decimal import Decimal
import unittest

from pulse.wire.rows import empty_row,build,available
from pulse.wire.metric_rows import add_oi,add_funding,add_ls
from pulse.wire.identity import digest
from pulse.metrics.open_interest import OIObservation,quantity_change
from pulse.metrics.funding import settled_report,FundingEstimate,native_estimate
from pulse.metrics.long_short import LSMetric
import test_runtime_funding as funding_fixture


NOW=1800000000000


class RealRowAdapterTests(unittest.TestCase):
    def setUp(self):
        self.universe=[dict(id=0,pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=4)]
        self.mapping=digest(self.universe)
        self.row=empty_row(self.universe[0],self.mapping)
        self.inputs={"raw":{"version":"sha256:"+"c"*64,"source_ms":NOW,"verified_at_ms":None,
            "expires_ms":NOW+8*86400000,"definition_version":"derived.inputs.v1"}}

    def seal(self):
        return build(self.universe,[self.row],self.inputs,horizon="1H",now_ms=NOW)

    def test_empty_is_unavailable_not_zero_and_seals(self):
        value=self.seal()
        self.assertIsNone(value["rows"][0]["price"]["value"])
        self.assertEqual(value["rows"][0]["signal_since"]["reason"],"state_unverified")

    def test_invalid_assembly_still_refused_by_mandatory_wire_gate(self):
        from pulse.wire.composer import compose
        from pulse.wire.validation import snapshot
        self.row["price"]["unit"]="invalid_unit"
        with self.assertRaises(ValueError):self.seal()
        assembled=build(self.universe,[self.row],self.inputs,horizon="1H",now_ms=NOW,validate_now=False)
        with self.assertRaises(ValueError):
            compose(assembled,validate=snapshot,brotli_compress=lambda b,q:b"unused",
                input_revisions={"raw":1},now_ms=NOW)

    def test_historical_ladder_survives_expired_snapshot_numerator(self):
        from pulse.wire.reference_rows import attach
        from pulse.metrics.price_change import PriceTick,CandleOpen,WINDOW_MS
        opens={NOW-window:CandleOpen("B-BTC_USDT",NOW-window,Decimal(100)) for window in WINDOW_MS.values()}
        attach(self.row,PriceTick("B-BTC_USDT",NOW-60000,Decimal(103)),opens,
            mapping_version=self.mapping,input_ref="raw",price_expires_ms=NOW,now_ms=NOW)
        row=self.seal()["rows"][0]
        self.assertEqual(row["references"]["status"],"available")
        self.assertIsNone(row["references"]["fallback"])
        self.assertTrue(all(c["status"]=="unavailable" for c in row["changes"].values()))
        self.assertTrue(all(p["status"]=="available" for p in row["references"]["ladder"]["slots"][0]))

    def test_container_covers_publish_gap_without_freshening_fields(self):
        from pulse.store.artifact_publisher import PROMOTE
        from pulse.wire.rows import SNAPSHOT_ENVELOPE_MS
        self.assertIn("local maxage="+str(SNAPSHOT_ENVELOPE_MS), PROMOTE)
        self.assertIn("if p.history==true then maxage=720000 end",PROMOTE)
        self.assertIn("p.expiry>p.source+maxage",PROMOTE)
        self.assertNotIn("SNAPSHOT_MAX_AGE_MS", PROMOTE)
        self.row["price"]=available("price","price_change.v1","100",source_ms=NOW,
            expires_ms=NOW+60000,input_refs=["raw"],now_ms=NOW)
        snapshot=self.seal()
        self.assertEqual(snapshot["expires_ms"], NOW+180000)
        self.assertEqual(snapshot["rows"][0]["price"]["expires_ms"], NOW+60000)
        self.assertEqual(snapshot["inputs"]["raw"]["expires_ms"], self.inputs["raw"]["expires_ms"])

    def test_oi_baseline_provenance_and_stale_refusal(self):
        current=dict(status="available",source_ms=NOW,expires_ms=NOW+900000,sources=("120",))
        metric=quantity_change(OIObservation(NOW,120.,"B-BTC_USDT"),
            OIObservation(NOW-3600000,100.,"B-BTC_USDT"),horizon="1H",mode="own",now_ms=NOW)
        add_oi(self.row,current,{"1H":metric},input_ref="raw",now_ms=NOW)
        self.assertEqual(self.seal()["rows"][0]["oi"]["change_1h"]["value"],"0.2")
        self.assertEqual(available("price","price_change.v1","100",source_ms=NOW-60000,
            expires_ms=NOW,input_refs=["raw"],now_ms=NOW)["status"],"unavailable")

    def test_expired_retained_oi_change_keeps_required_null_provenance(self):
        current=dict(status="available",source_ms=NOW-900000,expires_ms=NOW,sources=("120",))
        metric=quantity_change(OIObservation(NOW-900000,120.,"B-BTC_USDT"),
            OIObservation(NOW-4500000,100.,"B-BTC_USDT"),horizon="1H",mode="own",now_ms=NOW-900000)
        add_oi(self.row,current,{"1H":metric},input_ref="raw",now_ms=NOW)
        field=self.seal()["rows"][0]["oi"]["change_1h"]
        self.assertEqual(field["status"],"unavailable")
        self.assertIsNone(field["value"])
        for key in ("baseline_asof_ms","actual_duration_ms","baseline_error_ms","mode"):
            self.assertIsNone(field[key])

    def test_signed_funding_and_native_zero_is_valid(self):
        report=funding_fixture.fixture(4)
        report=replace(report,metadata=replace(report.metadata,mapping_version=self.mapping))
        meta=report.metadata
        estimate=FundingEstimate(meta.pair_id,self.mapping,4,meta.next_funding_ms,meta.settled_rate,Decimal(0),NOW)
        native=native_estimate(estimate,meta,now_ms=NOW)
        add_funding(self.row,report,native,input_ref="raw",now_ms=NOW)
        value=self.seal()["rows"][0]
        self.assertEqual(value["funding"]["native_estimate"]["value"],"0")
        self.assertEqual(Decimal(value["funding_change"]["settled_last"]["value"]),Decimal(0))
        self.assertEqual(Decimal(value["funding_change"]["basis_bps"]["value"]),Decimal(0))

    def test_input_expiry_not_extended(self):
        self.row["price"]=available("price","price_change.v1","100",source_ms=NOW,
            expires_ms=NOW+60000,input_refs=["raw"],now_ms=NOW)
        self.inputs["raw"]["expires_ms"]=NOW+1000
        with self.assertRaisesRegex(ValueError,"field_freshens_input"):self.seal()
