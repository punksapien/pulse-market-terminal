import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools/probes"))
from pulse_runtime_audit import audit


class RuntimeAuditTests(unittest.TestCase):
    def test_recorded_raw_source_math_and_tamper_refusal(self):
        source=Path(__file__).resolve().parents[1]/"docs/evidence/pulse_runtime_documents_cycle5_2026-10-26.json"
        evidence=json.loads(source.read_text())
        self.assertEqual(audit(evidence)["fallback_fraction_comparisons"],15)
        self.assertEqual(audit(evidence)["ladder_raw_open_comparisons"],75)
        for kind in ("numerator","reference","fraction","time","status"):
            bad=copy.deepcopy(evidence)
            row=bad["references"][0]
            if kind=="numerator": row["tick"]["price"]="1"
            if kind=="reference": row["fallback"]["references"][0]["price"]="1"
            if kind=="fraction": row["changes"]["1H"]["value_fraction"]="1"
            if kind=="time": row["changes"]["1H"]["reference_asof_ms"]+=60000
            if kind=="status": bad["status"]="stopped"
            with self.subTest(kind=kind),self.assertRaises(ValueError): audit(bad)


if __name__=="__main__": unittest.main()
