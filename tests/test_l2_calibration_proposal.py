from decimal import Decimal
import hashlib,json
from pathlib import Path
import unittest
from pulse.metrics.levels import magnitude,classify_basis

ROOT=Path(__file__).parents[1]

class ProposalTests(unittest.TestCase):
    def test_proposed_scales_are_reproducible_and_never_marked_approved(self):
        proposal=json.loads((ROOT/"docs/calibration/l2_policy.proposed.202609.json").read_text())
        raw=(ROOT/"docs/evidence/l2_calibration_202609.json").read_bytes();report=json.loads(raw)
        self.assertIsNone(proposal["approved_by"])
        self.assertEqual(proposal["status"],"proposal_only_not_activated")
        self.assertEqual(proposal["source_report_sha256"],hashlib.sha256(raw).hexdigest())
        self.assertEqual(report["pairs_requested"],50);self.assertEqual(report["pairs_processed"],50)
        self.assertEqual(report["coverage"]["CTUSDT"]["days"],0)
        self.assertEqual(report["coverage"]["COPPERUSDT"]["field_rejections"]["count_long_short_ratio_unavailable"],2)
        for key,scale in proposal["scales"].items():
            cuts=tuple(scale["cuts"])
            self.assertEqual(classify_basis(Decimal(0),cuts),1)
            for i,cut in enumerate(cuts):self.assertEqual(classify_basis(Decimal(cut),cuts),i+2)
            if key.startswith("open_interest"):
                metric={"1H":"oi_1h","4H":"oi_4h","24H":"oi_24h"}[key.split(".")[-1]]
                self.assertEqual([float(x) for x in cuts],report["metrics"][metric]["candidates"]["training_quantiles"]["cuts"])
        self.assertEqual([Decimal(x) for x in proposal["scales"]["long_short.v1"]["cuts"]],
            [magnitude("long_short.v1",x)[0] for x in ("1.5","1.8","3")])
        for metric in ("funding","funding_change"):
            self.assertFalse(report["metrics"][metric]["candidates"]["training_quantiles"]["positive_cuts"])
