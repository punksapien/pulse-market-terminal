import unittest
from unittest.mock import patch
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pulse.collectors import preview_sources as ps


class Client:
    def __init__(self):
        self.now = 1_800_000_030_000   # 30 s into a provider minute

    def time(self):
        return divmod(self.now, 1000) if False else (self.now // 1000, (self.now % 1000) * 1000)


class Adm:
    last_feedback = {"provider_date_ms": 1}
    def reserve(self,costs):return [b"reserved"]


def make(client):
    src = ps.Sources.__new__(ps.Sources)
    src.client, src.deadline, src.ca_file = client, time.monotonic() + 100, None
    src.dcx = src.binance = Adm()
    src.last = 0
    src.calls = 0
    src.last_source_ms = None
    src.header_at = src.header_minute = None
    src.check_lease = lambda: None
    return src


class HeaderTests(unittest.TestCase):
    def test_only_optional_all_pair_history_uses_reserved_metadata_headroom(self):
        from unittest.mock import Mock
        for all_mode,path,weight,family,expected in (
            (False,'/futures/data/openInterestHist',0,False,{}),
            (True,'/futures/data/openInterestHist',0,False,{'history_headroom':32}),
            (True,'/fapi/v1/fundingRate',0,True,{'history_headroom':32}),
            (True,'/fapi/v1/fundingInfo',0,True,{}),
            (True,'/fapi/v1/openInterest',1,False,{})):
            src=make(Client());src.all_policy=all_mode;src.policy_ready=True;src.pacer=ps.DispatchPacer()
            src.binance.reserve=Mock(return_value=[b'reserved'])
            def fetch(path,params,admission,costs,ca,**kw):
                self.assertEqual(kw['reserve'](costs),[b'reserved']);return {}
            with self.subTest(path=path,all_mode=all_mode),patch.object(ps,'fetch',fetch),patch.object(src,'_ensure_header'):
                src.request(path,{},weight=weight,family=family)
            self.assertEqual(src.binance.reserve.call_args.kwargs,expected)

    def test_header_is_rechecked_after_the_paced_queue_crosses_a_minute(self):
        client=Client();src=make(client);src.pacer=ps.DispatchPacer()
        src.pacer.header_at=client.now;src.pacer.header_minute=client.now//60000
        waits=[];seen=[]
        def wait(*args):
            waits.append(1)
            if len(waits)==1:client.now+=31000
        def reserve(costs):
            if costs['weight']==0:self.assertEqual(src.pacer.header_minute,client.now//60000)
            return [b'reserved']
        src.binance.reserve=reserve
        def fetch(path,params,admission,costs,ca,**kwargs):
            decision=kwargs['reserve'](costs);self.assertEqual(decision,[b'reserved'])
            seen.append((path,costs['weight']));admission.last_feedback={'provider_date_ms':client.now}
            return {}
        with patch.object(ps,'fetch',fetch),patch.object(src.pacer,'wait',side_effect=wait):
            src.request('/history',{})
        self.assertEqual(seen,[('/fapi/v1/openInterest',1),('/history',0)])
        self.assertEqual(src.calls,2)

    def shared_source(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        client=Client();client.hmget=Mock()
        src=make(client);src.pacer=ps.DispatchPacer()
        src.binance=SimpleNamespace(keys=['control'],mapping='a'*64,header_state=lambda:client.hmget('control',[]))
        values=[b'a'*64,b'R',b'1',str(client.now).encode(),str(client.now//60000).encode(),b'10',b'0']
        client.hmget.return_value=values
        return src,client,values

    def test_shared_reconciled_header_avoids_a_redundant_exchange_probe(self):
        src,client,values=self.shared_source()
        with patch.object(ps,'fetch') as transport:src._ensure_header()
        transport.assert_not_called();self.assertEqual(src.pacer.header_at,client.now)
        self.assertEqual(src.header_minute,client.now//60000)

    def test_unknown_stale_future_wrong_policy_or_floor_is_never_reused(self):
        for index,value in ((0,b'b'*64),(1,b'B'),(2,b'0'),(3,b'0'),(3,b'99999999999999999999'),
                (4,b'0'),(5,b'-1'),(3,b'bad')):
            src,client,values=self.shared_source();values[index]=value
            with self.subTest(index=index,value=value):self.assertFalse(src._shared_header())

    def test_inflight_shared_probe_waits_without_charging_and_has_a_deadline(self):
        src,client,fresh=self.shared_source();old=list(fresh)
        old[3]=str(client.now-60000).encode();old[4]=str(client.now//60000-1).encode();old[6]=str(client.now+30000).encode()
        client.hmget.side_effect=[old,fresh]
        with patch.object(ps.time,'sleep') as sleep:self.assertTrue(src._shared_header())
        sleep.assert_called_once_with(.05)
        src,client,fresh=self.shared_source();old=list(fresh)
        old[3]=b'0';old[6]=str(client.now+30000).encode();client.hmget.return_value=old
        src.deadline=time.monotonic()+1
        with patch.object(ps.time,'sleep') as sleep:self.assertFalse(src._shared_header())
        sleep.assert_not_called();self.assertEqual(client.hmget.call_count,1)

    def test_idle_pruning_is_cooperative_and_not_counted_as_http(self):
        src=make(Client());attempts=[]
        def fetch(*args,**kwargs):
            attempts.append(1)
            if len(attempts)<=12:
                exc=ps.AdmissionDenied("maintenance");exc.admission_reason="budget_pruned_retry";raise exc
            return {}
        with patch.object(ps,"fetch",fetch):src.request("/fixture",{},host="https://public.coindcx.com")
        self.assertEqual(len(attempts),13)
        self.assertEqual(src.calls,1)
        self.assertEqual(src.prune_steps,12)

    def test_pruning_has_a_hard_step_limit(self):
        src=make(Client());exc=ps.AdmissionDenied("maintenance");exc.admission_reason="budget_pruned_retry"
        with patch.object(ps.time,"monotonic",return_value=10),patch.object(ps,"fetch",side_effect=exc) as fetch:
            with self.assertRaises(ps.AdmissionDenied):src.request("/fixture",{},host="https://public.coindcx.com")
        self.assertEqual(fetch.call_count,129)
        self.assertEqual(src.calls,0)

    def test_pruning_checks_fence_and_wall_budget(self):
        src=make(Client());exc=ps.AdmissionDenied("maintenance");exc.admission_reason="budget_pruned_retry"
        with patch.object(ps,"fetch",side_effect=exc) as fetch,patch.object(src,"check_lease",side_effect=[None,None,None,RuntimeError("fence_lost")]):
            with self.assertRaisesRegex(RuntimeError,"fence_lost"):src.request("/fixture",{},host="https://public.coindcx.com")
        self.assertEqual(fetch.call_count,1)
        src=make(Client());clock=[10.0]
        def prune(*args,**kwargs):clock[0]+=4;raise exc
        with patch.object(ps.time,"monotonic",side_effect=lambda:clock[0]),patch.object(ps,"fetch",side_effect=prune) as fetch:
            with self.assertRaises(ps.AdmissionDenied):src.request("/fixture",{},host="https://public.coindcx.com")
        self.assertEqual(fetch.call_count,1)

    def run_requests(self, steps):
        client = Client()
        src = make(client)
        seen = []

        def fake_fetch(path, params, admission, costs, ca, **kw):
            seen.append((path, costs["weight"], client.now))
            admission.last_feedback={"provider_date_ms":client.now}
            return {}
        with patch.object(ps, "fetch", fake_fetch), patch.object(ps.time, "sleep", lambda s: setattr(client, "now", client.now + int(s * 1000))):
            for advance, kind in steps:
                client.now += advance
                if kind == "history":
                    src.request("/futures/data/openInterestHist", {"symbol": "X"})
                else:
                    src.request("/fapi/v1/openInterest", {"symbol": "X"}, weight=1)
        return seen

    def test_history_call_without_a_header_first_sends_one_heartbeat(self):
        seen = self.run_requests([(0, "history")])
        self.assertEqual([(p, w) for p, w, _ in seen], [("/fapi/v1/openInterest", 1), ("/futures/data/openInterestHist", 0)])

    def test_same_minute_reuses_the_header(self):
        seen = self.run_requests([(0, "history"), (5000, "history"), (5000, "history")])
        self.assertEqual(sum(1 for p, w, _ in seen if w == 1), 1)

    def test_new_provider_minute_refreshes_before_the_call(self):
        seen = self.run_requests([(0, "history"), (40000, "history")])
        self.assertEqual(sum(1 for p, w, _ in seen if w == 1), 2)

    def test_a_weighted_call_counts_as_the_header(self):
        seen = self.run_requests([(0, "weighted"), (2000, "history")])
        self.assertEqual([w for _, w, _ in seen], [1, 1, 0])

    def test_never_straddles_the_minute_boundary(self):
        client = Client()
        client.now = 1_800_000_059_500   # 0.5 s before the boundary
        src = make(client)
        times = []
        def fetch(*args,**kwargs):
            src.binance.last_feedback={"provider_date_ms":client.now}
            times.append(client.now)
            return {}
        with patch.object(ps, "fetch", fetch), \
             patch.object(ps.time, "sleep", lambda s: setattr(client, "now", client.now + int(s * 1000))):
            src.request("/futures/data/openInterestHist", {"symbol": "X"})
        self.assertTrue(all(t % 60000 < 59000 for t in times), times)

    def test_deadline_refuses_before_transport_and_live_timeout_is_bounded(self):
        client=Client();src=make(client)
        with patch.object(ps.time,"monotonic",return_value=10),patch.object(ps,"fetch") as transport:
            src.deadline=11
            with self.assertRaisesRegex(RuntimeError,"worker_source_budget_exhausted"):
                src.request("/fixture",{},host="https://public.coindcx.com")
            transport.assert_not_called()
            src.live=True;src.live_deadline=15
            transport.return_value={}
            src.request("/fixture",{},host="https://public.coindcx.com")
            self.assertEqual(transport.call_args.kwargs["timeout"],3)

    def test_coindcx_hosts_share_one_pacing_bucket(self):
        clock=[10.0];pacer=ps.DispatchPacer();waits=[]
        def sleep(seconds):waits.append(seconds);clock[0]+=seconds
        with patch.object(ps.time,"monotonic",side_effect=lambda:clock[0]),patch.object(ps.time,"sleep",sleep):
            pacer.wait("https://api.coindcx.com",100)
            pacer.wait("https://public.coindcx.com",100)
        self.assertAlmostEqual(sum(waits),.15)

    def test_tiny_remaining_http_budget_stops_before_charge_or_dispatch(self):
        src=make(Client());src.deadline=13.999
        with patch.object(ps.time,"monotonic",return_value=10),patch.object(ps,"fetch") as fetch:
            with self.assertRaisesRegex(RuntimeError,"worker_source_budget_exhausted"):
                src.request("/fixture",{},host="https://public.coindcx.com")
        fetch.assert_not_called();self.assertEqual(src.calls,0)

    def test_parallel_sources_share_one_header_probe_in_each_minute(self):
        client=Client();pacer=ps.DispatchPacer()
        sources=[make(client) for _ in range(8)]
        for src in sources:
            src.pacer=pacer
            src.binance=Adm()
        seen=[];lock=threading.Lock()
        def fetch(path,params,admission,costs,ca,**kwargs):
            kwargs['reserve'](costs)
            if costs["weight"]:
                with lock: seen.append(client.now)
                time.sleep(.01)
            admission.last_feedback={"provider_date_ms":client.now}
            return {}
        with patch.object(ps,"fetch",fetch),patch.object(pacer,"wait"):
            for _ in range(2):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda src:src.request("/history",{}),sources))
                client.now+=60000
        self.assertEqual(len(seen),2)
        self.assertEqual(sum(src.calls for src in sources),18)

    def test_failed_shared_probe_is_not_promoted_or_retried(self):
        src=make(Client());src.pacer=ps.DispatchPacer()
        denied=ps.AdmissionDenied("denied")
        denied.admission_reason="circuit_open"
        with patch.object(src.pacer,"wait"),patch.object(ps,"fetch",side_effect=denied) as fetch:
            with self.assertRaises(ps.AdmissionDenied): src.request("/history",{})
        self.assertEqual(fetch.call_count,1)
        self.assertIsNone(src.pacer.header_at)

    def test_fresh_header_does_not_serialize_ordinary_weighted_http(self):
        client=Client();pacer=ps.DispatchPacer()
        pacer.header_at=client.now;pacer.header_minute=client.now//60000
        sources=[make(client) for _ in range(8)]
        for src in sources: src.pacer=pacer;src.binance=Adm()
        barrier=threading.Barrier(8)
        def fetch(path,params,admission,*args,**kwargs):
            kwargs['reserve'](args[0])
            barrier.wait(timeout=2)
            admission.last_feedback={"provider_date_ms":client.now}
            return {}
        with patch.object(ps,"fetch",fetch),patch.object(pacer,"wait"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda src:src.request("/fapi/v1/openInterest",{"symbol":"ETHUSDT"},weight=1),sources))
        self.assertEqual(sum(src.calls for src in sources),8)


if __name__ == "__main__":
    unittest.main()
