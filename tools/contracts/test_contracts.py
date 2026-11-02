import copy
import gzip
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, ValidationError
from canonical import generation, seal_fixture
from snapshot_spec import SNAPSHOT, snapshot_fixture, make_unavailable
from semantics import validate_snapshot
from validate import registry


class SnapshotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.local,_=registry()
        cls.validator=Draft202012Validator({"$ref":SNAPSHOT},registry=cls.local)

    def validate(self,value):
        self.validator.validate(value)
        validate_snapshot(value)

    def test_fictional_fixture_states_and_horizons(self):
        directory=Path(__file__).resolve().parents[2]/"docs/contracts/fixtures"
        for path in directory.glob("snapshot.*.json"):
            with self.subTest(path=path.name): self.validate(json.loads(path.read_text()))

    def test_classified_native_stable_fixture(self):
        from classified_fixture import classified_fixture
        s=classified_fixture(); self.validate(s)
        row=s["rows"][0]
        self.assertEqual(row["volatility"]["direction"]["value"],"Stable")
        self.assertEqual([row["volatility"]["windows"][h]["level"] for h in ("1H","4H","24H")],[3,2,1])

    def test_unknown_schema_properties_and_units_fail(self):
        fixture=seal_fixture(snapshot_fixture())
        for kind in ("schema","extra","unit","float","missing","unavailable_zero"):
            bad=copy.deepcopy(fixture); row=bad["rows"][0]
            if kind=="schema": bad["schema"]="pulse.snapshot.v2"
            if kind=="extra": row["sneaky_legacy_join"]={}
            if kind=="unit": row["funding"]["native_estimate"]["unit"]="bps"
            if kind=="float": row["price"]["value"]=102.0
            if kind=="missing": del row["oi"]
            if kind=="unavailable_zero": row["oi"]["quantity"].update(status="unavailable",reason="source_unavailable")
            with self.subTest(kind=kind),self.assertRaises(ValidationError): self.validator.validate(bad)

    def test_semantic_identity_time_and_operand_faults(self):
        fixture=snapshot_fixture()
        for kind in ("id","input","expiry","basis","nft","estimate","since","minute","fallback","score","integer","scale"):
            bad=copy.deepcopy(fixture); row=bad["rows"][0]
            if kind=="id": row["id"]=1
            if kind=="input": row["price"]["input_refs"]=["missing:input"]
            if kind=="expiry": row["price"]["expires_ms"]=bad["generated_ms"]-1
            if kind=="basis": row["funding_change"]["basis_bps"]["value"]="1"
            if kind=="nft":
                row["funding_change"]["nft"]+=5001; row["funding"]["nft"]+=5001
            if kind=="estimate": row["funding"]["native_estimate"]["expires_ms"]=row["funding"]["native_estimate"]["asof_ms"]+120001
            if kind=="since": row["signal_since"]["state"]="ReversalLong"
            if kind=="minute": row["references"]["ladder"]["slots"][0][0]["reference_minute_ms"]+=60000
            if kind=="fallback": row["references"]["fallback"]["references"]["1H"]["reference_minute_ms"]+=60000
            if kind=="score": row["volatility"]["score"]["value"]="0.999"
            if kind=="integer": bad["generated_ms"]=float(bad["generated_ms"])
            if kind=="scale": row["context"]["volatility_momentum"]["value"]="0."+"0"*60+"1"
            bad=seal_fixture(bad)
            self.validator.validate(bad)
            with self.subTest(kind=kind),self.assertRaises(ValueError): validate_snapshot(bad)

    def test_zero_delta_not_unavailable_and_classification_not_invented(self):
        snapshot=snapshot_fixture(); row=snapshot["rows"][0]
        row["funding_change"]["settled_last"].update(value="0",level_basis="0")
        row["funding_change"]["basis_bps"]["value"]="0"
        self.validate(seal_fixture(snapshot))
        row["oi"]["change_1h"].update(classification_status="classified",level=4,classification_version="made_up")
        with self.assertRaises(ValidationError): self.validator.validate(seal_fixture(snapshot))

    def test_due_pending_does_not_keep_settled_numbers(self):
        snapshot=make_unavailable(snapshot_fixture())
        row=snapshot["rows"][0]
        row["funding_change"].update(status="settlement_pending",reason="settlement_pending")
        self.validate(seal_fixture(snapshot))
        row["funding_change"]["settled_last"]=snapshot_fixture()["rows"][0]["funding_change"]["settled_last"]
        with self.assertRaises(ValueError): self.validate(seal_fixture(snapshot))

    def test_missing_reference_never_uses_previous_minute(self):
        snapshot=snapshot_fixture(); point=snapshot["rows"][0]["references"]["fallback"]["references"]["1H"]
        point.update(status="unavailable",reason="reference_missing",open_price=None)
        with self.assertRaises(ValueError): self.validate(seal_fixture(snapshot))

    def test_digest_stable_across_encoding_and_changes_on_content(self):
        snapshot=seal_fixture(snapshot_fixture())
        raw=json.dumps(snapshot).encode()
        self.assertEqual(generation(json.loads(gzip.decompress(gzip.compress(raw)))),snapshot["generation"])
        snapshot["rows"][0]["outlook"]["description"]["value"]="Different text"
        self.assertNotEqual(generation(snapshot),snapshot["generation"])
        with self.assertRaises(ValueError): validate_snapshot(snapshot)


if __name__=="__main__": unittest.main()
