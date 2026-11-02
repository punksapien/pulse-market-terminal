import gzip
import math
import itertools
import random
import struct
import unittest
import zlib

from pulse.store.bucket_ring import BucketCollision, BucketRing, DiscardedBaseline, IntegrityError, MAX_FUTURE_SKEW_MS, WriteStatus
from pulse.store.memory_store import MemoryStore


class RingTests(unittest.TestCase):
    def test_exact_and_inclusive_baselines(self):
        ring = BucketRing(60000, 8)
        ring.write(60000, (1.0,), now_ms=60000)
        for error in (0, 59000, 60000):
            self.assertEqual(ring.baseline(60000 + error, 60000), (60000, 1.0))
        self.assertIsNone(ring.baseline(120001, 60000))
        self.assertIsNone(ring.baseline(59999, 60000))

    def test_cold_start_tolerance(self):
        ring = BucketRing(300000, 8)
        ring.write(300000, (100.0,), now_ms=300000)
        self.assertEqual(ring.baseline(600000, 300000), (300000, 100.0))
        self.assertIsNone(ring.baseline(600001, 300000))
        with self.assertRaises(ValueError):
            ring.baseline(600000, 300001)

    def test_gap_not_filled(self):
        ring = BucketRing(60000, 8)
        ring.write(0, (1.0,), now_ms=0)
        ring.write(180000, (2.0,), now_ms=180000)
        self.assertIsNone(ring.baseline(120000, 60000))

    def test_wrap_retains_fixed_size(self):
        ring = BucketRing(60000, 4)
        size = ring.nbytes
        for bucket in range(100):
            ring.write(bucket * 60000, (float(bucket),), now_ms=bucket * 60000)
            self.assertEqual(ring.nbytes, size)
        self.assertIsNone(ring.baseline(95 * 60000, 0))
        self.assertEqual(ring.baseline(96 * 60000, 0), (96 * 60000, 96.0))
        with self.assertRaises(ValueError):
            ring.write(95 * 60000, (1000.0,), now_ms=99 * 60000)

    def test_out_of_order_inside_retention(self):
        ring = BucketRing(60000, 4)
        ring.write(180000, (3.0,), now_ms=180000)
        ring.write(120000, (2.0,), now_ms=180000)
        self.assertEqual(ring.baseline(120000, 0), (120000, 2.0))

    def test_duplicates_and_exact_conflicts(self):
        encodings = []
        for values in ((1.0, 2.0), (2.0, 1.0)):
            ring = BucketRing(60000, 4)
            self.assertEqual(ring.write(10000, (values[0],), now_ms=60000), WriteStatus.WRITTEN)
            self.assertEqual(ring.write(10000, (values[0],), now_ms=60000), WriteStatus.DUPLICATE)
            with self.assertRaises(BucketCollision):
                ring.write(10000, (values[1],), now_ms=60000)
            with self.assertRaises(BucketCollision):
                ring.baseline(55000, 60000)
            encodings.append(ring.to_bytes())
            ring.write(50000, (3.0,), now_ms=60000)
            self.assertEqual(ring.baseline(55000, 60000), (50000, 3.0))
            ring.write(240000, (4.0,), now_ms=240000)
            self.assertEqual(ring.baseline(240000, 0), (240000, 4.0))
        self.assertEqual(*encodings)

    def test_distinct_timestamps_converge_under_replay(self):
        outputs = []
        samples = ((10000, 1.0), (30000, 2.0), (50000, 3.0))
        for ordering in itertools.permutations(samples):
            ring = BucketRing(60000, 4)
            for timestamp, value in ordering + ordering:
                ring.write(timestamp, (value,), now_ms=60000)
            self.assertEqual(ring.collision_buckets, 1)
            self.assertEqual(ring.baseline(55000, 60000), (50000, 3.0))
            before = ring.to_bytes()
            self.assertEqual(ring.write(50000, (3.0,), now_ms=60000), WriteStatus.DUPLICATE)
            self.assertEqual(before, ring.to_bytes())
            outputs.append(before)
        self.assertTrue(all(raw == outputs[0] for raw in outputs))

    def test_discarded_sample_does_not_return_wrong_prior_bucket(self):
        ring = BucketRing(60000, 4)
        for timestamp, value in ((50000, 100.0), (70000, 200.0), (110000, 300.0)):
            ring.write(timestamp, (value,), now_ms=120000)
        with self.assertRaises(DiscardedBaseline):
            ring.baseline(90000, 60000)
        self.assertEqual(ring.baseline(115000, 60000), (110000, 300.0))

    def test_unavailable_is_not_corruption_and_write_results_are_distinct(self):
        self.assertFalse(issubclass(DiscardedBaseline, IntegrityError))
        ring = BucketRing(60000, 4)
        self.assertEqual(ring.write(50000, (3.0,), now_ms=60000), WriteStatus.WRITTEN)
        self.assertEqual(ring.write(50000, (3.0,), now_ms=60000), WriteStatus.DUPLICATE)
        self.assertEqual(ring.write(10000, (1.0,), now_ms=60000), WriteStatus.DROPPED_OLDER)
        self.assertEqual(ring.write(10000, (1.0,), now_ms=60000), WriteStatus.DROPPED_OLDER)
        self.assertEqual(ring.collision_buckets, 1)
        with self.assertRaises(BucketCollision) as conflict:
            ring.write(50000, (4.0,), now_ms=60000)
        self.assertEqual(conflict.exception.status, WriteStatus.CONFLICT)

    def test_future_rejection_is_immutable_and_ring_recovers(self):
        ring = BucketRing(60000, 8)
        now = 1792980000000
        ring.write(now, (1.0,), now_ms=now)
        before = ring.to_bytes()
        for future in (now + 10**12, now + MAX_FUTURE_SKEW_MS + 1):
            with self.assertRaisesRegex(ValueError, "future_timestamp"):
                ring.write(future, (2.0,), now_ms=now)
            self.assertEqual(ring.to_bytes(), before)
        ring.write(now + 60000, (3.0,), now_ms=now + 60000)
        self.assertEqual(ring.baseline(now + 60000, 0), (now + 60000, 3.0))
        edge = BucketRing(60000, 8)
        edge.write(now + MAX_FUTURE_SKEW_MS, (1.0,), now_ms=now)
        with self.assertRaises(TypeError):
            edge.write(now, (1.0,))

    def test_own_and_cold_start_use_different_rings(self):
        for step, capacity, error in ((60000, 1536, 60000), (300000, 320, 300000)):
            ring = BucketRing(step, capacity)
            ring.write(step, (100.0,), now_ms=step)
            self.assertEqual(ring.baseline(step + error, error), (step, 100.0))
            self.assertIsNone(ring.baseline(step + error + 1, error))
            self.assertEqual(ring.nbytes, BucketRing.nbytes_for(step, capacity))

    def test_binary64_bits_and_transport_round_trip(self):
        ring = BucketRing(60000, 8, 3)
        values = (-0.0, math.nextafter(1.0, 2.0), 1e-300)
        ring.write(60000, values, now_ms=60000)
        raw = ring.to_bytes()
        for encode, decode in ((zlib.compress, zlib.decompress), (gzip.compress, gzip.decompress)):
            back = BucketRing.from_bytes(decode(encode(raw)))
            self.assertEqual(back.to_bytes(), raw)
            self.assertEqual(struct.pack("<ddd", *back.baseline(60000, 0)[1:]), struct.pack("<ddd", *values))

    def test_structural_corruption_header_record_and_mask(self):
        for area in ("header", "record", "valid"):
            ring = BucketRing(60000, 8)
            ring.write(60000, (100.0,), now_ms=60000)
            offset = {"header": 20, "record": ring.offset + ring.stride + 2,
                      "valid": 64}[area]
            ring._data[offset] ^= 2 if area == "valid" else 1
            with self.assertRaises(IntegrityError):
                ring.scrub()

    def test_checksum_consistent_bad_latest_rejected(self):
        ring = BucketRing(60000, 8)
        ring.write(60000, (1.0,), now_ms=60000)
        ring._header(10000, 0)
        with self.assertRaisesRegex(IntegrityError, "latest_bucket_mismatch"):
            ring.scrub()

    def test_no_claim_to_detect_all_finite_value_corruption(self):
        ring = BucketRing(60000, 8)
        ring.write(60000, (100.0,), now_ms=60000)
        ring._data[ring.offset + ring.stride + 8] ^= 1
        self.assertTrue(ring.scrub())
        self.assertNotEqual(ring.baseline(60000, 0)[1], 100.0)

    def test_invalid_inputs_cannot_mutate_ring(self):
        ring = BucketRing(60000, 8)
        before = ring.to_bytes()
        for timestamp, values in ((-1, (1.0,)), (True, (1.0,)), (0, (math.nan,)), (0, (math.inf,)), (0, (1,)), (0, ())):
            with self.assertRaises(ValueError):
                ring.write(timestamp, values, now_ms=0)
            self.assertEqual(ring.to_bytes(), before)
        for raw in (b"", before[:-1], before + b"0"):
            with self.assertRaises(IntegrityError):
                BucketRing.from_bytes(raw)

    def test_seeded_lookup_matches_sorted_reference(self):
        rng = random.Random(44)
        ring = BucketRing(60000, 1536)
        samples = []
        for bucket in range(3000):
            if rng.random() < .12:
                continue
            timestamp = bucket * 60000 + rng.randrange(60000)
            record = (timestamp, rng.random())
            ring.write(timestamp, record[1:], now_ms=timestamp)
            samples.append(record)
        latest = samples[-1][0] // 60000
        for _ in range(2000):
            target = rng.randrange((latest - 1535) * 60000, (latest + 1) * 60000)
            candidates = [x for x in samples if x[0] <= target and target - x[0] <= 60000 and x[0] // 60000 > latest - 1536]
            expected = max(candidates, default=None)
            self.assertEqual(ring.baseline(target, 60000), expected)
        self.assertTrue(ring.scrub())


class MemoryStoreTests(unittest.TestCase):
    def test_admission_and_stale_writer(self):
        size = BucketRing(60000, 8).nbytes
        store = MemoryStore(size)
        store.claim(1)
        store.create_ring("pulse:c0:oi:btc", 60000, 8, 1, 1)
        self.assertEqual(store.used_bytes, size)
        with self.assertRaises(MemoryError):
            store.create_ring("pulse:c0:oi:eth", 60000, 8, 1, 1)
        store.claim(2)
        with self.assertRaises(PermissionError):
            store.write_ring("pulse:c0:oi:btc", 0, (1.0,), 1, now_ms=0)
        store.write_ring("pulse:c0:oi:btc", 0, (2.0,), 2, now_ms=0)
        self.assertEqual(store.baseline("pulse:c0:oi:btc", 0, 0), (0, 2.0))
        with self.assertRaises(ValueError):
            store.claim(2)
