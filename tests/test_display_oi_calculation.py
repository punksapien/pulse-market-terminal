import math
import unittest
from fractions import Fraction

from pulse.metrics.open_interest import OIObservation as Observation, quantity_change, WINDOW_MS
from pulse.store.bucket_ring import BucketRing, DiscardedBaseline
from pulse.observation import MAX_SOURCE_TIME_MS

NOW = 1792980000000


def OIObservation(timestamp, quantity, pair_id="BTCUSDT"):
    return Observation(timestamp, quantity, pair_id)


class OICalculationTests(unittest.TestCase):
    def test_all_horizons_and_tolerance_edges(self):
        for horizon, window in WINDOW_MS.items():
            for mode, tolerance in (("own", 60000), ("cold-start", 300000)):
                for error in (0, tolerance - 1, tolerance, tolerance + 1):
                    with self.subTest(horizon=horizon, mode=mode, error=error):
                        current = OIObservation(NOW, 110.0)
                        baseline = OIObservation(NOW - window - error, 100.0)
                        result = quantity_change(current, baseline, horizon=horizon, mode=mode, now_ms=NOW)
                        self.assertEqual(result.status, "available" if error <= tolerance else "unavailable")
                        self.assertEqual(result.asof_ms, NOW)
                        self.assertEqual(result.baseline_error_ms, error)
                        self.assertEqual(result.actual_duration_ms, window + error)
                        if result.status == "available":
                            self.assertAlmostEqual(result.value_fraction, .1)
                            self.assertEqual(result.direction, "increasing")
                            self.assertEqual(result.classification_status, "uncalibrated")

    def test_payload_and_zero_cases(self):
        current = OIObservation(NOW, 110.0)
        base = OIObservation(NOW - 3600000, 100.0)
        cases = [
            (None, base, "current_missing"), (current, None, "baseline_missing"),
            (OIObservation(True, 1.0), base, "current_malformed"),
            (OIObservation(-1, 1.0), base, "current_malformed"),
            (OIObservation(NOW, math.nan), base, "current_invalid_quantity"),
            (OIObservation(NOW, math.inf), base, "current_invalid_quantity"),
            (OIObservation(NOW, -1.0), base, "current_invalid_quantity"),
            (OIObservation(NOW, True), base, "current_invalid_quantity"),
            (current, OIObservation(NOW - 3600000, math.inf), "baseline_invalid_quantity"),
            (current, OIObservation(NOW - 3600000, -1.0), "baseline_invalid_quantity"),
            (current, OIObservation(NOW - 3600000, 100.0, "ETHUSDT"), "pair_mismatch"),
            (current, OIObservation(NOW - 3600000, 100.0, ""), "baseline_invalid_pair"),
            (current, OIObservation(NOW - 3600000, 0.0), "zero_baseline"),
            (current, OIObservation(NOW - 3600000 + 1, 100.0), "baseline_after_target"),
            (OIObservation(NOW, 1e308), OIObservation(NOW - 3600000, 1e-308), "nonfinite_result"),
        ]
        for numerator, denominator, reason in cases:
            with self.subTest(reason=reason):
                result = quantity_change(numerator, denominator, horizon="1H", mode="own", now_ms=NOW)
                self.assertEqual((result.status, result.reason, result.value_fraction), ("unavailable", reason, None))
        for quantity, expected, direction in ((0.0, -1.0, "decreasing"), (100.0, 0.0, "unchanged")):
            result = quantity_change(OIObservation(NOW, quantity), base, horizon="1H", mode="own", now_ms=NOW)
            self.assertEqual((result.status, result.value_fraction, result.direction), ("available", expected, direction))

    def test_source_age_and_skew_boundaries(self):
        for offset, reason in ((5000, None), (5001, "current_future"), (-900000, None), (-900001, "current_stale")):
            current = OIObservation(NOW + offset, 110.0)
            baseline = OIObservation(current.source_time_ms - 3600000, 100.0)
            result = quantity_change(current, baseline, horizon="1H", mode="own", now_ms=NOW)
            self.assertEqual(result.reason, reason)

    def test_ring_route_and_collision_guard(self):
        for mode, step, slots in (("own", 60000, 1536), ("cold-start", 300000, 320)):
            target = NOW - 3600000
            ring = BucketRing(step, slots)
            ring.write(target - step, (100.0,), now_ms=NOW)
            selected = ring.baseline(target, step)
            result = quantity_change(OIObservation(NOW, 110.0), OIObservation(*selected), horizon="1H", mode=mode, now_ms=NOW)
            self.assertEqual((result.status, result.mode, result.baseline_error_ms), ("available", mode, step))
        ring = BucketRing(60000, 1536)
        for timestamp in (target + 10000, target + 50000):
            ring.write(timestamp, (100.0,), now_ms=NOW)
        with self.assertRaises(DiscardedBaseline):
            ring.baseline(target + 30000, 60000)

    def test_bad_configuration_fails_explicitly(self):
        for horizon, mode, clock in (("2H", "own", NOW), ("1H", "inferred", NOW), ("1H", "own", True), ("1H", "own", MAX_SOURCE_TIME_MS + 1)):
            with self.assertRaises(ValueError):
                quantity_change(None, None, horizon=horizon, mode=mode, now_ms=clock)

    def test_near_unchanged_preserves_small_return(self):
        baseline = 1.5
        current = math.nextafter(baseline, math.inf)
        expected = float((Fraction(current) - Fraction(baseline)) / Fraction(baseline))
        result = quantity_change(OIObservation(NOW, current), OIObservation(NOW - 3600000, baseline), horizon="1H", mode="own", now_ms=NOW)
        self.assertEqual(result.value_fraction, expected)
        self.assertNotEqual(current / baseline - 1.0, expected)

    def test_source_timestamp_above_safe_integer_rejected(self):
        result = quantity_change(OIObservation(MAX_SOURCE_TIME_MS + 1, 110.0), OIObservation(NOW, 100.0), horizon="1H", mode="own", now_ms=NOW)
        self.assertEqual(result.reason, "current_malformed")
