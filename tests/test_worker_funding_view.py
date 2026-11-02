import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from pulse.store.worker_state import WorkerState,WRITE,READ_FUNDING,funding_projection

NOW=1800000000000


def record():
    return {"instrument":{"pair":"B-BTC_USDT","quote_currency_short_name":"USDT",
        "settle_currency_short_name":"USDT","funding_frequency":"4","unused":"x"*10000},
        "instrument_ms":NOW,"legacy":{"unused":"y"*10000}}


class FundingViewTests(TestCase):
    def state(self):
        self.client=Mock();self.client.eval.return_value=1;self.client.script_load.return_value="fund-sha"
        self.pub=SimpleNamespace(client=self.client,prefix="pulse:stg:{"+"a"*32+"}:publication:",
            lease="pulse:stg:{"+"a"*32+"}:publication:lease",owner=b"owner",preflight=Mock())
        return WorkerState(self.pub)

    def test_projection_is_atomic_with_fenced_full_record_write(self):
        state=self.state();value=record();state.write("BTCUSDT",value)
        args=self.client.eval.call_args.args
        self.assertEqual(args[:5],(WRITE,3,self.pub.lease,state.key,state.funding_key))
        self.assertEqual(json.loads(args[-1]),["B-BTC_USDT","4",NOW])
        self.assertLess(len(args[-1]),80)
        self.assertEqual(json.loads(args[-2]),value)
        self.assertEqual(state.io_stats()['sent_payload_bytes'],len(args[-1])+len(args[-2]))
        self.assertEqual(state.io_stats()['write_calls'],1)
        self.client.eval.return_value=0
        with self.assertRaisesRegex(RuntimeError,"worker_state_fenced_out"):state.write("BTCUSDT",value)

    def test_reader_is_bounded_and_never_fetches_full_records(self):
        state=self.state();names=[f"P{i}USDT" for i in range(505)]
        def read(sha,n,key,*batch):
            self.assertEqual((sha,n,key),("fund-sha",1,state.funding_key))
            self.assertLessEqual(len(batch),16)
            return [b"available"]+[json.dumps(["B-"+s[:-4]+"_USDT",4,NOW]).encode() for s in batch]
        self.client.evalsha.side_effect=read
        result=state.read_funding_many(names)
        self.assertEqual(len(result),505)
        self.assertEqual(self.client.evalsha.call_count,32)
        self.client.script_load.assert_called_once_with(READ_FUNDING)
        self.client.pipeline.assert_not_called()
        self.assertEqual(result["P0USDT"]["instrument_ms"],NOW)
        self.assertGreater(state.io_stats()['funding_read_bytes'],0)
        self.assertEqual(state.io_stats()['record_read_bytes'],0)

    def test_partial_missing_wrong_identity_and_size_fail_closed(self):
        self.assertIsNone(funding_projection({"schedule_retry_ms":{}}))
        value=record();value["instrument"]["settle_currency_short_name"]="USDC"
        self.assertIsNone(funding_projection(value))
        for body in (b"{}",b"[]",b"x"*1025,json.dumps(["B-ETH_USDT",4,NOW]).encode()):
            state=self.state();self.client.evalsha.return_value=[b"available",body]
            with self.assertRaises((ValueError,TypeError)):state.read_funding_many(["BTCUSDT"])
        state=self.state();self.client.evalsha.return_value=[b"available",None]
        self.assertEqual(state.read_funding_many(["BTCUSDT"]),{"BTCUSDT":None})
        for names in ([{}],["BTCUSDT"]*2,[str(i) for i in range(1025)]):
            with self.assertRaises(ValueError):state.read_funding_many(names)
