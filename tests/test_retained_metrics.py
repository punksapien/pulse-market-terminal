from dataclasses import asdict
from decimal import Decimal
import json
from unittest import TestCase

from pulse.collectors.preview_compose import retained_metrics,asdict_ls
from pulse.collectors.preview_pair import exact_json
from pulse.metrics.long_short import LSMetric
from pulse.metrics.open_interest import OIChange
from pulse.wire.metric_rows import add_ls,add_oi
from pulse.wire.rows import empty_row


class RetainedMetricTests(TestCase):
    def test_json_roundtrip_preserves_values_and_expiry_gate(self):
        now=1800000000000;mapping="sha256:"+"b"*64
        meta={"id":0,"pair_id":"B-BTC_USDT","symbol":"BTCUSDT","funding_hours":8}
        current={"status":"available","source_ms":now,"expires_ms":now+60000,
            "sources":["100"],"values":[100.0]}
        change=OIChange("available",None,.25,"increasing","own",now,now-3600000,3600000,0,meta["pair_id"])
        metric=LSMetric("available",None,Decimal("2.1234"),"ratio",asof_ms=now,expires_at_ms=now+60000)
        ls={"series":{"global":{"ratio":metric,"current":{"sources":["2.1234","0.68","0.32"]},"evolution":{}}},
            "divergence":LSMetric("unavailable","baseline_missing",None,"pp")}
        record=json.loads(json.dumps(exact_json({"derived":{"mapping_version":mapping,"pair_id":meta["pair_id"],
            "current":current,"changes":{"1H":asdict(change)},"baselines":{},"ls":asdict_ls(ls)}})))
        c,changes,_,restored=retained_metrics(record,meta,mapping)
        for stamp,status in ((now,"available"),(now+60000,"unavailable")):
            row=empty_row(meta,mapping)
            add_oi(row,c,changes,input_ref="pairs:0",now_ms=stamp)
            add_ls(row,restored,input_ref="pairs:0",now_ms=stamp)
            self.assertEqual(row["oi"]["quantity"]["status"],status)
            self.assertEqual(row["ls"]["global_ratio"]["status"],status)
            if status=="available":
                self.assertEqual(row["oi"]["change_1h"]["value"],"0.25")
                self.assertEqual(row["ls"]["global_ratio"]["value"],"2.1234")
        record["derived"]["pair_id"]="B-ETH_USDT"
        with self.assertRaisesRegex(ValueError,"derived_identity_refused"):
            retained_metrics(record,meta,mapping)
