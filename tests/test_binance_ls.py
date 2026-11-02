from dataclasses import replace
from decimal import Decimal
import json
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from pulse.collectors.binance_ls import history_spec, parse_history, fetch_history
from pulse.store.ls_reader import read_metrics
from pulse.store.bucket_ring import BucketRing

NOW = 1800000000000


def row(stamp=NOW, long="0.6", short="0.4", ratio="1.5"):
    return {"symbol": "BTCUSDT", "timestamp": stamp,
            "longShortRatio": ratio, "longAccount": long, "shortAccount": short}


class Store:
    """Memory fixture uses the real ring; production reads use bounded Lua."""
    def __init__(self, series, payload):
        self.spec = history_spec("B-BTC_USDT", "BTCUSDT", series)
        points = parse_history(payload, self.spec, now_ms=NOW)
        self.ring = BucketRing(self.spec.step_ms, self.spec.capacity, 3)
        self.lookups = []
        for point in points:
            self.ring.write(point.source_ms, point.values, now_ms=NOW)
        self.current = ({"status": "available", "source_ms": points[-1].source_ms,
                         "values": points[-1].values, "sources": points[-1].sources}
                        if points else {"status": "unavailable", "reason": "missing"})

    def read(self):
        return self.current

    def baseline(self, target, tolerance):
        self.lookups.append((target, tolerance))
        value = self.ring.baseline(target, tolerance)
        return ({"status": "available", "source_ms": value[0], "values": value[1:]}
                if value else {"status": "unavailable", "reason": "baseline_missing"})


class LSAdapterTests(unittest.TestCase):
    def setUp(self):
        self.spec = history_spec("B-BTC_USDT", "BTCUSDT", "global")

    def test_specs_empty_order_duplicates_and_original_precision(self):
        self.assertEqual(self.spec.ttl_ms, 900000)
        self.assertEqual(history_spec("B-BTC_USDT", "BTCUSDT", "top").ttl_ms, 2100000)
        self.assertEqual(parse_history([], self.spec, now_ms=NOW), ())
        payload = [row(NOW), row(NOW - 300000), row(NOW, ratio="1.5000")]
        points = parse_history(payload, self.spec, now_ms=NOW)
        self.assertEqual([p.source_ms for p in points], [NOW-300000, NOW])
        self.assertEqual(points[-1].sources[0], "1.5000")
        self.assertEqual(points[-1].expires_ms, NOW+900000)
        payload[0]["timestamp"] = str(NOW)
        self.assertEqual(len(parse_history(payload, self.spec, now_ms=NOW)), 2)

    def test_invalid_payloads_fail_before_store(self):
        cases = [(None, "response_size"), ([row()] * 321, "response_size")]
        for key, value, reason in (
                ("symbol", "ETHUSDT", "identity"), ("timestamp", True, "timestamp"),
                ("timestamp", NOW+1, "timestamp"), ("timestamp", NOW+300000, "future"),
                ("longAccount", "0.61", "share_sum"), ("shortAccount", "0", "zero_short"),
                ("longShortRatio", "2", "ratio_share"), ("longAccount", "-0.1", "sign"),
                ("longShortRatio", "NaN", "decimal_source"), ("longShortRatio", 1.5, "decimal_source"),
                ("longShortRatio", "1.50000000000000001", "precision"),
                ("longShortRatio", "0", "invalid_ratio")):
            item = row()
            item[key] = value
            cases.append(([item], reason))
        cases.append(([row(), row(long="0.5", short="0.5", ratio="1")], "conflict"))
        for payload, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                parse_history(payload, self.spec, now_ms=NOW)

    def test_old_history_is_validated_without_live_ttl_and_trimmed(self):
        points = parse_history([row(NOW-90000000), row(NOW)], self.spec, now_ms=NOW)
        self.assertEqual(len(points), 2)
        self.assertLess(points[0].expires_ms, NOW)
        self.assertEqual(parse_history([row(NOW-96300000)], self.spec, now_ms=NOW), ())
        points = parse_history([row(NOW-96000000), row(NOW)], self.spec, now_ms=NOW)
        self.assertEqual(len(points), 1)  # Same modulo slot, outside 320-bucket retention.

    def test_wrong_descriptor_clock_and_series_do_not_reserve(self):
        admission = Mock()
        for spec in (None, replace(self.spec, ttl_ms=1000)):
            with self.assertRaises(ValueError):
                fetch_history(spec, admission, clock=lambda: NOW)
        with self.assertRaises(ValueError):
            fetch_history(self.spec, admission, clock=True)
        for clock in (-1, True, 2**53):
            with self.assertRaises(ValueError):
                parse_history([], self.spec, now_ms=clock)
        for series in ("positions", None):
            with self.assertRaises(ValueError):
                history_spec("B-BTC_USDT", "BTCUSDT", series)
        for limit in (0, 321, True, "3"):
            with self.assertRaisesRegex(ValueError, "limit"):
                fetch_history(self.spec, admission, clock=lambda: NOW, limit=limit)
        admission.reserve.assert_not_called()

    def test_transport_uses_accounts_costs_no_key_and_response_feedback(self):
        admission = Mock()
        admission.reserve.return_value = [b"reserved"]
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = json.dumps([row()]).encode()
        with patch("pulse.collectors.binance_transport.build_opener", return_value=opener):
            for series, path in (("global", "globalLongShortAccountRatio"),
                                 ("top", "topLongShortAccountRatio")):
                spec = history_spec("B-BTC_USDT", "BTCUSDT", series)
                result = fetch_history(spec, admission, clock=lambda: NOW)
                self.assertEqual(len(result), 1)
                request = opener.open.call_args.args[0]
                self.assertIn(path+"?symbol=BTCUSDT&period=5m&limit=320", request.full_url)
                self.assertNotIn("X-mbx-apikey", request.headers)
                admission.reserve.assert_called_with({"history": 1, "weight": 0})
            fetch_history(self.spec, admission, clock=lambda: NOW, limit=3)
            self.assertIn("limit=3", opener.open.call_args.args[0].full_url)
            response.read.return_value = json.dumps([row(), row()]).encode()
            with self.assertRaisesRegex(ValueError, "response_size"):
                fetch_history(self.spec, admission, clock=lambda: NOW, limit=1)
            for code in (403, 418, 429, 451):
                opener.open.reset_mock()
                opener.open.side_effect = HTTPError("https://fapi.binance.com", code, "", {}, None)
                with self.assertRaisesRegex(RuntimeError, "http_"+str(code)):
                    fetch_history(self.spec, admission, clock=lambda: NOW)
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(admission.feedback.call_args.args[0], code)


