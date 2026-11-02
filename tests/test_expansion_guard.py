import unittest
from unittest.mock import Mock

from pulse.store.expansion_guard import guard, ExpansionStopped
from pulse.store.staging_capsule import StagingCapsule


class ExpansionGuardTests(unittest.TestCase):
    def info(self, used):
        return {"used_memory":used,"maxmemory_policy":"noeviction",
                "used_memory_rss":used,"mem_fragmentation_ratio":1.0,"used_memory_peak":used}

    def test_hard_stop_and_reserve_boundaries(self):
        for used in (120000000,120000001):
            with self.assertRaisesRegex(ExpansionStopped,"gate_stop"):
                guard(self.info(used),approved_plan_fallback=True)
        with self.assertRaisesRegex(ExpansionStopped,"headroom"):
            guard(self.info(119000000),approved_plan_fallback=True)
        guard(self.info(118999998),additional_bytes=1,approved_plan_fallback=True)
        with self.assertRaisesRegex(ExpansionStopped,"headroom"):
            guard(self.info(118999999),additional_bytes=1,approved_plan_fallback=True)
        capacity, observation = guard(self.info(38000000),additional_bytes=25152,approved_plan_fallback=True)
        self.assertEqual(observation["used_memory_rss"],38000000)
        self.assertEqual(observation["mem_fragmentation_ratio"],1.0)
        self.assertEqual(capacity.ceiling_bytes,125000000)

    def test_unknown_usage_cannot_bypass_stop(self):
        for bad in (None,True,-1,"100"):
            with self.assertRaises(ExpansionStopped):
                guard(self.info(bad),approved_plan_fallback=True)

    def test_post_write_overshoot_is_reported_and_never_rolled_back(self):
        client = Mock()
        client.info.side_effect=[self.info(10000000),self.info(120000001)]
        client.evalsha.return_value=[b"written",b"1793000000000"]
        store=StagingCapsule(client,"a"*32,approved_plan_fallback=True)
        point=Mock(symbol="BTCUSDT",source_ms=1793000000000,quantity=1.,source_quantity="1")
        with self.assertRaises(ExpansionStopped) as result:
            store.write(point)
        self.assertEqual(result.exception.observation["used_memory"],120000001)
        client.evalsha.assert_called_once()
        client.delete.assert_not_called()

    def test_over_limit_owner_never_acquires(self):
        client=Mock()
        client.info.return_value=self.info(120000001)
        store=StagingCapsule(client,"a"*32,approved_plan_fallback=True)
        with self.assertRaises(ExpansionStopped):
            store.acquire()
        client.eval.assert_not_called()


class StopConfigurationTests(unittest.TestCase):
    def test_default_and_explicit_overrides_respect_approved_ceiling(self):
        from pulse.store.expansion_guard import _stop_bytes
        self.assertEqual(_stop_bytes({}), 120000000)
        for value in (40000000, 115000000, 120000000):
            self.assertEqual(_stop_bytes({"PULSE_MEMORY_STOP_BYTES": str(value)}), value)
        for bad in ("120000001", "39999999", "abc", "-1", "1e8", "", "１２0000000", " 120000000", "120000000\n"):
            with self.assertRaises(ValueError):
                _stop_bytes({"PULSE_MEMORY_STOP_BYTES": bad})

    def test_forty_mb_is_no_longer_an_implicit_stop(self):
        guard(ExpansionGuardTests().info(41000000), approved_plan_fallback=True)
