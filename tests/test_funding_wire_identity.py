from decimal import Decimal
import unittest
from pulse.metrics.funding import FundingMetadata,FundingEstimate,Settlement,settled_report,current_delta


class FundingWireIdentityTests(unittest.TestCase):
    def test_public_and_limit_mapping_tokens_preserve_arithmetic(self):
        now=1800000000000; latest=now-1000; h=8; due=latest+h*3600000
        for mapping in ("sha256:"+"a"*64,"v"*128,"v"*129,"",None):
            meta=FundingMetadata("B-BTC_USDT",mapping,h,h,due,Decimal("0.00015"),now)
            events=[Settlement(meta.pair_id,latest-h*3600000,Decimal("0.0001")),
                    Settlement(meta.pair_id,latest,Decimal("0.00015"))]
            report=settled_report(events,meta,now_ms=now)
            if not isinstance(mapping,str) or not 1<=len(mapping)<=128:
                self.assertEqual(report.last.reason,"invalid_identity")
                continue
            self.assertEqual(report.last.status,"available")
            estimate=FundingEstimate(meta.pair_id,mapping,h,due,meta.settled_rate,Decimal("0.0002"),now)
            result=current_delta(estimate,report,now_ms=now)
            self.assertEqual(result.fraction,Decimal("0.00005"))
            self.assertEqual(result.bps,Decimal("0.5"))


if __name__=="__main__": unittest.main()
