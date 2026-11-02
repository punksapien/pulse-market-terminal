import importlib.util
from pathlib import Path
import struct
import unittest

from pulse.collectors.one_pair_oi import CurrentOI
from pulse.store.one_pair_oi import empty_capsule, RING_BYTES, CAPSULE_BYTES

spec = importlib.util.spec_from_file_location("capsule_reference", Path(__file__).resolve().parents[1]/"tools/probes/capsule_reference.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CapsuleReference, boundary_cases = module.CapsuleReference, module.boundary_cases


class CapsuleReferenceTests(unittest.TestCase):
    def test_empty_capsule_matches_layout(self):
        self.assertEqual(CapsuleReference().to_bytes(), empty_capsule())

    def test_conflict_and_recovery_preserve_explicit_unavailability(self):
        base = 1792990000100
        reference = CapsuleReference()
        first = CurrentOI("BTCUSDT",base,"100",100.)
        self.assertEqual(reference.write(first,now_ms=base), "written")
        frame = reference.to_bytes()
        self.assertEqual(reference.write(first,now_ms=base+1), "duplicate")
        self.assertEqual(reference.to_bytes(),frame)
        conflict = CurrentOI("BTCUSDT",base,"101",101.)
        self.assertEqual(reference.write(conflict,now_ms=base+1), "exact_timestamp_conflict")
        self.assertEqual(reference.to_bytes()[RING_BYTES+8:RING_BYTES+9],b"Q")
        self.assertEqual(int.from_bytes(reference.to_bytes()[-192:],"little"),0)
        self.assertEqual(reference.write(CurrentOI("BTCUSDT",base+1,"102",102.),now_ms=base+1),"written")
        self.assertEqual(reference.to_bytes()[RING_BYTES+8:RING_BYTES+9],b"R")
        self.assertEqual(int.from_bytes(reference.to_bytes()[-192:],"little"),1)

    def test_vector_coverage_shapes_and_no_fake_clock_required(self):
        now = 1792999000000
        cases = boundary_cases(now)
        self.assertEqual(len(cases),8)
        for name, reference, points in cases:
            self.assertEqual(len(reference.to_bytes()),CAPSULE_BYTES)
            for point in points:
                self.assertLessEqual(now-point.source_ms,900000)
                reference.write(point,now_ms=now)
            if name.startswith("advance_"):
                gap = int(name.split("_")[1])
                expected = 1 | (1 << gap) if gap<1536 else 1
                self.assertEqual(int.from_bytes(reference.to_bytes()[-192:],"little"),expected)

    def test_rejected_times_leave_entire_capsule_unchanged(self):
        reference = CapsuleReference()
        before = reference.to_bytes()
        for stamp, reason in ((1000001,"future_timestamp"),(0,"stale_timestamp")):
            self.assertEqual(reference.write(CurrentOI("BTCUSDT",stamp,"1",1.),now_ms=950000),reason)
            self.assertEqual(reference.to_bytes(),before)
