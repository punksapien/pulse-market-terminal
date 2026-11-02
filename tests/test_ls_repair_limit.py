"""Bounded gap recovery must not add requests or restamp source clocks."""
import unittest
from unittest.mock import Mock

from pulse.collectors.preview_pair import ls_repair_limit,bootstrap_history_limit


class RepairLimitTests(unittest.TestCase):
    def test_elapsed_buckets(self):
        now = 100_000_000
        for age, expected in ((0, 3), (300_000, 3), (300_001, 4),
                              (900_000, 5), (100_000_000, 320)):
            with self.subTest(age=age):
                self.assertEqual(ls_repair_limit(
                    {"global_source_ms": now-age}, "global", now), expected)
        self.assertEqual(ls_repair_limit({}, "global", now), 320)
        self.assertEqual(ls_repair_limit(
            {"top_refreshed_ms": now-900_000}, "top", now), 5)

    def test_bad_clocks(self):
        for stamp in (True, "1", -1, 100_005_001):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                ls_repair_limit({"top_source_ms": stamp}, "top", 100_000_000)

    def test_reused_bootstrap_requires_exact_retained_boundary(self):
        now=100_000_000;stamp=now-3_600_000;store=Mock()
        store.baseline.return_value=dict(status='available',source_ms=stamp)
        self.assertEqual(bootstrap_history_limit(store,{'cold_source_ms':stamp},'cold',now),14)
        store.baseline.assert_called_once_with(stamp,0)
        for result in (dict(status='unavailable',reason='discarded_baseline'),
                       dict(status='unavailable',reason='missing_or_invalid_frame'),
                       dict(status='available',source_ms=stamp-1)):
            store.baseline.return_value=result
            self.assertEqual(bootstrap_history_limit(store,{'cold_source_ms':stamp},'cold',now),320)
        store.reset_mock()
        self.assertEqual(bootstrap_history_limit(store,{},'cold',now),320)
        store.baseline.assert_not_called()
        for bad in (True,-1,now+5001,'old'):
            with self.assertRaises(ValueError):bootstrap_history_limit(store,{'cold_source_ms':bad},'cold',now)


if __name__ == "__main__":
    unittest.main()
