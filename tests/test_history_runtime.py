from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from pulse.collectors import history_runtime as h

ENV={"VERCEL_ENV":"preview","PULSE_RELEASE_TOP350":"1"}


class HistoryRuntimeTests(TestCase):
    def rig(self,index=32):
        client=Mock();pub=Mock();state=Mock();state.read.return_value={"index":index}
        state.read_many.side_effect=lambda names:{n:{} for n in names}
        universe=[dict(id=i,symbol="P"+str(i),pair_id="B-P"+str(i)+"_USDT",funding_hours=4) for i in range(350)]
        def prepare(c,run,batch,records,p,**kw):
            return [SimpleNamespace(channel="history:"+str(batch[0]["id"]//10),allocation_bytes=100)]
        deps=dict(connect=Mock(return_value=client),Publisher=Mock(return_value=pub),State=Mock(return_value=state),
            load=Mock(return_value=universe),prepare=Mock(side_effect=prepare),clock=lambda c:1800000000000,
            now=lambda:0,Document=Mock(return_value=SimpleNamespace(read=lambda **kw:{"revision":0})))
        return client,pub,state,deps

    def run_history(self,deps,env=ENV):
        return h.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,env=env,deps=deps)

    def test_bounded_rotation_wraps_and_reads_only_forty_rows(self):
        c,p,s,d=self.rig();r=self.run_history(d)
        self.assertEqual(r["history_buckets"],[32,33,34,0]);self.assertEqual(r["source_dispatches"],0)
        self.assertEqual(p.publish.call_count,4);self.assertEqual(d["Publisher"].call_args.kwargs,{"lane":"history"})
        self.assertEqual(sum(len(call.args[0]) for call in s.read_many.call_args_list),40)
        self.assertEqual(s.write.call_args.args,("HISTORY_CURSOR",{"index":1}))
        for call in d["prepare"].call_args_list:
            self.assertEqual(call.kwargs["retention_ms"],720000);self.assertFalse(call.kwargs["include_identity"])
        c.close.assert_called_once();p.release.assert_called_once()

    def test_failed_promotion_does_not_advance_failed_bucket_cursor(self):
        c,p,s,d=self.rig();p.publish.side_effect=[None,RuntimeError("publication_fenced_out")]
        with self.assertRaisesRegex(RuntimeError,"fenced_out"):self.run_history(d)
        s.write.assert_called_once_with("HISTORY_CURSOR",{"index":33})
        c.close.assert_called_once();p.release.assert_called_once()

    def test_unknown_cursor_refuses_without_writes_and_production_never_connects(self):
        for index in (35,-1,True,"0"):
            c,p,s,d=self.rig(index)
            with self.assertRaisesRegex(ValueError,"cursor"):self.run_history(d)
            p.publish.assert_not_called();s.write.assert_not_called()
        for env in ({"VERCEL_ENV":"production","PULSE_RELEASE_TOP350":"1"},{"VERCEL_ENV":"preview"}):
            c,p,s,d=self.rig()
            with self.assertRaisesRegex(ValueError,"preview_release"):self.run_history(d,env)
            d["connect"].assert_not_called()

    def test_busy_history_owner_does_not_read_or_publish(self):
        c,p,s,d=self.rig();p.acquire.side_effect=RuntimeError("publication_lease_busy")
        self.assertEqual(self.run_history(d),{"status":"lease_busy"})
        d["load"].assert_not_called();p.publish.assert_not_called();c.close.assert_called_once()
