import unittest
from unittest.mock import patch
from pulse.collectors import preview_pair as pp


class Src:
    last_source_ms = 1_800_000_000_000

    def __init__(self, rows):
        self.rows = rows

    def clock(self):
        return 1_800_000_000_000

    def request(self, path, params, **kw):
        return {"data": self.rows}


class LegacyOptionalTests(unittest.TestCase):
    def test_candle_validation_clock_cost_is_constant_and_future_still_refused(self):
        from unittest.mock import Mock
        now=1_800_000_000_000
        counts=[]
        keys=("volatility_score","regime","vol_direction","rsi_14","vol_momentum","vol_velocity_label",
              "trade_signal","vol_outlook","vol_outlook_confidence","vol_outlook_reasons")
        for size in (40,3000):
            source=Src([]);source.clock=Mock(return_value=now)
            rows=[dict(time=now-i*60000,open=1,high=1,low=1,close=1,volume=1) for i in range(size)]
            source.request=lambda path,params,**kw:[] if 'openInterestHist' in path else {"data":rows}
            with patch.object(pp,"evaluate",return_value={h:{k:1 for k in keys} for h in ("1H","4H","24H")}):
                result=pp.collect_legacy(source,"B-X_USDT","XUSDT",{},{} )
            counts.append(source.clock.call_count)
            self.assertEqual(result["legacy_source_ms"],source.last_source_ms)
        self.assertEqual(counts[0],counts[1])
        self.assertLess(counts[1],15)
        rows[0]["time"]=now+5001
        with self.assertRaisesRegex(ValueError,"legacy_candle_time"):
            pp.collect_legacy(source,"B-X_USDT","XUSDT",{},{} )

    def test_new_listing_with_few_candles_does_not_raise_and_marks_legacy_unavailable(self):
        rows = [{"time": 1_799_999_000_000 + i * 60000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1} for i in range(5)]
        prior = {"legacy": {"1H": {}}, "legacy_source_ms": 5}
        now = 1_800_000_000_000
        out = pp.refresh_legacy(Src(rows), "B-NEW_USDT", "NEWUSDT", {"mkt": "NEWUSDT"}, prior, now)
        self.assertNotIn("legacy", out)
        self.assertEqual(out["legacy_error"], "legacy_candle_response_bound")
        self.assertEqual(out["legacy_source_ms"], now, "retry after the refresh interval, not on every run")

    def test_fresh_legacy_is_left_alone(self):
        prior = {"legacy": {"1H": {"x": 1}}, "legacy_source_ms": 1_800_000_000_000 - 1000}
        out = pp.refresh_legacy(Src([]), "B-X_USDT", "XUSDT", {}, dict(prior), 1_800_000_000_000)
        self.assertEqual(out, prior)

    def test_refresh_starts_before_the_fifteen_minute_model_budget(self):
        now=1_800_000_000_000
        prior={"legacy_source_ms":now-13*60000}
        with patch.object(pp,"collect_legacy") as collect:
            pp.refresh_legacy(Src([]),"B-X_USDT","XUSDT",{},prior,now)
        collect.assert_called_once()
        self.assertLess(pp.LEGACY_REFRESH_MS,15*60000)

    def test_other_errors_are_not_swallowed(self):
        class Boom(Src):
            def request(self, *a, **k):
                raise RuntimeError("worker_source_budget_exhausted")
        with self.assertRaises(RuntimeError):
            pp.refresh_legacy(Boom([]), "B-X_USDT", "XUSDT", {}, {}, 1_800_000_000_000)


if __name__ == "__main__":
    unittest.main()
