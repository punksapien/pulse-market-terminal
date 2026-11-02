from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from pulse.collectors import live_runtime as live
from pulse.collectors.binance_transport import AdmissionDenied
from pulse.store.expansion_guard import ExpansionStopped

ENV={"VERCEL_ENV":"preview","PULSE_RELEASE_TOP350":"1","PULSE_PREVIEW_DCX12":"1"}
NOW=1800000000000


class LiveRuntimeTests(TestCase):
    def rig(self):
        wall=[0.];client=Mock();pub=Mock();state=Mock()
        source=SimpleNamespace(dcx=Mock(),clock=lambda:NOW+int(wall[0]*1000),calls=0)
        universe=[dict(id=0,symbol="BTCUSDT",pair_id="B-BTC_USDT",funding_hours=8)]
        def bulk(s):s.calls+=1;return {"ts":s.clock(),"prices":{}}
        def metadata(s):s.calls+=2;return ({"data":{},"source_ms":s.clock()},
            {"data":{"BTCUSDT":{"nextFundingTime":NOW+28800000}},"source_ms":s.clock()})
        def price(*a,**kw):return SimpleNamespace(channel="live_price_v1")
        def funding(*a,**kw):return SimpleNamespace(channel="live_funding_v1")
        d=dict(connect=Mock(return_value=client),Sources=Mock(return_value=source),Publisher=Mock(return_value=pub),
            State=Mock(return_value=state),Document=Mock(return_value=SimpleNamespace(read=lambda **kw:{"revision":0})),
            load=Mock(return_value=universe),bulk=Mock(side_effect=bulk),metadata=Mock(side_effect=metadata),
            price=Mock(side_effect=price),funding=Mock(side_effect=funding),now=lambda:wall[0],
            sleep=lambda seconds:wall.__setitem__(0,wall[0]+seconds))
        return client,pub,state,source,wall,d

    def run_live(self,d,env=ENV):
        return live.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,env=env,deps=d)

    def test_fast_lane_keeps_publishing_without_metric_work_and_preserves_source_clocks(self):
        c,p,s,source,wall,d=self.rig();r=self.run_live(d)
        self.assertGreaterEqual(r["price_publishes"],20);self.assertGreaterEqual(r["funding_publishes"],10)
        self.assertLessEqual(r["funding_max_publish_gap_ms"],5000)
        self.assertEqual(d["Publisher"].call_args.kwargs,{"lane":"live"})
        s.read_many.assert_not_called();d["metadata"].assert_called_once()
        for call in d["funding"].call_args_list:
            self.assertLessEqual(call.args[3]["ts"],call.kwargs["now_ms"])
            self.assertLess(call.kwargs["now_ms"]-call.args[3]["ts"],2000)
        c.close.assert_called_once();p.release.assert_called_once()

    def test_binance_denial_or_bad_metadata_does_not_stop_healthy_price(self):
        for error in (ValueError("bad_metadata"),AdmissionDenied("budget_exhausted:1"),TimeoutError()):
            c,p,s,source,wall,d=self.rig();d["metadata"].side_effect=error
            r=self.run_live(d)
            self.assertGreaterEqual(r["price_publishes"],20);self.assertEqual(r["funding_publishes"],0)
            d["metadata"].assert_called_once()
            if isinstance(error,AdmissionDenied):self.assertEqual(r["funding_stopped"],"admission:budget_exhausted:1")

    def test_denied_dcx_is_not_retried_and_fencing_is_never_hidden(self):
        c,p,s,source,wall,d=self.rig();error=AdmissionDenied("circuit_open");error.denied_source="coindcx"
        d["bulk"].side_effect=error;r=self.run_live(d)
        self.assertEqual(r["stopped"],"admission:circuit_open");d["bulk"].assert_called_once();p.publish.assert_not_called()
        c,p,s,source,wall,d=self.rig();p.publish.side_effect=RuntimeError("publication_fenced_out")
        with self.assertRaisesRegex(RuntimeError,"fenced_out"):self.run_live(d)
        p.release.assert_called_once();c.close.assert_called_once()

    def test_production_or_missing_policy_refuses_before_connect(self):
        for env in ({**ENV,"VERCEL_ENV":"production"},{**ENV,"PULSE_PREVIEW_DCX12":"0"}):
            c,p,s,source,wall,d=self.rig()
            with self.assertRaisesRegex(ValueError,"preview_release"):self.run_live(d,env)
            d["connect"].assert_not_called()

    def test_explicit_production_uses_same_lane_and_existing_quota_scope(self):
        env=dict(VERCEL_ENV="production",PULSE_RELEASE_TOP350="1",PULSE_PRODUCTION_ENABLED="1",
                 PULSE_UNIVERSE_MODE="all",PULSE_PRODUCTION_DCX12_APPROVED="1")
        c,p,s,source,wall,d=self.rig();r=self.run_live(d,env)
        self.assertGreater(r["price_publishes"],0)
        self.assertEqual(d["Sources"].call_args.kwargs["scope"],"vercel-bom1-all-v1")
        self.assertTrue(d["Sources"].call_args.kwargs["preview_dcx12"])

    def test_temporary_headroom_waits_without_exchange_dispatch_then_recovers(self):
        c,p,s,source,wall,d=self.rig();dispatch=[]
        original=d['bulk'].side_effect
        d['bulk'].side_effect=lambda src:(dispatch.append(wall[0]),original(src))[1]
        p.publish.side_effect=[ExpansionStopped('staging_memory_gate_headroom_denied',{},additional_bytes=1234)]+[None]*80
        def preflight(additional):
            self.assertEqual(additional,1234)
            if wall[0]<5:raise ExpansionStopped('staging_memory_gate_headroom_denied',{},additional_bytes=additional)
        p.preflight.side_effect=preflight
        result=self.run_live(d)
        self.assertEqual(dispatch[:2],[0,5]);self.assertEqual(result['memory_pauses'],5)
        self.assertEqual(result['memory_recovery'],'resumed')
        self.assertGreaterEqual(result['price_publishes'],20)
        self.assertEqual(d['metadata'].call_count,1)
        c.close.assert_called_once();p.release.assert_called_once()

    def test_hard_stop_or_lost_fence_is_not_hidden_by_memory_recovery(self):
        for reason in ('staging_memory_gate_stop','staging_memory_gate_unknown_usage','staging_eviction_policy_refused'):
            c,p,s,source,wall,d=self.rig()
            p.publish.side_effect=ExpansionStopped(reason,{})
            with self.assertRaisesRegex(ExpansionStopped,reason):self.run_live(d)
            self.assertEqual(d['bulk'].call_count,1);p.release.assert_called_once()
        c,p,s,source,wall,d=self.rig()
        p.publish.side_effect=ExpansionStopped('staging_memory_gate_headroom_denied',{},additional_bytes=1)
        p.renew.side_effect=[None,None,RuntimeError('publication_fenced_out')]
        with self.assertRaisesRegex(RuntimeError,'fenced_out'):self.run_live(d)
        self.assertEqual(d['bulk'].call_count,1);p.release.assert_called_once()

    def test_permanent_headroom_wait_stays_bounded_and_does_not_fabricate_publication(self):
        c,p,s,source,wall,d=self.rig()
        denied=ExpansionStopped('staging_memory_gate_headroom_denied',{},additional_bytes=1)
        p.publish.side_effect=denied;p.preflight.side_effect=denied
        result=self.run_live(d)
        self.assertEqual(result['status'],'incomplete');self.assertEqual(result['price_publishes'],0)
        self.assertEqual(d['bulk'].call_count,1);self.assertLessEqual(wall[0],55)
        self.assertEqual(result['memory_recovery'],'waiting');p.release.assert_called_once()

    def test_funding_pressure_recovery_fetches_fresh_input_without_past_due_spin(self):
        c,p,s,source,wall,d=self.rig();dispatch=[];renewed=[];original=d['bulk'].side_effect
        p.renew.side_effect=lambda:renewed.append(wall[0])
        d['bulk'].side_effect=lambda src:(dispatch.append(wall[0]),original(src))[1]
        denied=[False]
        def publish(packages,**kw):
            if packages[0].channel=='live_funding_v1' and not denied[0]:
                denied[0]=True
                raise ExpansionStopped('staging_memory_gate_headroom_denied',{},additional_bytes=1)
        p.publish.side_effect=publish
        result=self.run_live(d)
        self.assertEqual(dispatch[:2],[0,1],'recovery must fetch before retrying funding, not spin on its overdue timer')
        self.assertEqual(result['memory_recovery'],'resumed');self.assertGreater(result['funding_publishes'],0)
        self.assertLessEqual(sum(1<=t<2 for t in renewed),3,'past-due funding must not create a lease-renewal hot loop')
