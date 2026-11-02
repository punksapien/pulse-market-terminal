import unittest
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from pulse.store.cohort_memory import marginal_projection, settled_sample
from tests.test_cohort_bootstrap import bootstrap


class CohortMemoryTests(unittest.TestCase):
    def test_dispatch_checks_header_after_pacing_and_boundary_wait(self):
        client = Mock()
        with patch.object(bootstrap,"source_admission"):
            dispatch = bootstrap.Dispatch(client,None)
        dispatch.clock = Mock(side_effect=[1_799_999,1_800_100])
        dispatch.stop = Mock()
        dispatch.stop.is_set.return_value = False
        dispatch.last_dispatch = 10
        dispatch.header = {"status":200,"used_weight_1m":1,"provider_date_ms":1_799_000}
        admission = Mock()
        admission.reserve.return_value = [b"reserved"]
        def heartbeat(*args,**kwargs):
            dispatch.heartbeat.last_feedback = {"status":200,"used_weight_1m":2,"provider_date_ms":1_800_000}
        with patch.object(bootstrap.time,"monotonic",return_value=10.1), patch.object(
                bootstrap,"fetch_current",side_effect=heartbeat) as fetch:
            self.assertEqual(dispatch.admit(admission,{"history":1,"weight":0}),[b"reserved"])
        fetch.assert_called_once()
        waits = [call.args[0] for call in dispatch.stop.wait.call_args_list]
        self.assertAlmostEqual(waits[0],.1)
        self.assertAlmostEqual(waits[1],.101)
        self.assertAlmostEqual(waits[2],.2)
        self.assertEqual(dispatch.calls,2)

    def test_resume_keeps_identity_evidence_and_prior_counts_without_mutation(self):
        original = {"run_id":"fixture", "manifest_digest":"digest", "pairs":[{"symbol":"BTCUSDT"}],
                    "status":"stopped", "reason":"old_projection", "source_dispatches":21,
                    "cursor_persist_failed":True,"admission_reason":"old"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"previous.json"
            raw = json.dumps(original).encode()
            path.write_bytes(raw)
            result = bootstrap.resume_result(path,"digest")
            self.assertEqual(path.read_bytes(),raw)
            self.assertEqual(result["run_id"],"fixture")
            self.assertEqual(result["pairs"],original["pairs"])
            self.assertNotIn("reason",result)
            self.assertNotIn("cursor_persist_failed",result)
            self.assertNotIn("admission_reason",result)
            prior = result["prior_invocations"][0]
            self.assertEqual(prior["sha256"],hashlib.sha256(raw).hexdigest())
            self.assertEqual(prior["source_dispatches"],21)
            self.assertEqual(prior["reason"],"old_projection")
            with self.assertRaisesRegex(ValueError,"manifest_mismatch"):
                bootstrap.resume_result(path,"different")

    def test_spacing_and_latest_eligible_stage(self):
        stages = [{"pairs":5,"used_memory_median":10_000_000},
                  {"pairs":10,"used_memory_median":11_000_000}]
        for count in (5,10):
            value = marginal_projection(stages[:count//10],
                {"pairs":count,"used_memory_median":11_000_000},universe=504,ceiling=125_000_000)
            self.assertFalse(value["stop"])
            self.assertEqual(value["status"],"unavailable")
        value = marginal_projection(stages,{"pairs":25,"used_memory_median":14_000_000},
                                    universe=504,ceiling=125_000_000)
        self.assertEqual(value["prior_pairs"],10)
        self.assertEqual(value["bytes_per_pair"],200_000)
        self.assertEqual(value["projected_bytes"],109_800_000)
        self.assertFalse(value["stop"])  # >80% does not imply the 90% projection stop

    def test_limits_negative_growth_and_invalid_universe(self):
        for current,ceiling,stopped in ((2_000_000,10_000_000,True),
                                        (2_000_000,20_000_000,False)):
            v = marginal_projection([{"pairs":10,"used_memory_median":1_000_000}],
                {"pairs":20,"used_memory_median":current},universe=90,ceiling=ceiling)
            self.assertEqual(v["projected_bytes"],9_000_000)
            self.assertEqual(v["stop"],stopped)
        v = marginal_projection([{"pairs":5,"used_memory_median":3_000_000}],
            {"pairs":25,"used_memory_median":2_000_000},universe=504,ceiling=125_000_000)
        self.assertTrue(v["negative_slope_clamped"])
        self.assertEqual(v["projected_bytes"],2_000_000)
        with self.assertRaises(ValueError):
            marginal_projection([],{"pairs":25},universe=24,ceiling=125_000_000)

    def test_settle_retains_all_samples_and_guards_each(self):
        client,wait,clock = Mock(),Mock(),Mock(return_value=123)
        client.info.side_effect = [{"used_memory":v,"maxmemory_policy":"noeviction",
                                   "used_memory_rss":v*2,"mem_fragmentation_ratio":2,
                                   "used_memory_peak":10_000_000}
                                  for v in (2_000_000,8_000_000,3_000_000,4_000_000,5_000_000)]
        v = settled_sample(client,wait=wait,clock=clock)
        self.assertEqual(v["used_memory_median"],4_000_000)
        self.assertEqual(len(v["samples"]),5)
        self.assertEqual([c.args[0] for c in wait.call_args_list],[2,1,1,1,1])
        self.assertEqual(v["samples"][0]["used_memory_rss"],4_000_000)
        client.info.side_effect = None
        client.info.return_value = {"used_memory":120_000_000,"maxmemory_policy":"noeviction"}
        with self.assertRaisesRegex(RuntimeError,"gate_stop"):
            settled_sample(client,wait=wait,clock=clock)


if __name__ == "__main__":
    unittest.main()
