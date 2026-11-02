from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import Mock

from pulse.collectors.preview_compose import prepare_funding
from pulse.wire.identity import digest


class FundingLaneTests(unittest.TestCase):
    def fixture(self,h):
        now=1800000000000
        meta=dict(id=0,pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=h)
        universe=[meta]
        publisher=Mock(mapping_version=digest(universe))
        publisher.register_source.side_effect=lambda name,body,**kw:(1,"sha256:"+hashlib.sha256(body).hexdigest())
        records={"BTCUSDT":{"instrument":{"pair":"B-BTC_USDT","quote_currency_short_name":"USDT",
            "settle_currency_short_name":"USDT","funding_frequency":h},"instrument_ms":now,"funding_report":"retained-not-mutated"}}
        bulk={"ts":now,"prices":{"B-BTC_USDT":{"mkt":"BTCUSDT","fr":"0.0001","efr":"-0.00002","bmST":now}}}
        info={"source_ms":now,"data":{"BTCUSDT":{"symbol":"BTCUSDT","fundingIntervalHours":h}}}
        premium={"source_ms":now,"data":{"BTCUSDT":{"symbol":"BTCUSDT","time":now,"nextFundingTime":now+h*3600000}}}
        return now,universe,records,publisher,bulk,info,premium

    def document(self,args):
        now,universe,records,publisher,bulk,info,premium=args
        publication=prepare_funding(universe,records,publisher,bulk,info,premium,now_ms=now)
        descriptor=json.loads(publication.pointer)
        return publication,json.loads(dict(publication.blobs)[descriptor["logical_sha256"]])

    def test_exact_signed_native_rates_all_intervals_keep_their_source_time(self):
        for h in (1,4,8):
            args=self.fixture(h);before=deepcopy(args[2])
            publication,doc=self.document(args)
            row=doc["rows"][0]
            self.assertEqual((row["status"],row["efr"],row["fr"]),("available","-0.00002","0.0001"))
            self.assertEqual(row["source_ms"],args[0])
            self.assertEqual(doc["expires_ms"]-doc["generated_ms"],15000)
            self.assertEqual(publication.channel,"live_funding_v1")
            self.assertEqual(len(publication.input_revisions),4)
            # ADR-0116: the live row carries its own pilot band; efr -0.00002 is 2.6 / 1.4 / 1.2 bps from the anchor at 1h / 4h / 8h
            self.assertEqual((row["level"],row["classification_version"]),({1:2,4:1,8:1}[h],"funding.v1.l2.pilot1"))
            self.assertEqual(args[2],before)

    def test_old_or_foreign_metadata_is_unavailable_not_retimestamped(self):
        for kind in ("old","foreign","missing"):
            args=list(self.fixture(4))
            if kind=="old":args[2]["BTCUSDT"]["instrument_ms"]-=130000
            elif kind=="foreign":args[2]["BTCUSDT"]["instrument"]["pair"]="B-ETH_USDT"
            else:args[2]={"BTCUSDT":None}
            publication,doc=self.document(args)
            self.assertEqual(doc["rows"][0]["status"],"unavailable")
            self.assertIsNone(doc["rows"][0]["efr"])
