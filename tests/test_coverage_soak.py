import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location("soak",Path(__file__).parents[1]/"tools/probes/pulse_coverage_soak.py")
soak=importlib.util.module_from_spec(spec);spec.loader.exec_module(soak)


class SoakTests(unittest.TestCase):
    def test_never_calls_missing_or_stale_fields_complete(self):
        report=dict(horizon="1H",generation="g",pairs=350,live_returns={},references={},
            all_fields={"x":dict(fresh_at_response=349,rows=350,reasons={})})
        self.assertEqual(soak.summarize(report)["status"],"coverage_gaps")
        report["all_fields"]["x"]["fresh_at_response"]=350
        self.assertEqual(soak.summarize(report)["status"],"coverage_complete")
        report["all_fields"]={}
        self.assertEqual(soak.summarize(report)["status"],"coverage_gaps")
