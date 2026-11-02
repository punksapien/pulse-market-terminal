import math
import os
import unittest
from unittest.mock import patch

from api.index import _enrich_with_signals
from pulse.legacy.signal_engine import enrich_token
from pulse.legacy.signal_oi import (
    compute_oi_metrics,
    fetch_oi_metrics,
    unavailable_oi_metrics,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def mount(self, *_args, **_kwargs):
        return None

    def get(self, *_args, **_kwargs):
        return FakeResponse(self.payload)


class OIMetricTests(unittest.TestCase):
    def test_binance_metrics_use_chronological_usd_notional(self):
        history = [
            {
                "timestamp": 1,
                "sumOpenInterest": "10",
                "sumOpenInterestValue": "100",
            },
            {
                "timestamp": 2,
                "sumOpenInterest": "11",
                "sumOpenInterestValue": "110",
            },
            {
                "timestamp": 3,
                "sumOpenInterest": "12",
                "sumOpenInterestValue": "120",
            },
        ]
        result = compute_oi_metrics(
            history,
            provider="binance",
            source_symbol="BTCUSDT",
        )
        self.assertTrue(result["oi_available"])
        self.assertEqual(result["oi_source"], "binance")
        self.assertEqual(result["oi_usd"], 120.0)
        self.assertAlmostEqual(result["oi_24h_change"], math.log(1.2), places=4)
        self.assertEqual(result["oi_asof"], 3)

    def test_bybit_descending_history_is_sorted_before_calculation(self):
        history = [
            {"timestamp": "3", "openInterest": "120"},
            {"timestamp": "2", "openInterest": "110"},
            {"timestamp": "1", "openInterest": "100"},
        ]
        result = compute_oi_metrics(
            history,
            provider="bybit",
            price_hint=2.0,
            source_symbol="BTCUSDT",
        )
        self.assertTrue(result["oi_available"])
        self.assertEqual(result["oi_source"], "bybit")
        self.assertEqual(result["oi_usd"], 240.0)
        self.assertAlmostEqual(result["oi_24h_change"], math.log(1.2), places=4)
        self.assertEqual(result["oi_asof"], 3)

    def test_unavailable_oi_is_not_encoded_as_zero(self):
        result = unavailable_oi_metrics("missing")
        self.assertFalse(result["oi_available"])
        self.assertIsNone(result["oi_24h_change"])
        self.assertIsNone(result["oi_usd"])
        self.assertEqual(result["oi_trend"], "unavailable")

    @patch("pulse.legacy.signal_oi.fetch_bybit_oi")
    @patch("pulse.legacy.signal_oi.fetch_binance_oi")
    def test_provider_failover_uses_bybit_when_binance_has_no_data(
        self,
        fetch_binance,
        fetch_bybit,
    ):
        fetch_binance.return_value = []
        fetch_bybit.return_value = [
            {"timestamp": "1", "openInterest": "100"},
            {"timestamp": "2", "openInterest": "110"},
        ]
        result = fetch_oi_metrics(
            "B-BTC_USDT",
            market_symbol="BTCUSDT",
            price_hint=10,
            providers=["binance", "bybit"],
        )
        self.assertTrue(result["oi_available"])
        self.assertEqual(result["oi_source"], "bybit")
        fetch_binance.assert_called_once()
        fetch_bybit.assert_called_once()

    def test_vercel_prefers_bybit_and_local_prefers_binance(self):
        with patch.dict(os.environ, {}, clear=True):
            from pulse.legacy.signal_oi import get_oi_provider_order
            self.assertEqual(get_oi_provider_order(), ["binance", "bybit"])
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
            from pulse.legacy.signal_oi import get_oi_provider_order
            self.assertEqual(get_oi_provider_order(), ["bybit", "binance"])


class OIAvailabilitySignalTests(unittest.TestCase):
    def _base_token(self):
        return {
            "volatility_score": 0.05,
            "vol_1h": 0.05,
            "vol_4h": 0.04,
            "vol_24h": 0.03,
            "vol_7d_avg": 0.03,
            "vol_percentile_30d": 0.5,
            "quality_score": 1.0,
            "dollar_vol_24h": 1_000_000,
            "volume_24h": 100_000,
            "price_change_24h": 0.03,
            "price_change_1h": 0.01,
            "jump_fraction": 0.05,
            "funding_rate": 0.0,
            "rsi_14": 55,
        }

    def test_missing_oi_does_not_generate_oi_claims(self):
        token = self._base_token()
        token.update(unavailable_oi_metrics("unsupported"))
        result = enrich_token(token, 86400)
        self.assertNotIn("flat OI", result["card_rationale"])
        self.assertIn("OI unavailable", result["card_rationale"])
        self.assertFalse(any(
            reason.get("icon") == "Open Interest"
            for reason in result["quant_reasons"]
        ))

    @patch("api.index._get_cached_oi")
    def test_oi_change_does_not_modify_price_change_or_volume_contract(
        self,
        get_cached_oi,
    ):
        get_cached_oi.return_value = unavailable_oi_metrics("unsupported")
        report = {
            "meta": {"target_seconds": 3600},
            "summary": [{
                "instrument": "B-TEST_USDT",
                **self._base_token(),
            }],
            "instruments": {
                "B-TEST_USDT": {
                    "instrument": "B-TEST_USDT",
                    "symbol": "TEST",
                    **self._base_token(),
                }
            },
        }
        v3_payload = {
            "prices": {
                "B-TEST_USDT": {
                    "mkt": "TESTUSDT",
                    "ls": 10,
                    "pc": 5.6,
                    "v": 1_234,
                    "fr": 0.0001,
                    "h": 11,
                    "l": 9,
                }
            }
        }
        enriched = _enrich_with_signals(
            report,
            session=FakeSession(v3_payload),
        )
        row = enriched["summary"][0]
        self.assertAlmostEqual(row["price_change_24h"], 0.056, places=12)
        self.assertEqual(row["dollar_vol_24h"], 1_234.0)
        self.assertEqual(row["volume_24h"], 123.4)
        self.assertEqual(row["daily_volume"], 123.4)
        self.assertEqual(row["price_change_1h"], 0.01)


if __name__ == "__main__":
    unittest.main()
