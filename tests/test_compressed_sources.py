import gzip
from unittest import TestCase
import brotli

from pulse.wire.source_codec import encode,decode,gunzip,MAGIC,BR_MAGIC,MAX_BYTES
from pulse.wire.publication import prepare,byte_digest
from pulse.store.published_artifacts import descriptor_valid
from pulse.http.encoded_artifact import artifact_response


class CompressedSourceTests(TestCase):
    def test_source_codec_is_lossless_deterministic_and_accepts_old_raw(self):
        for body in (b"{}",b'{"fraction":"0.000000000000000001"}',b"x"*100000):
            encoded=encode(body)
            self.assertEqual(decode(encoded),body)
            self.assertEqual(encode(body),encoded)
            self.assertEqual(decode(body),body)

    def test_corrupt_trailing_and_decompression_bomb_are_refused(self):
        for body in (MAGIC+b"bad",encode(b"x")+b"trailing",MAGIC+gzip.compress(b"x"*(MAX_BYTES+1)),
                encode(b"x")+gzip.compress(b"y"),b"",b"x"*(MAX_BYTES+1)):
            with self.subTest(size=len(body)),self.assertRaises(ValueError):decode(body)
        for maximum in (0,-1,True,MAX_BYTES+1):
            with self.assertRaises(ValueError):gunzip(gzip.compress(b"x"),maximum=maximum)

    def test_brotli_and_legacy_gzip_are_bounded_and_lossless(self):
        for body in (b'{}',b'x'*MAX_BYTES,bytes(range(256))*100):
            self.assertEqual(decode(MAGIC+gzip.compress(body,mtime=0)),body)
            self.assertEqual(decode(BR_MAGIC+brotli.compress(body,quality=5)),body)
            encoded=encode(body)
            self.assertLessEqual(len(encoded),len(MAGIC+gzip.compress(body,compresslevel=6,mtime=0)))
        for body in (BR_MAGIC+b'bad',BR_MAGIC+brotli.compress(b'x'*(MAX_BYTES+1),quality=5),
                BR_MAGIC+brotli.compress(b'x',quality=5)+b'trailing',
                (BR_MAGIC+brotli.compress(b'hello',quality=5))[:-1]):
            with self.assertRaises(ValueError):decode(body)

    def test_compressed_only_descriptor_reader_and_logical_hash(self):
        doc={"mapping_version":"sha256:"+"a"*64,"generation":None,"value":"0.000000000000000001"}
        descriptor,blobs=prepare(doc,brotli_compress=lambda b,q:brotli.compress(b,quality=q),include_identity=False)
        descriptor_valid(descriptor)
        self.assertEqual(set(descriptor["variants"]),{"br11","gzip"})
        for header,encoding,decoder in (("br","br",brotli.decompress),("gzip","gzip",gzip.decompress)):
            response=artifact_response(descriptor,blobs.get,accept_encoding=header,cache_control="public")
            self.assertEqual(response.status,200)
            self.assertEqual(response.headers["Content-Encoding"],encoding)
            self.assertEqual(byte_digest(decoder(response.body)),descriptor["logical_sha256"])
        for header in (None,"identity","br;q=0,gzip;q=0"):
            self.assertEqual(artifact_response(descriptor,blobs.get,accept_encoding=header,cache_control="public").status,406)
