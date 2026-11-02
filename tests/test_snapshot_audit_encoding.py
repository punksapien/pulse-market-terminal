import gzip
import importlib.util
from pathlib import Path
import unittest

import brotli

spec=importlib.util.spec_from_file_location('snapshot_audit',Path('tools/probes/pulse_snapshot_audit.py'))
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


class AuditEncodingTests(unittest.TestCase):
    def test_browser_encoding_variants_reconstruct_same_logical_bytes(self):
        raw=b'{"value":"1.234567890123456789"}'
        for encoding,data in (('identity',raw),('gzip',gzip.compress(raw)),('br',brotli.compress(raw))):
            self.assertEqual(audit.decode_response(data,encoding),raw)

    def test_invalid_truncated_and_expanded_bodies_refuse(self):
        for encoding in ('deflate','unexpected'):
            with self.assertRaises(ValueError):audit.decode_response(b'x',encoding)
        with self.assertRaises(ValueError):audit.decode_response(b'x'*101,'identity',maximum=100)
        for encoding,data in (('gzip',gzip.compress(b'x'*10000)),('br',brotli.compress(b'x'*10000))):
            with self.assertRaises(ValueError):audit.decode_response(data,encoding,maximum=100)
        with self.assertRaises(ValueError):audit.decode_response(brotli.compress(b'abc')[:-1],'br')
