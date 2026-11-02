import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

PROBES=Path(__file__).resolve().parents[1]/"tools/probes"
spec=importlib.util.spec_from_file_location("production_scope_probe",PROBES/"pulse_production_scope_parity.py")
probe=importlib.util.module_from_spec(spec)
sys.path.insert(0,str(PROBES))
try:spec.loader.exec_module(probe)
finally:sys.path.pop(0)


class ScopeProbeTests(unittest.TestCase):
    def raw(self):
        raw=Mock()
        raw.info.return_value={"used_memory":4000000,"maxmemory_policy":"noeviction"}
        raw.time.return_value=(1800000000,0)
        raw.get.return_value=b"synthetic-identical-frame"
        raw.ttl.return_value=-1
        return raw

    def capsule(self,client,run,spec,**kwargs):
        result=Mock()
        result.frame="pulse:stg:{"+run+"}:"+spec.pair_id+":"+spec.metric+":frame"
        result.owner=b"owner"
        result.write.side_effect=lambda point:[b"fenced_out"] if result.owner==b"stale-owner" else [b"written"]
        result.write_many.side_effect=lambda points:[[b"written"] for p in points]
        result.read.return_value={"status":"available"}
        result.baseline.return_value={"status":"available"}
        return result

    def test_all_six_descriptors_include_a_full_batch_and_old_owner_check(self):
        raw=self.raw()
        with patch.object(probe,"connect",return_value=raw),patch.object(probe,"MetricCapsule",side_effect=self.capsule), \
                patch.object(probe,"publication_parity",return_value={"encoding":"br"}) as publication:
            value=probe.run("cli")
        self.assertEqual(value["write_pairs"],132)
        self.assertEqual(len(value["cases"]),6)
        self.assertFalse(value["production_access"])
        self.assertTrue(all(c["batch_samples"]==16 for c in value["cases"]))
        publication.assert_called_once()
        self.assertEqual(value["publication"]["encoding"],"br")
        raw.close.assert_called_once()

    def test_real_frame_difference_fails_instead_of_reporting_parity(self):
        raw=self.raw();raw.get.side_effect=[b"a",b"b"]
        with patch.object(probe,"connect",return_value=raw),patch.object(probe,"MetricCapsule",side_effect=self.capsule):
            with self.assertRaisesRegex(RuntimeError,"scope_frame_parity_failed"):probe.run("cli")
        raw.close.assert_called_once()

    def test_isolated_preview_capacity_refusal_is_before_any_synthetic_write(self):
        for info in ({"used_memory":19000000,"maxmemory_policy":"noeviction"},
                     {"used_memory":1000000,"maxmemory_policy":"allkeys-lru"},
                     {"used_memory":None,"maxmemory_policy":"noeviction"}):
            raw=Mock();raw.info.return_value=info
            with patch.object(probe,"connect",return_value=raw),patch.object(probe,"MetricCapsule") as capsule:
                with self.assertRaisesRegex(RuntimeError,"isolated_preview_capacity_refused"):probe.run("cli")
                capsule.assert_not_called()
            raw.close.assert_called_once()
            raw.delete.assert_not_called()
