from datetime import datetime,timezone
from email.utils import format_datetime
import unittest
from unittest.mock import Mock

from pulse.store.binance_admission import BinanceAdmission,header_values
from pulse.store.shared_admission import RollingLimit


class HeaderTests(unittest.TestCase):
    def test_used_weight_date_and_retry_after(self):
        now=1793001600000
        date=format_datetime(datetime.fromtimestamp(now/1000,timezone.utc),usegmt=True)
        self.assertEqual(header_values({"x-mbx-used-weight-1m":"17","date":date,"Retry-After":"3"},now_ms=now),
                         (17,now,3000))
        retry=format_datetime(datetime.fromtimestamp((now+7000)/1000,timezone.utc),usegmt=True)
        self.assertEqual(header_values({"Date":date,"Retry-After":retry},now_ms=now)[2],7000)
        behind=format_datetime(datetime.fromtimestamp((now-2000)/1000,timezone.utc),usegmt=True)
        self.assertEqual(header_values({"Date":behind,"Retry-After":retry},now_ms=now)[2],9000)
        self.assertEqual(header_values({"Retry-After":retry},now_ms=now)[2],12000)
        for raw in ("-2","NaN","1.5","x","99999999999",None):
            self.assertEqual(header_values({"X-MBX-USED-WEIGHT-1M":raw},now_ms=now)[0],-1)
        self.assertEqual(header_values({"Date":"not-a-date"},now_ms=now)[1],-1)
        self.assertEqual(header_values({"Retry-After":"999999"},now_ms=now)[2],999999000)


class SourceControlTests(unittest.TestCase):
    def test_optional_history_reserve_preserves_mapping_charges_and_weight_cap(self):
        self.store=BinanceAdmission(self.client,"test",{
            "history":RollingLimit(850,300000),"weight":RollingLimit(1680,60000)},
            approved_plan_fallback=True,approved_bootstrap=True)
        identity=self.store.mapping
        self.client.evalsha.return_value=[b"budget_exhausted:1"]
        self.store.reserve({"history":1,"weight":0},history_headroom=32)
        args=self.client.evalsha.call_args.args
        self.assertEqual(args[-9],818);self.assertEqual(args[-5],1680)
        self.assertEqual(self.store.mapping,identity);self.assertEqual(self.store.limits["history"].cap,850)
        self.assertIsNone(self.store.pending)
        self.store.reserve({"history":0,"weight":1},history_headroom=32)
        self.assertEqual(self.client.evalsha.call_args.args[-9],850,"zero-history OI/header calls must remain allowed")
        self.store.reserve({"history":2,"weight":0})
        self.assertEqual(self.client.evalsha.call_args.args[-9],850,"metadata uses reserved portion within original cap")
        for bad in (True,-1,33,"32"):
            with self.assertRaisesRegex(ValueError,"invalid_history_headroom"):
                self.store.reserve({"history":1,"weight":0},history_headroom=bad)

    def setUp(self):
        self.client=Mock()
        self.client.info.return_value={"used_memory":5000000,"maxmemory_policy":"noeviction"}
        self.client.time.return_value=(1793001600,0)
        self.limits={"history":RollingLimit(700,300000),"weight":RollingLimit(1680,60000)}
        self.store=BinanceAdmission(self.client,"test",self.limits,approved_plan_fallback=True,approved_bootstrap=True)

    def test_header_control_is_a_bounded_scoped_read_not_budget_reconciliation(self):
        values=[b'a',b'R',b'1',b'10',b'0',b'5',b'0']
        self.client.hmget.return_value=values
        self.assertEqual(self.store.header_state(),values)
        self.client.hmget.assert_called_once_with(self.store.keys[-1],
            ['mapping','state','initialised','header_at','provider_minute','provider_floor','refresh_until'])
        self.client.evalsha.assert_not_called();self.client.hgetall.assert_not_called()
        self.store.keys[-1]='pulse:prod:forbidden'
        with self.assertRaises(ValueError):self.store.header_state()

    def test_bootstrap_nonce_persistent_charge_and_feedback(self):
        self.assertEqual(self.client.mock_calls,[])
        self.client.evalsha.return_value=[b"reserved",b"1793001600000",b"bootstrap"]
        self.store.reserve({"history":0,"weight":1})
        self.assertIsNotNone(self.store.pending)
        self.assertEqual(self.store.reserve({"history":0,"weight":1}),[b"previous_feedback_required"])
        date="Mon, 26 Oct 2026 10:40:00 GMT"
        self.client.evalsha.return_value=[b"feedback_recorded"]
        result=self.store.feedback(200,{"Date":date,"X-MBX-USED-WEIGHT-1M":"7"},draw=0)
        self.assertEqual(result["used_weight_1m"],7)
        self.assertIsNone(self.store.pending)
        with self.assertRaisesRegex(RuntimeError,"feedback_without_permit"):
            self.store.feedback(200,{})
        with self.assertRaisesRegex(RuntimeError,"requires_header_bootstrap"):
            self.store.reconcile({"history":0,"weight":0},evidence_id="x")

    def test_feedback_refusal_and_scope_fail_closed(self):
        self.client.evalsha.return_value=[b"circuit_open"]
        self.assertEqual(self.store.reserve({"history":0,"weight":1}),[b"circuit_open"])
        self.assertIsNone(self.store.pending)
        self.store.pending=("a"*32,1)
        self.client.evalsha.return_value=[b"bootstrap_header_unverified"]
        with self.assertRaisesRegex(RuntimeError,"feedback_refused"):
            self.store.feedback(200,{},draw=0)
        self.store.keys[0]="pulse:prod:meta"
        with self.assertRaisesRegex(ValueError,"scope_refused"):
            self.store.reserve({"history":0,"weight":1})

    def test_zero_weight_endpoint_and_missing_weighted_header(self):
        self.client.evalsha.return_value=[b"feedback_recorded"]
        for cost,expected in ((0,-1),(1,0)):
            self.store.pending=("a"*32,cost)
            self.store.feedback(200,{"X-MBX-USED-WEIGHT-1M":"0"},draw=0)
            self.assertEqual(self.store.last_feedback["used_weight_1m"],expected)
