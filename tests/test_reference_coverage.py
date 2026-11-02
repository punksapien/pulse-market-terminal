import unittest
from dataclasses import replace
from decimal import Decimal

from pulse.metrics.price_change import CandleOpen, PriceTick, WINDOW_MS, build_ladder, price_change
from pulse.metrics.reference_coverage import ReferenceCoverageBudget


class ReferenceCoverageTests(unittest.TestCase):
    def test_true_minimum_and_concrete_margin(self):
        self.assertEqual(ReferenceCoverageBudget().assess(), {
            "minimum_remaining_ms": 240001, "maximum_delay_ms": 206000,
            "margin_ms": 34001, "admitted": True})
        self.assertFalse(replace(ReferenceCoverageBudget(), missed_poll_ms=120000).assess()["admitted"])
        self.assertFalse(replace(ReferenceCoverageBudget(), stale_while_revalidate_ms=120000).assess()["admitted"])

    def test_handoff_candidate_before_expiry_and_delayed_delivery(self):
        start = 1792980000000
        versions = dict(mapping_version="m1", definition_version="price_change.v1", generation_id="g1")
        opens = {start + i * 60000 - window: CandleOpen("BTCUSDT", start + i * 60000 - window, Decimal(100 + i))
                 for i in range(8) for window in WINDOW_MS.values()}
        old = build_ladder(opens, pair_id="BTCUSDT", now_ms=start + 59999, **versions)
        budget = ReferenceCoverageBudget()
        completed = old.built_at_ms + budget.producer_ms + budget.scheduler_jitter_ms + budget.lease_handoff_ms + budget.build_ms
        self.assertLess(completed, old.valid_until_ms)
        new_versions = versions | {"generation_id": "g2"}
        new = build_ladder(opens, pair_id="BTCUSDT", now_ms=completed, **new_versions)
        arrival = old.built_at_ms + budget.maximum_delay_ms()
        tick = PriceTick("BTCUSDT", arrival, Decimal(110))
        old_result = price_change(tick, old, horizon="1H", now_ms=arrival, numerator_expires_at_ms=start + 600000, **versions)
        new_result = price_change(tick, new, horizon="1H", now_ms=arrival, numerator_expires_at_ms=start + 600000, **new_versions)
        self.assertEqual(old_result.status, new_result.status, "available")
        self.assertEqual(old_result.status, "available")
        self.assertEqual(old_result.value_fraction, new_result.value_fraction)
        expired = price_change(tick, old, horizon="1H", now_ms=old.valid_until_ms, numerator_expires_at_ms=start + 600000, **versions)
        self.assertEqual(expired.reason, "ladder_expired")

    def test_invalid_budget(self):
        for budget in (replace(ReferenceCoverageBudget(), build_ms=-1), replace(ReferenceCoverageBudget(), build_ms=True)):
            with self.assertRaises(ValueError):
                budget.assess()
