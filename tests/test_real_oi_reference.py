import importlib.util
from pathlib import Path
import sys
import unittest

path=Path(__file__).resolve().parents[1]/"tools/probes/pulse_real_oi.py"
sys.path.insert(0,str(path.parent))
spec=importlib.util.spec_from_file_location("real_oi_reference",path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RawReferenceTests(unittest.TestCase):
    def test_raw_source_recompute_boundaries_and_zero(self):
        current={"time":100000000,"openInterest":"110"}
        target=current["time"]-3600000
        for error,expected in ((0,"available"),(300000,"available"),(300001,"unavailable"),(-1,"unavailable")):
            result=module.raw_reference(current,[{"timestamp":target-error,"sumOpenInterest":"100"}],"1H")
            self.assertEqual(result["status"],expected)
            if expected=="available":
                self.assertEqual(float(result["value_fraction_exact"]),.1)
        result=module.raw_reference(current,[{"timestamp":target,"sumOpenInterest":"0"}],"1H")
        self.assertEqual(result["reason"],"zero_baseline")
