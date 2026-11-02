from copy import deepcopy
import gzip
import hashlib
import json
import unittest
import io
import os
from unittest.mock import Mock,patch

from pulse.wire.compact import raw,PARTS
from pulse.wire.publication import prepare,manifest
from pulse.http.wire_reader import response
from pulse.store.published_artifacts import PublishedArtifacts,descriptor_valid

MAPPING="sha256:"+"a"*64
GEN="sha256:"+"b"*64
NOW=1800000000000


class WireReaderTests(unittest.TestCase):
    def setUp(self):
        self.base={"mapping_version":MAPPING,"generation":GEN,"horizon":"1H","generated_ms":NOW,"expires_ms":NOW+60000}
        # Stub compression is not exercised: these tests request gzip or identity.
        self.descriptor,self.blobs=prepare(self.base,brotli_compress=lambda b,q:b"stub-br")
        self.manifest=manifest(self.base,{part:deepcopy(self.descriptor) for part in PARTS})
        self.artifacts=Mock(mapping_version=MAPPING)
        self.artifacts.descriptor.return_value=self.descriptor
        self.artifacts.blob.side_effect=self.blobs.get
        self.reader=Mock()
        self.store=Mock(return_value=self.reader)
        self.factory=Mock(return_value=self.artifacts)

    def record(self,value,expiry=NOW+60000):
        body=raw(value)
        self.reader.read.return_value={"status":"available","reason":None,"payload":body,
            "digest":hashlib.sha256(body).hexdigest(),"source_ms":NOW,"expires_ms":expiry}

    def request(self,query,**kwargs):
        return response(query,environment=kwargs.pop("environment","preview"),store_factory=self.store,
            artifacts_factory=self.factory,now_ms=kwargs.pop("now_ms",NOW),**kwargs)

    def test_preview_gate_and_bad_queries_do_not_access_store(self):
        self.assertEqual(self.request({"live":["1"]},environment="production").status,404)
        for query in ({},{"live":["bad"]},{"horizon":["1H","4H"]},{"artifact":["../anything"]},
            {"artifact":["sha256:"+"0"*64],"utm_source":["x"]},{"horizon":["1H"],"part":["core"]}):
            self.assertEqual(self.request(query).status,400)
        self.store.assert_not_called(); self.factory.assert_not_called()

    def test_manifest_artifact_and_304_paths(self):
        self.record(self.manifest)
        first=self.request({"horizon":["1H"]})
        self.assertEqual(first.status,200)
        self.store.assert_called_once_with("snapshot:1h:manifest")
        self.assertEqual(json.loads(first.body)["generation"],GEN)
        self.assertEqual(self.request({"horizon":["1H"]},if_none_match=first.headers["ETag"]).status,304)
        self.assertEqual(self.request({"horizon":["1H"]},accept_encoding="br,identity;q=0").status,406)
        result=self.request({"artifact":[self.descriptor["logical_sha256"]]},accept_encoding="gzip,identity;q=0")
        self.assertEqual(result.status,200)
        self.assertEqual(gzip.decompress(result.body),raw(self.base))
        self.assertEqual(result.headers["Vary"],"Accept-Encoding")
        self.assertIn("immutable",result.headers["Cache-Control"])

    def test_explicit_production_activation_keeps_the_read_only_contract(self):
        self.record(self.manifest)
        result=self.request({"horizon":["1H"]},environment="production",production_active=True)
        self.assertEqual(result.status,200)
        self.store.assert_called_once_with("snapshot:1h:manifest")
        self.reader.acquire.assert_not_called()
        self.reader.publish.assert_not_called()

    def test_live_pointer_channels_expiry_and_missing_fail_closed(self):
        for query,name in (({"live":["1"]},"live_funding_v1"),({"live":["price"]},"live_price_v1")):
            self.record(self.descriptor,expiry=NOW+15000)
            result=self.request(query,accept_encoding="gzip,identity;q=0")
            self.assertEqual(result.status,200)
            self.store.assert_called_with(name)
            # the live price pointer is cached for 1 s, funding for 3 s (the price must feel live)
            self.assertIn("s-maxage=1" if name=="live_price_v1" else "s-maxage=3",result.headers["Cache-Control"])
            self.assertEqual(self.request(query,now_ms=NOW+15000).status,503)
        self.reader.read.return_value={"status":"unavailable","reason":"document_missing"}
        self.assertEqual(self.request({"horizon":["1H"]}).status,503)

    def test_history_is_lazy_prebuilt_and_cacheable(self):
        self.record(self.descriptor)
        result=self.request({"history":["13"]},accept_encoding="gzip,identity;q=0")
        self.assertEqual(result.status,200)
        self.store.assert_called_with("history:1")
        self.assertEqual(gzip.decompress(result.body),raw(self.base))
        self.assertIn("s-maxage=10",result.headers["Cache-Control"])
        self.assertEqual(self.request({"history":["13"]},now_ms=NOW+60000).status,503)

    def test_corruption_wrong_mapping_and_mixed_generations_refuse(self):
        for kind in ("mapping","generation","time","hash"):
            value=deepcopy(self.manifest)
            if kind=="mapping": value["mapping_version"]="sha256:"+"e"*64
            if kind=="generation": value["parts"]["oi"]["generation"]="sha256:"+"e"*64
            if kind=="time": value["generated_ms"]-=1000
            self.record(value)
            if kind=="hash": self.reader.read.return_value["digest"]="0"*64
            self.assertEqual(self.request({"horizon":["1H"]}).status,503)
        self.artifacts.blob.return_value=None
        self.artifacts.blob.side_effect=lambda identifier:None
        self.assertEqual(self.request({"artifact":[self.descriptor["logical_sha256"]]}).status,503)

    def test_exact_bounded_staging_reads_and_descriptor_validation(self):
        client=Mock()
        store=PublishedArtifacts(client,"c"*32,MAPPING)
        client.getrange.return_value=raw(self.descriptor)
        self.assertEqual(store.descriptor(self.descriptor["logical_sha256"]),self.descriptor)
        key,start,end=client.getrange.call_args.args
        self.assertTrue(key.startswith("pulse:stg:{"+"c"*32+"}:publication:artifact:"))
        self.assertEqual((start,end),(0,16384))
        client.getrange.return_value=b"x"*1114113
        with self.assertRaises(ValueError): store.blob("sha256:"+"d"*64)
        with self.assertRaises(ValueError): store.key("prod",MAPPING)
        bad=deepcopy(self.descriptor); bad["variants"]["identity"]["bytes"]+=1
        with self.assertRaises(ValueError): descriptor_valid(bad)
        self.assertEqual(set(client.method_calls[i][0] for i in range(len(client.method_calls))),{"getrange"})

    def test_actual_handler_dispatch_is_opt_in_and_preview_only(self):
        from api import pulse
        self.record(self.manifest)
        for flag,environment,channel,status in (("1","preview","snapshot:1h:manifest",200),
            ("0","preview","snapshot:1h",200),("1","production",None,404)):
            handler=Mock(path="/api/pulse?horizon=1H",headers={},wfile=io.BytesIO())
            self.store.reset_mock(); self.factory.reset_mock()
            with patch.dict(os.environ,{"PULSE_WIRE_V1_ENABLED":flag,"VERCEL_ENV":environment}), \
                 patch.object(pulse,"store_factory",self.store),patch.object(pulse,"artifacts_factory",self.factory), \
                 patch.object(pulse.time,"time_ns",return_value=NOW*1000000):
                pulse.handler.do_GET(handler)
            handler.send_response.assert_called_once_with(status)
            if channel is None:
                self.store.assert_not_called(); self.factory.assert_not_called()
            else: self.store.assert_called_once_with(channel)


if __name__=="__main__": unittest.main()
