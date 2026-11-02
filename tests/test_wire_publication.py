import gzip
import json
import subprocess
import unittest
from unittest.mock import Mock

from pulse.wire.publication import prepare,manifest,byte_digest
from pulse.wire.compact import raw,PARTS
from pulse.http.encoded_artifact import artifact_response,negotiate


def brotli(body,quality):
    script="const z=require('node:zlib'),fs=require('node:fs');process.stdout.write(z.brotliCompressSync(fs.readFileSync(0),{params:{[z.constants.BROTLI_PARAM_QUALITY]:Number(process.argv[1])}}))"
    return subprocess.check_output(["node","-e",script,str(quality)],input=body)


class PublicationTests(unittest.TestCase):
    def test_variants_roundtrip_and_representation_etags(self):
        doc={"mapping_version":"m1","generation":"g1","decimal":"0.000000000000000001"}
        descriptor,blobs=prepare(doc,brotli_compress=brotli,include_fast=True)
        expected=raw(doc)
        for name,variant in descriptor["variants"].items():
            body=blobs[variant["sha256"]]
            self.assertEqual(byte_digest(body),variant["sha256"])
            if name=="gzip": decoded=gzip.decompress(body)
            elif name.startswith("br"):
                decoded=subprocess.check_output(["node","-e","process.stdout.write(require('node:zlib').brotliDecompressSync(require('node:fs').readFileSync(0)))"],input=body)
            else: decoded=body
            self.assertEqual(decoded,expected)
        for header,coding in ((None,"identity"),("br","br"),("gzip,identity;q=0","gzip")):
            response=artifact_response(descriptor,blobs.get,accept_encoding=header,cache_control="public, max-age=10")
            self.assertEqual(response.status,200)
            self.assertEqual(response.headers.get("Content-Encoding","identity"),coding)
            self.assertEqual(response.headers["Vary"],"Accept-Encoding")
            not_modified=artifact_response(descriptor,blobs.get,accept_encoding=header,if_none_match=response.headers["ETag"],cache_control="public, max-age=10")
            self.assertEqual(not_modified.status,304); self.assertEqual(not_modified.body,b"")

    def test_negotiation_quality_wildcards_and_zero(self):
        variants={k:{} for k in ("identity","gzip","br11","br5")}
        for header,expected in ((None,"identity"),("br, gzip","br11"),("br;q=0,gzip;q=1","gzip"),
            ("*;q=0",None),("*;q=0,gzip;q=0.5","gzip"),("br;q=2,identity;q=0",None),
            ("br;q=0.8,gzip;q=0.9,identity;q=0","gzip"),("garbage","identity"),("br; q=1;wat=1,identity;q=0",None)):
            with self.subTest(header=header): self.assertEqual(negotiate(header,variants),expected)
        self.assertIsNone(negotiate("x"*1025,variants))

    def test_missing_corrupt_and_incomplete_generation_refuse(self):
        doc={"mapping_version":"m","generation":"g","x":1}
        d,blobs=prepare(doc,brotli_compress=lambda b,q:b"fake-br-for-isolation-test")
        for read in (lambda key:None,lambda key:b"corrupt"):
            with self.assertRaises(ValueError): artifact_response(d,read,cache_control="no-store")
        fetch=Mock()
        self.assertEqual(artifact_response(d,fetch,accept_encoding="*;q=0",cache_control="no-store").status,406)
        fetch.assert_not_called()
        with self.assertRaises(ValueError): manifest(doc,{"core":d})
        descriptors={name:d for name in PARTS}; bad={**d,"mapping_version":"other"}; descriptors["oi"]=bad
        with self.assertRaises(ValueError): manifest(doc,descriptors)
        with self.assertRaises(ValueError): prepare({**doc,"x":"x"*1_048_576},brotli_compress=lambda b,q:b)


if __name__=="__main__": unittest.main()
