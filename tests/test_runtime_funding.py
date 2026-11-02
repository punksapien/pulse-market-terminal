from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import unittest
from unittest.mock import Mock

from pulse.metrics.funding import FundingMetadata, Settlement, settled_report, HOUR, GRACE
from pulse.store.funding_cache import encode, decode, FundingCache
from pulse.store.runtime_document import RuntimeDocument, document

NOW=1800000000000


def fixture(hours=4):
    meta=FundingMetadata("B-BTC_USDT","mapping",hours,hours,NOW+hours*HOUR-1000,
                         Decimal(".0001"),NOW)
    events=[Settlement(meta.pair_id,NOW-1000-i*hours*HOUR,Decimal(".0001")) for i in range(170)]
    return settled_report(events,meta,now_ms=NOW)


class RuntimeFundingTests(unittest.TestCase):
    def test_exact_codec_and_expiry_all_intervals(self):
        for h in (1,4,8):
            report=fixture(h)
            blob=encode(report)
            self.assertEqual(decode(blob.payload),report)
            self.assertEqual(blob.expires_ms,NOW-1000+h*HOUR+GRACE)
            self.assertEqual(blob.digest,hashlib.sha256(blob.payload).hexdigest())
        for raw in (b"{}",b"null",b"[]",b"not json",b'{"schema":"unknown"}'):
            with self.assertRaises(ValueError): decode(raw)
        with self.assertRaises(ValueError): encode(replace(fixture(),verified_at_ms=None))
        report=fixture()
        for broken in (replace(report.last,fraction=Decimal("NaN")),
                       replace(report.last,fraction=0.1),
                       replace(report.last,expires_at_ms=NOW+999999999),
                       replace(report.last,status="invented")):
            with self.assertRaises(ValueError): encode(replace(report,last=broken))

    def test_failed_refresh_retains_and_due_view_never_extends(self):
        report=fixture()
        store=Mock()
        store.read.return_value={"status":"available","revision":1,"payload":encode(report).payload}
        cache=FundingCache(store,"B-BTC_USDT","mapping")
        self.assertEqual(cache.refresh(replace(report,verified_at_ms=None),expected_revision=1),[b"retained_last_good"])
        store.publish.assert_not_called()
        store.failed.assert_called_once_with("settlement_verification_failed")
        kept=cache.read(now_ms=NOW+130000)["report"]
        self.assertEqual(kept,report)
        due=report.latest.time_ms+4*HOUR
        pending=cache.read(now_ms=due)["report"]
        self.assertEqual(pending.last.status,"settlement_pending")
        self.assertIsNone(pending.last.fraction)
        self.assertEqual(pending.verified_at_ms,report.verified_at_ms)
        expired=cache.read(now_ms=due+GRACE)["report"]
        self.assertEqual(expired.last.reason,"expired")
        with self.assertRaises(ValueError):
            FundingCache(store,"B-ETH_USDT","mapping").refresh(report,expected_revision=1)

    def test_reader_is_one_bounded_command_no_refresh(self):
        client=Mock()
        candidate=document({"n":"0.0000"},source_ms=NOW,expires_ms=NOW+10000)
        client.hmget.return_value=[candidate.payload,candidate.digest.encode(),str(NOW).encode(),
            str(NOW+10000).encode(),b"mapping",b"1",b"source_timeout",str(NOW+1).encode()]
        store=RuntimeDocument(client,"a"*32,"funding:btc","mapping")
        self.assertEqual(store.read(now_ms=NOW)["payload"],candidate.payload)
        client.info.assert_not_called(); client.eval.assert_not_called()
        self.assertEqual(store.read(now_ms=NOW+10000)["reason"],"document_expired")
        client.hmget.return_value[0]=b"corrupted"
        self.assertEqual(store.read(now_ms=NOW)["reason"],"document_integrity_failed")
        store.data="pulse:prod:forbidden"
        with self.assertRaises(ValueError): store.read(now_ms=NOW)

    def test_payload_caps_candidate_hash_and_identity(self):
        for value in ({"n":float("nan")},{"s":"x"*131072}):
            with self.assertRaises(ValueError): document(value,source_ms=NOW,expires_ms=NOW+1)
        client=Mock()
        store=RuntimeDocument(client,"a"*32,"snapshot:1h","mapping")
        with self.assertRaises(ValueError):
            store.publish(replace(document({},source_ms=NOW,expires_ms=NOW+1),digest="0"*64),expected_revision=0)
        client.info.assert_not_called()
        for channel in ("snapshot/1h","","X","x"*81):
            with self.assertRaises(ValueError): RuntimeDocument(client,"a"*32,channel,"mapping")
        # A rehashed but noncanonical stored representation is refused too.
        with self.assertRaises(ValueError): decode(json.dumps(json.loads(encode(fixture()).payload)).encode())


if __name__=="__main__": unittest.main()
