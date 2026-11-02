import json
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

from pulse.metrics.price_change import (
    CandleOpen, PriceTick, WINDOW_MS, build_fallback, build_ladder,
    fallback_change, price_change, select_tick,
)
from pulse.observation import MAX_SOURCE_TIME_MS

D = Decimal
START = 1792980000000
VERSIONS = dict(mapping_version="m1", definition_version="price_change.v1", generation_id="g1")


def opens_for(pair="BTCUSDT", start=START, slots=5):
    return {start + i * 60000 - window: CandleOpen(pair, start + i * 60000 - window, D(100 + 10 * i))
            for i in range(slots) for window in WINDOW_MS.values()}


class PriceReferenceTests(unittest.TestCase):
    def ladder(self, opens=None, now=START, **versions):
        return build_ladder(opens_for() if opens is None else opens, pair_id="BTCUSDT", now_ms=now, **(VERSIONS | versions))

    def evaluate(self, tick, frame, now=None, **overrides):
        args = VERSIONS | dict(horizon="1H", numerator_expires_at_ms=START + 600000,
                               now_ms=tick.source_time_ms if now is None else now) | overrides
        return price_change(tick, frame, **args)

    def test_minute_boundary_uses_event_time_and_next_reference(self):
        frame = self.ladder()
        for horizon, window in WINDOW_MS.items():
            for offset, denominator in ((0, D(100)), (59999, D(100)), (60000, D(110)), (60001, D(110))):
                tick = PriceTick("BTCUSDT", START + offset, D(110))
                result = self.evaluate(tick, frame, horizon=horizon)
                self.assertEqual(result.status, "available")
                self.assertEqual(result.reference_asof_ms, START + offset // 60000 * 60000 - window)
                self.assertEqual(result.value_fraction, (D(110) - denominator) / denominator)
        delayed = self.evaluate(PriceTick("BTCUSDT", START + 10000, D(110)), frame, now=START + 70000)
        self.assertEqual(delayed.value_fraction, D("0.1"))
        self.assertEqual(delayed.numerator_age_ms, 60000)

    def test_ladder_and_numerator_expiry_are_not_extended_on_read(self):
        frame = self.ladder(now=START + 59000)
        self.assertEqual(frame.valid_until_ms - frame.built_at_ms, 241000)
        tick = PriceTick("BTCUSDT", START + 60000, D(110))
        for now, reason in ((frame.valid_until_ms - 1, None), (frame.valid_until_ms, "ladder_expired"), (frame.valid_until_ms + 1, "ladder_expired")):
            self.assertEqual(self.evaluate(tick, frame, now=now).reason, reason)
        self.assertEqual(self.evaluate(tick, frame, now=START + 120000, numerator_expires_at_ms=START + 120000).reason, "numerator_expired")
        self.assertEqual(self.evaluate(PriceTick("BTCUSDT", START - 1, D(100)), frame, now=START + 59000).reason, "outside_ladder")
        self.assertEqual(frame, self.ladder(now=START + 59000))

    def test_missing_reference_suppresses_only_one_horizon(self):
        for bad in (None, D(0), D(-1), D("NaN"), D("Infinity"), 100.0):
            opens = opens_for()
            key = START - WINDOW_MS["4H"]
            opens[key] = CandleOpen("BTCUSDT", key, bad)
            frame = self.ladder(opens)
            tick = PriceTick("BTCUSDT", START, D(110))
            for horizon in WINDOW_MS:
                result = self.evaluate(tick, frame, horizon=horizon)
                self.assertEqual(result.status, "unavailable" if horizon == "4H" else "available")
        empty = self.ladder({})
        self.assertEqual(self.evaluate(PriceTick("BTCUSDT", START, D(110)), empty).reason, "reference_missing_or_invalid")

    def test_pair_reference_and_version_mismatch_are_unavailable(self):
        frame = self.ladder()
        tick = PriceTick("BTCUSDT", START, D(110))
        for field in VERSIONS:
            self.assertEqual(self.evaluate(tick, frame, **{field: "different"}).reason, "version_mismatch")
        self.assertEqual(self.evaluate(replace(tick, pair_id="ETHUSDT"), frame).reason, "pair_or_ladder_mismatch")
        for candle in (CandleOpen("ETHUSDT", START - 3600000, D(100)), CandleOpen("BTCUSDT", START - 3600000 + 1, D(100))):
            corrupted = replace(frame, slots=((candle, None, None),) + frame.slots[1:])
            self.assertEqual(self.evaluate(tick, corrupted).reason, "reference_missing_or_invalid")
        next_frame = self.ladder(generation_id="g2")
        self.assertEqual(self.evaluate(tick, next_frame).reason, "version_mismatch")
        self.assertEqual(self.evaluate(tick, frame).value_fraction, D("0.1"))

    def test_tick_order_equal_time_and_invalid_inputs(self):
        current = PriceTick("BTCUSDT", START, D(100))
        for price in (D(0), D(-1), D("NaN"), D("Infinity"), 100.0):
            decision = select_tick(current, replace(current, price=price), pair_id="BTCUSDT", now_ms=START)
            self.assertEqual((decision.status, decision.tick), ("rejected", current))
        for offset, expected in ((-1, "ignored_out_of_order"), (0, "accepted_equal_time"), (5000, "accepted"), (5001, "rejected")):
            incoming = PriceTick("BTCUSDT", START + offset, D(110))
            decision = select_tick(current, incoming, pair_id="BTCUSDT", now_ms=START)
            self.assertEqual(decision.status, expected)
            if expected == "accepted_equal_time":
                self.assertEqual(decision.tick.price, D(110))
        self.assertEqual(select_tick(None, current, pair_id="ETHUSDT", now_ms=START).status, "rejected")

    def test_fallback_outside_future_ladder_uses_own_exact_references(self):
        source = START - 1
        tick = PriceTick("BTCUSDT", source, D(110))
        opens = {((source - window) // 60000) * 60000: CandleOpen("BTCUSDT", ((source - window) // 60000) * 60000, D(100)) for window in WINDOW_MS.values()}
        fallback = build_fallback(opens, tick, now_ms=START + 10000, **VERSIONS)
        self.assertEqual(self.evaluate(tick, self.ladder(), now=START + 10000).reason, "outside_ladder")
        for horizon in WINDOW_MS:
            for age, delayed in ((120000, False), (120001, True)):
                result = fallback_change(fallback, pair_id="BTCUSDT", horizon=horizon, now_ms=source + age,
                                         expires_at_ms=START + 240000, **VERSIONS)
                self.assertEqual(result.value_fraction, D("0.1"))
                self.assertEqual(result.delayed, delayed)
            expired = fallback_change(fallback, pair_id="BTCUSDT", horizon=horizon, now_ms=START + 240000,
                                      expires_at_ms=START + 240000, **VERSIONS)
            self.assertEqual(expired.reason, "fallback_expired")
        self.assertEqual(fallback_change(fallback, pair_id="ETHUSDT", horizon="1H", now_ms=START, expires_at_ms=START + 240000, **VERSIONS).reason, "pair_mismatch")
        self.assertEqual(fallback_change(fallback, pair_id="BTCUSDT", horizon="1H", now_ms=START, expires_at_ms=START + 240000, **(VERSIONS | {"generation_id": "g2"})).reason, "version_mismatch")
        fabricated = replace(fallback, built_at_ms=0)
        self.assertEqual(fallback_change(fabricated, pair_id="BTCUSDT", horizon="1H", now_ms=START, expires_at_ms=START + 240000, **VERSIONS).reason, "reference_missing_or_invalid")

    def test_recorded_four_contract_24h_ticker_fixtures(self):
        directory = Path(__file__).parent / "fixtures/price_change"
        files = sorted(directory.glob("binance_24h_*.json"))
        self.assertEqual(len(files), 4)
        for path in files:
            data = json.loads(path.read_text())
            ticker = data["ticker"]
            candle = data["kline_1m_at_openTime"]
            tick = PriceTick(data["symbol"], ticker["closeTime"], D(ticker["lastPrice"]))
            expected_minute = (tick.source_time_ms - WINDOW_MS["24H"]) // 60000 * 60000
            self.assertEqual(expected_minute, candle["openTime"])
            opens = {candle["openTime"]: CandleOpen(data["symbol"], candle["openTime"], D(candle["open"]))}
            frame = build_ladder(opens, pair_id=data["symbol"], now_ms=tick.source_time_ms, **VERSIONS)
            result = price_change(tick, frame, horizon="24H", now_ms=tick.source_time_ms,
                                  numerator_expires_at_ms=tick.source_time_ms + 120000, **VERSIONS)
            self.assertEqual(result.status, "available")
            self.assertEqual((result.value_fraction * 100).quantize(D(".001")), D(ticker["priceChangePercent"]))

    def test_decimal_context_and_structural_validation(self):
        tick = PriceTick("BTCUSDT", START, D(4))
        opens = opens_for()
        opens[START - 3600000] = CandleOpen("BTCUSDT", START - 3600000, D(3))
        frame = self.ladder(opens)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(self.evaluate(tick, frame).value_fraction, D("0.3333333333333333333333333333333333"))
        for malformed in (replace(frame, slots=()), replace(frame, slots=((),)), replace(frame, start_ms=START + 1)):
            self.assertEqual(self.evaluate(tick, malformed).reason, "malformed_ladder")
        for clock in (True, -1, MAX_SOURCE_TIME_MS + 1):
            with self.assertRaises(ValueError):
                self.ladder(now=clock)
        for slots in (True, 0, 9):
            with self.assertRaises(ValueError):
                build_ladder({}, pair_id="BTCUSDT", now_ms=START, slots=slots, **VERSIONS)
        with self.assertRaises(ValueError):
            build_ladder([], pair_id="BTCUSDT", now_ms=START, **VERSIONS)

    def test_extreme_arithmetic_and_malformed_fallback_fail_closed(self):
        tick = PriceTick("BTCUSDT", START, D("1e1000000"))
        self.assertEqual(self.evaluate(tick, self.ladder()).reason, "arithmetic_unavailable")
        normal = PriceTick("BTCUSDT", START, D(100))
        frame = build_fallback(opens_for(), normal, now_ms=START, **VERSIONS)
        for bad in (replace(frame, references=()), replace(frame, built_at_ms=MAX_SOURCE_TIME_MS)):
            result = fallback_change(bad, pair_id="BTCUSDT", horizon="1H", now_ms=START, expires_at_ms=START + 120000, **VERSIONS)
            self.assertEqual(result.reason, "malformed_fallback")
        with self.assertRaises(ValueError):
            build_fallback({}, replace(normal, price=D(0)), now_ms=START, **VERSIONS)
