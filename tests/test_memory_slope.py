import importlib.util
from pathlib import Path
import sys
import unittest

PROBES=Path(__file__).resolve().parents[1]/"tools/probes"
spec=importlib.util.spec_from_file_location("memory_slope_probe",PROBES/"pulse_staging_memory_slope.py")
module=importlib.util.module_from_spec(spec)
sys.path.insert(0,str(PROBES))
try:
    spec.loader.exec_module(module)
finally:
    sys.path.pop(0)


class MemorySlopeTests(unittest.TestCase):
    def stage(self,count,used):
        return {"frames":count,"used_memory_median":used,"used_memory_samples":[used]*5}

    def test_direct_slope_passes_small_overhead(self):
        result=module.slope(self.stage(8,1000000),self.stage(16,1240000))
        self.assertEqual(result["bytes_per_added_frame"],30000)
        self.assertTrue(result["passes_1_3x"])

    def test_large_slope_fails(self):
        result=module.slope(self.stage(16,1000000),self.stage(32,5000000))
        self.assertFalse(result["passes_1_3x"])

    def test_delayed_negative_or_noisy_info_cannot_pass(self):
        for after in (self.stage(16,1000000),self.stage(16,900000)):
            self.assertFalse(module.slope(self.stage(8,1000000),after)["conclusive"])
        after=self.stage(16,1240000)
        after["used_memory_samples"]=[1000000,1240000,1240000,1240000,1500000]
        self.assertFalse(module.slope(self.stage(8,1000000),after)["passes_1_3x"])

    def test_reversed_counts_refused(self):
        with self.assertRaises(ValueError):
            module.slope(self.stage(16,1),self.stage(8,2))