class LSReadTests(unittest.TestCase):
    def test_exact_decimal_evolution_and_same_bucket_divergence(self):
        history = [row(NOW-window, long="0.5", short="0.5", ratio="1")
                   for window in (3600000, 14400000, 86400000)] + [row()]
        global_store = Store("global", history)
        top = Store("top", [row(NOW, long="0.7", short="0.3", ratio="2.3333")])
        result = read_metrics(global_store, top, now_ms=NOW)
        self.assertEqual(result["series"]["global"]["ratio"].value, Decimal("1.5"))
        for horizon in ("1H", "4H", "24H"):
            value = result["series"]["global"]["evolution"][horizon]["metric"]
            self.assertEqual(value.value, Decimal("10"))
            self.assertEqual(value.mode, "cold-start")
            self.assertEqual(value.baseline_error_ms, 0)
        self.assertEqual(result["divergence"].value, Decimal("10"))
        self.assertLessEqual(len(global_store.lookups), 7)
        self.assertEqual(result["series"]["global"]["ratio"].classification_status, "uncalibrated")

    def test_lagged_top_uses_common_not_independent_latest_and_expiry(self):
        global_store = Store("global", [row(NOW-300000), row()])
        top = Store("top", [row(NOW-300000, long="0.7", short="0.3", ratio="2.3333")])
        result = read_metrics(global_store, top, now_ms=NOW)
        self.assertEqual(result["divergence"].asof_ms, NOW-300000)
        self.assertEqual(result["divergence"].value, Decimal("10"))
        result = read_metrics(global_store, top, now_ms=NOW+600000)
        self.assertEqual(result["divergence"].status, "unavailable")
        self.assertEqual(result["series"]["top"]["ratio"].status, "available")
        self.assertEqual(read_metrics(global_store, top, now_ms=NOW+2100000)["series"]["top"]["ratio"].reason, "expired")

    def test_empty_missing_baseline_and_corrupt_candidate_fail_closed(self):
        global_store, top = Store("global", []), Store("top", [])
        result = read_metrics(global_store, top, now_ms=NOW)
        self.assertEqual(result["series"]["global"]["ratio"].reason, "missing")
        self.assertEqual(result["divergence"].status, "unavailable")
        global_store, top = Store("global", [row()]), Store("top", [row()])
        result = read_metrics(global_store, top, now_ms=NOW)
        self.assertEqual(result["series"]["global"]["evolution"]["1H"]["metric"].reason, "baseline_missing")
        top.baseline = lambda *args: {"status": "unavailable", "reason": "quarantined"}
        self.assertEqual(read_metrics(global_store, top, now_ms=NOW)["divergence"].reason, "quarantined")
        top.spec = replace(top.spec, pair_id="B-ETH_USDT")
        with self.assertRaisesRegex(ValueError, "identity"):
            read_metrics(global_store, top, now_ms=NOW)

    def test_numerical_source_bridge_preserves_evolution(self):
        # Trailing zeros may disappear from history, numeric source precision may not.
        global_store = Store("global", [row(NOW-3600000, long="0.5000", short="0.5000", ratio="1.0000"), row()])
        top = Store("top", [])
        result = read_metrics(global_store, top, now_ms=NOW)
        self.assertEqual(result["series"]["global"]["evolution"]["1H"]["metric"].value, Decimal("10.0"))

    def test_read_error_and_missing_exact_bucket_are_not_interpolated(self):
        global_store = Store("global", [row(NOW-600000), row()])
        top = Store("top", [row(NOW-300000)])
        self.assertEqual(read_metrics(global_store, top, now_ms=NOW)["divergence"].status, "unavailable")
        global_store.current = {"status": "unavailable", "reason": "quarantined"}
        result = read_metrics(global_store, top, now_ms=NOW)
        self.assertEqual(result["series"]["global"]["ratio"].reason, "quarantined")


if __name__ == "__main__":
    unittest.main()
