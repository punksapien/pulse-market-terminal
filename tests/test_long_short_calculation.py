import json
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

from pulse.metrics.long_short import LSObservation, SERIES_TTL_MS, WINDOW_MS, common_bucket_divergence, ratio_metric, share_evolution, recent_divergence_slice

D = Decimal
NOW = 1792980000000


def point(time=NOW, series="global", long="0.6", short="0.4", ratio="1.5", pair="BTCUSDT"):
    return LSObservation(pair, series, time, D(ratio), D(long), D(short))


class LongShortTests(unittest.TestCase):
    def test_only_chosen_common_bucket_payload_can_refuse(self):
        old = point(time=NOW-40*300000, ratio="9")
        top = point(series="top")
        for globals in ([old, point()], [old, replace(old, ratio=D(8)), point()]):
            result = common_bucket_divergence(globals, [top], pair_id="BTCUSDT", now_ms=NOW)
            self.assertEqual(result.status, "available")
            self.assertEqual(recent_divergence_slice(globals, now_ms=NOW), (point(),))
        bad = replace(point(), ratio=D(9))
        result = common_bucket_divergence([point(time=NOW-300000), bad], [point(time=NOW-300000, series="top"), top], pair_id="BTCUSDT", now_ms=NOW)
        self.assertEqual(result.reason, "ratio_share_mismatch")

    def test_historical_source_rounding_and_log_symmetry(self):
        data = json.loads((Path(__file__).parent / "fixtures/long_short/mumbai_btc_2026-10-26.json").read_text())
        observation = LSObservation(data["pair_id"], data["series"], data["bucket_ms"], D(data["ratio"]), D(data["long_share"]), D(data["short_share"]))
        self.assertEqual(ratio_metric(observation, now_ms=data["bucket_ms"] + 300000).status, "available")
        high = ratio_metric(point(long="0.6667", short="0.3333", ratio="2"), now_ms=NOW)
        low = ratio_metric(point(long="0.3333", short="0.6667", ratio="0.5"), now_ms=NOW)
        self.assertEqual(high.level_basis, low.level_basis)
        self.assertEqual((high.direction, low.direction), ("long_dominant", "short_dominant"))
        self.assertEqual(high.classification_status, "uncalibrated")

    def test_expiry_is_per_series_and_boundary_is_exclusive(self):
        for series, ttl in SERIES_TTL_MS.items():
            self.assertEqual(ratio_metric(point(series=series), now_ms=NOW + ttl - 1).status, "available")
            self.assertEqual(ratio_metric(point(series=series), now_ms=NOW + ttl).reason, "expired")
        self.assertEqual(ratio_metric(point(series="top"), now_ms=NOW + 19 * 60000).status, "available")
        self.assertEqual(ratio_metric(point(), now_ms=NOW + 19 * 60000).reason, "expired")

    def test_validation_tolerances_and_malformed_payloads(self):
        cases = [
            (point(long="0.5002", short="0.5", ratio="1.0004"), None),
            (point(long="0.500200001", short="0.5", ratio="1.0004"), "share_sum_mismatch"),
            (point(long="0.5", short="0.5", ratio="1.005"), None),
            (point(long="0.5", short="0.5", ratio="1.00500001"), "ratio_share_mismatch"),
            (point(long="0.4", short="0.6", ratio="2"), "ratio_share_mismatch"),
            (point(long="0.6", short="0.41"), "share_sum_mismatch"),
            (point(long="1", short="0", ratio="1"), "zero_short_share"),
            (point(long="0", short="1", ratio="0"), "invalid_ratio"),
            (point(ratio="NaN"), "invalid_ratio"), (point(long="Infinity"), "invalid_share"),
            (replace(point(), long_share=0.6), "invalid_share"),
            (point(time=NOW + 1), "invalid_bucket"), (None, "missing_observation"),
            (point(series="positions"), "invalid_series"),
        ]
        for observation, reason in cases:
            with self.subTest(reason=reason):
                result = ratio_metric(observation, now_ms=NOW)
                self.assertEqual(result.reason, reason)
                if reason:
                    self.assertIsNone(result.value)
        self.assertEqual(ratio_metric(point(), now_ms=NOW - 5000).status, "available")
        self.assertEqual(ratio_metric(point(), now_ms=NOW - 5001).reason, "future_bucket")

    def test_evolution_is_pp_and_preserves_baseline_metadata(self):
        current = point(long="0.66", short="0.34", ratio="1.9412")
        for horizon, window in WINDOW_MS.items():
            for mode, error, available in (("own", 0, True), ("own", 300000, False), ("cold-start", 300000, True), ("cold-start", 600000, False)):
                baseline = point(time=NOW - window - error)
                result = share_evolution(current, baseline, horizon=horizon, mode=mode, now_ms=NOW)
                self.assertEqual(result.status, "available" if available else "unavailable")
                if available:
                    self.assertEqual((result.value, result.unit), (D(6), "pp"))
                    self.assertEqual(result.actual_duration_ms, window + error)
                    self.assertEqual(result.baseline_error_ms, error)
        self.assertEqual(share_evolution(current, None, horizon="1H", mode="own", now_ms=NOW).status, "unavailable")
        self.assertEqual(share_evolution(current, point(time=NOW - 3600000, pair="ETHUSDT"), horizon="1H", mode="own", now_ms=NOW).reason, "identity_mismatch")
        same = share_evolution(point(), point(time=NOW - 3600000), horizon="1H", mode="own", now_ms=NOW)
        self.assertEqual((same.value, same.direction), (D(0), "unchanged"))
        zero_share = point(time=NOW - 3600000, long="0", short="1", ratio="0")
        self.assertEqual(share_evolution(current, zero_share, horizon="1H", mode="own", now_ms=NOW).value, D(66))

    def test_latest_common_bucket_not_independent_latest_values(self):
        global_history = [point(time=NOW), point(time=NOW - 300000)]
        top_history = [point(time=NOW - 300000, series="top", long="0.7", short="0.3", ratio="2.3333")]
        result = common_bucket_divergence(global_history, top_history, pair_id="BTCUSDT", now_ms=NOW)
        self.assertEqual((result.value, result.unit, result.asof_ms), (D(10), "pp", NOW - 300000))
        self.assertEqual(result.source_age_ms, 300000)
        self.assertEqual(common_bucket_divergence([point()], top_history, pair_id="BTCUSDT", now_ms=NOW).reason, "no_common_bucket")
        old_top = [point(series="top")]
        expired = common_bucket_divergence([point()], old_top, pair_id="BTCUSDT", now_ms=NOW + 900000)
        self.assertEqual(expired.reason, "no_unexpired_common_bucket")
        self.assertEqual(ratio_metric(old_top[0], now_ms=NOW + 900000).status, "available")

    def test_duplicate_conflict_history_bounds_and_context(self):
        top = point(series="top", long="0.7", short="0.3", ratio="2.3333")
        self.assertEqual(common_bucket_divergence([point(), point()], [top], pair_id="BTCUSDT", now_ms=NOW).status, "available")
        conflict = point(long="0.5", short="0.5", ratio="1")
        self.assertEqual(common_bucket_divergence([point(), conflict], [top], pair_id="BTCUSDT", now_ms=NOW).reason, "conflicting_duplicate")
        self.assertEqual(common_bucket_divergence([point()] * 321, [top], pair_id="BTCUSDT", now_ms=NOW).reason, "history_bound_exceeded")
        self.assertEqual(common_bucket_divergence([point(pair="ETHUSDT")], [top], pair_id="BTCUSDT", now_ms=NOW).reason, "identity_mismatch")
        expected = ratio_metric(point(), now_ms=NOW)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(ratio_metric(point(), now_ms=NOW), expected)

    def test_balanced_negative_zero_and_extreme_input_cases(self):
        balanced = point(long="0.5", short="0.5", ratio="1")
        self.assertEqual(ratio_metric(balanced, now_ms=NOW).direction, "balanced")
        negative = share_evolution(balanced, point(time=NOW - 3600000), horizon="1H", mode="own", now_ms=NOW)
        self.assertEqual((negative.value, negative.direction), (D(-10), "decreasing"))
        equal = common_bucket_divergence([point()], [point(series="top")], pair_id="BTCUSDT", now_ms=NOW)
        self.assertEqual((equal.value, equal.direction), (D(0), "equal"))
        self.assertEqual(ratio_metric(replace(point(), series=[]), now_ms=NOW).reason, "invalid_series")
        tiny = point(long="1e-2000000", short="1", ratio="2e-2000000")
        self.assertEqual(ratio_metric(tiny, now_ms=NOW).reason, "arithmetic_unavailable")
        with self.assertRaises(ValueError):
            ratio_metric(point(), now_ms=True)
