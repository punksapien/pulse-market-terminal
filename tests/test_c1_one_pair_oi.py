import json
import struct
import unittest
from unittest.mock import Mock,patch

from pulse.collectors.one_pair_oi import parse_current_oi,fetch_current_oi
from pulse.store.one_pair_oi import empty_capsule,RING_BYTES,CAPSULE_BYTES,DERIVED,OnePairStore
from pulse.store.bucket_ring import BucketRing


class OnePairTests(unittest.TestCase):
    def test_source_identity_precision_and_timestamp_guards(self):
        base={"symbol":"BTCUSDT","time":1792990000000,"openInterest":"12345.678"}
        point=parse_current_oi(base)
        self.assertEqual(point.source_quantity,"12345.678")
        self.assertEqual(parse_current_oi(dict(base,openInterest="0")).quantity,0.)
        for update in ({"symbol":"ETHUSDT"},{"time":True},{"time":-1},{"openInterest":"NaN"},
                       {"openInterest":"-1"},{"openInterest":"1.0000000000000001"},{"openInterest":"1e999"},{"openInterest":"1e-320"}):
            with self.assertRaises(ValueError):
                parse_current_oi(dict(base,**update))

    def test_reserve_before_dispatch_and_no_automatic_retry(self):
        with patch("pulse.collectors.one_pair_oi.build_opener") as opener:
            with self.assertRaisesRegex(RuntimeError,"budget_denied"):
                fetch_current_oi(reserve=lambda:False)
            opener.assert_not_called()
            opener.return_value.open.side_effect=TimeoutError("fixture")
            with self.assertRaises(TimeoutError):
                fetch_current_oi(reserve=lambda:True)
            self.assertEqual(opener.return_value.open.call_count,1)

    def test_preallocation_fixed_size_preserves_approved_ring_bytes(self):
        data=empty_capsule()
        self.assertEqual(len(data),CAPSULE_BYTES)
        self.assertEqual(data[:RING_BYTES],BucketRing(60000,1536).to_bytes())
        self.assertEqual((RING_BYTES,CAPSULE_BYTES,DERIVED.size),(24832,25152,96))

    def test_read_path_never_calls_collector(self):
        client=Mock()
        client.evalsha.return_value=[b"no_observation"]
        with patch("pulse.collectors.one_pair_oi.fetch_current_oi",side_effect=AssertionError("read source call")):
            store=OnePairStore(client,"a"*32)
            self.assertEqual(store.read(),{"status":"unavailable","reason":"no_observation"})
        with self.assertRaises(ValueError):
            OnePairStore(client,"production")

    def test_own_namespace_and_preallocation_memory_guards(self):
        client=Mock()
        store=OnePairStore(client,"a"*32)
        with self.assertRaises(ValueError):
            store.read(frame="vol:v2:unrelated")
        client.evalsha.assert_not_called()
        client.info.return_value={"used_memory":21000000}
        with self.assertRaisesRegex(RuntimeError,"memory_gate"):
            store.initialise()
        client.eval.assert_not_called()
