"""Offline orchestration witnesses; never connect to Redis or an exchange."""
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

PROBES = str(Path(__file__).resolve().parents[1]/"tools/probes")
sys.path.insert(0, PROBES)
import pulse_real_ls as probe
from tests.test_binance_ls import NOW, Store, row


class LSProbeTests(unittest.TestCase):
    def test_header_refresh_at_minute_rollover_age_and_invalid_feedback(self):
        feedback = {"status": 200, "used_weight_1m": 1, "provider_date_ms": NOW}
        for data, stamp, expected in ((None, NOW, True), ({}, NOW, True),
                (feedback, NOW+10000, False), (feedback, NOW+55000, True),
                (feedback, NOW+60000, True),
                ({**feedback, "provider_date_ms": NOW-1}, NOW, True),
                ({**feedback, "used_weight_1m": 0}, NOW, True)):
            self.assertEqual(probe.header_refresh_needed(data, stamp), expected)

    def test_independent_witness_matches_and_detects_numerical_and_time_errors(self):
        raw = {"global": [row(NOW-3600000, long="0.5", short="0.5", ratio="1"), row()],
               "top": [row(NOW, long="0.7", short="0.3", ratio="2.3333")]}
        expected = probe.raw_reference(raw, now_ms=NOW)
        computed = probe.read_metrics(Store("global", raw["global"]), Store("top", raw["top"]), now_ms=NOW)
        self.assertEqual(probe.verify(computed, expected), 4)
        metric = computed["series"]["global"]["ratio"]
        for bad, message in ((replace(metric, value=Decimal("1.6")), "ratio_mismatch"),
                             (replace(metric, asof_ms=NOW-1), "ratio_time_mismatch")):
            computed["series"]["global"]["ratio"] = bad
            with self.assertRaisesRegex(RuntimeError, message):
                probe.verify(computed, expected)
        computed["series"]["global"]["ratio"] = metric
        evolution = computed["series"]["global"]["evolution"]["1H"]
        correct = evolution["metric"]
        for bad, message in ((replace(correct, value=Decimal("9")), "evolution_mismatch"),
                             (replace(correct, comparison_asof_ms=NOW-1), "baseline_time_mismatch")):
            evolution["metric"] = bad
            with self.assertRaisesRegex(RuntimeError, message):
                probe.verify(computed, expected)
        evolution["metric"] = correct
        correct = computed["divergence"]
        for bad, message in ((replace(correct, value=Decimal("9")), "divergence_mismatch"),
                             (replace(correct, asof_ms=NOW-1), "divergence_time_mismatch")):
            computed["divergence"] = bad
            with self.assertRaisesRegex(RuntimeError, message):
                probe.verify(computed, expected)

    def test_empty_and_expired_are_unavailable_not_zero(self):
        for raw, now in (({"global": [], "top": []}, NOW),
                         ({"global": [row()], "top": [row()]}, NOW+2100000)):
            expected = probe.raw_reference(raw, now_ms=now)
            computed = probe.read_metrics(Store("global", raw["global"]), Store("top", raw["top"]), now_ms=now)
            self.assertEqual(probe.verify(computed, expected), 0)
            self.assertIsNone(expected["divergence"]["value_pp"])

    def test_operator_stops_on_failure_keeps_manifest_and_closes_client(self):
        client = Mock()
        client.info.return_value = {"used_memory": 10000000}
        client.time.return_value = (NOW//1000, 0)
        admission = Mock()
        admission.keys = ["pulse:stg:quota-fixture"]
        admission.last_feedback = {"status": 200, "used_weight_1m": 1, "provider_date_ms": NOW}
        checkpoint = Mock()
        def factory(client, run_id, spec, **kwargs):
            store = Mock()
            store.spec = spec
            store.frame = "pulse:stg:"+spec.metric+":frame"
            store.lease = "pulse:stg:"+spec.metric+":lease"
            store.fence = "pulse:stg:"+spec.metric+":fence"
            return store
        with ExitStack() as stack:
            stack.enter_context(patch.object(probe, "connect_staging", return_value=(client, {"transport": "fixture"})))
            stack.enter_context(patch.object(probe, "BinanceAdmission", return_value=admission))
            stack.enter_context(patch.object(probe, "MetricCapsule", side_effect=factory))
            stack.enter_context(patch.object(probe, "guard"))
            heartbeat = stack.enter_context(patch.object(probe, "fetch_current"))
            source = stack.enter_context(patch.object(probe, "fetch_history", side_effect=RuntimeError("fixture_source_denied")))
            result = probe.run("unused", symbols=["BTCUSDT"], stages=[1], scope="fixture",
                               run_id="a"*32, ca_file=None, checkpoint=checkpoint)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "fixture_source_denied")
        self.assertEqual(len(result["keys"]), 6)
        self.assertEqual(result["exchange_attempts"], 2)
        self.assertEqual(result["deletions"], 0)
        self.assertEqual(heartbeat.call_count, 1)
        self.assertEqual(source.call_count, 1)
        client.close.assert_called_once()
        self.assertGreaterEqual(checkpoint.call_count, 2)

    def test_evidence_decimal_serialization_never_coerces_to_float(self):
        self.assertEqual(probe.json_default(Decimal("0.00000000000000001")), "1E-17")
        with self.assertRaises(TypeError):
            probe.json_default(object())


if __name__ == "__main__":
    unittest.main()
