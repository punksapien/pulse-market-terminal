from copy import deepcopy
import gzip
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from jsonschema import Draft202012Validator

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"tests"))
from test_wire_publication import brotli
from pulse.wire.composer import compose
from pulse.wire.live_price import compose_live_price
from pulse.wire.publication import byte_digest
from pulse.http.wire_reader import response
from canonical import seal_fixture
from snapshot_spec import snapshot_fixture
from semantics import validate_snapshot
from live_spec import validate_live
from validate import registry


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.s=seal_fixture(snapshot_fixture());self.now=self.s["generated_ms"]
        self.registry,_=registry()

    def snapshot_gate(self,s):
        Draft202012Validator({"$ref":"urn:pulse:schema:v1:snapshot"},registry=self.registry).validate(s)
        validate_snapshot(s)

    def price_gate(self,s):
        Draft202012Validator({"$ref":"urn:pulse:schema:v1:live_price"},registry=self.registry).validate(s)
        validate_live(s,self.s["universe"],self.s["mapping_version"])

    def price(self,row=None):
        return compose_live_price(self.s["universe"],{"ts":self.now,"prices":{} if row is None else {"B-BTC_USDT":row}},
            mapping_version=self.s["mapping_version"],batch_version="sha256:"+"a"*64,sequence=1,
            now_ms=self.now,price_max_age_ms=60000,validate=self.price_gate)

    def test_live_price_source_clock_text_and_unavailable_reasons(self):
        source={"mkt":"BTCUSDT","ls":"103.1250","btST":self.now-500,"bmST":self.now}
        value=self.price(source)
        self.assertEqual(value["rows"][0]["price"],"103.1250")
        self.assertEqual(value["rows"][0]["source_ms"],source["btST"])
        self.assertEqual(value["rows"][0]["expires_ms"],source["btST"]+60000)
        self.assertEqual(value["expires_ms"],self.now+15000)
        for patch,reason in (({"ls":103.125},"source_precision_unsupported"),({"mkt":"ETHUSDT"},"ticker_identity_mismatch"),
            ({"btST":self.now-60000},"stale_price"),({"btST":self.now+5001},"future_price"),
            ({"ls":"100.00000000000000001"},"binary64_roundtrip_loss")):
            row=self.price({**source,**patch})["rows"][0]
            self.assertEqual(row["reason"],reason);self.assertIsNone(row["price"])
        self.assertEqual(self.price()["rows"][0]["reason"],"source_unavailable")
        self.assertEqual(self.price({**source,"ls":80460})["rows"][0]["price"],"80460")
        self.assertEqual(self.price({**source,"ls":True})["rows"][0]["reason"],"source_precision_unsupported")

    def test_recorded_public_ticker_replay(self):
        evidence=json.loads((ROOT/"docs/evidence/pulse_funding_runtime_sources_cycle5_2026-10-26.json").read_text())
        checked=0
        for pair in evidence["pairs"]:
            if pair["symbol"]!="BTCUSDT": continue
            source=pair["raw_ticker"];now=pair["ticker_source_ms"]
            result=compose_live_price(self.s["universe"],{"ts":now,"prices":{"B-BTC_USDT":source}},
                mapping_version=self.s["mapping_version"],batch_version="sha256:"+"a"*64,sequence=1,
                now_ms=now,price_max_age_ms=60000,validate=self.price_gate)["rows"][0]
            self.assertEqual(result["status"],"available")
            self.assertEqual(result["price"],str(source["ls"]))
            self.assertEqual(result["source_ms"],source["btST"])
            checked+=1
        self.assertGreater(checked,0)

    def test_snapshot_bundle_served_by_real_reader_with_gzip(self):
        package=compose(self.s,validate=self.snapshot_gate,brotli_compress=brotli,input_revisions={"source":1},now_ms=self.now)
        self.assertEqual(package.channel,"snapshot:1h:manifest")
        descriptors={key:json.loads(body) for key,body in package.descriptors};blobs=dict(package.blobs)
        record={"status":"available","reason":None,"payload":package.pointer,"digest":byte_digest(package.pointer)[7:],
                "source_ms":package.generated_ms,"expires_ms":package.expires_ms}
        store=Mock();store.read.return_value=record
        artifacts=Mock(mapping_version=self.s["mapping_version"])
        artifacts.descriptor.side_effect=descriptors.__getitem__;artifacts.blob.side_effect=blobs.get
        args=dict(environment="preview",store_factory=lambda _:store,artifacts_factory=lambda:artifacts,now_ms=self.now)
        m=response({"horizon":["1H"]},**args)
        self.assertEqual(m.status,200)
        for descriptor in json.loads(m.body)["parts"].values():
            result=response({"artifact":[descriptor["logical_sha256"]]},accept_encoding="gzip,identity;q=0",**args)
            self.assertEqual(result.status,200)
            doc=json.loads(gzip.decompress(result.body));self.assertEqual(doc["generation"],self.s["generation"])
        self.assertEqual(package.allocation_bytes,len(package.pointer)+sum(len(b) for _,b in package.descriptors+package.blobs))

    def test_live_document_composition_and_validation_before_compression(self):
        value=self.price({"mkt":"BTCUSDT","ls":"100","btST":self.now})
        vector={"ticker":1}
        def gate(v): self.price_gate(v);vector["ticker"]=2
        package=compose(value,validate=gate,brotli_compress=brotli,input_revisions=vector,now_ms=self.now)
        self.assertEqual(package.input_revisions,(("ticker",1),))
        self.assertEqual(package.channel,"live_price_v1")
        self.assertEqual(json.loads(package.pointer)["schema"],"pulse.artifact.v1")
        compress=Mock()
        bad=deepcopy(self.s);bad["rows"][0]["price"]["value"]="0"
        with self.assertRaises(ValueError): compose(bad,validate=self.snapshot_gate,brotli_compress=compress,input_revisions={"x":1},now_ms=self.now)
        compress.assert_not_called()
        with self.assertRaises(ValueError): compose(self.s,validate=self.snapshot_gate,brotli_compress=compress,input_revisions={},now_ms=self.now)
        with self.assertRaises(ValueError): compose(self.s,validate=self.snapshot_gate,brotli_compress=compress,input_revisions={"x":1},now_ms=self.now+60000)


if __name__=="__main__": unittest.main()
