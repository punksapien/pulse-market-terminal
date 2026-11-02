from concurrent.futures import ThreadPoolExecutor
import threading
from unittest import TestCase
from unittest.mock import Mock,patch

from pulse.collectors.preview_sources import DispatchPacer
from pulse.collectors import binance_transport as transport


class ReservationPacingTests(TestCase):
    def test_parallel_reservations_are_spaced_after_slow_preparation(self):
        clock=[10.0];pacer=DispatchPacer();stamps=[];barrier=threading.Barrier(8)
        def callback(costs):
            stamps.append(clock[0]);clock[0]+=.04
            return [b"reserved"]
        def work(i):
            barrier.wait(timeout=2)
            return pacer.reserve("https://public.coindcx.com" if i%2 else
                "https://api.coindcx.com",100,callback,{"requests":1})
        with patch("pulse.collectors.preview_sources.time.monotonic",side_effect=lambda:clock[0]), \
             patch("pulse.collectors.preview_sources.time.sleep",side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)):
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertTrue(all(r==[b"reserved"] for r in pool.map(work,range(8))))
        self.assertTrue(all(b-a>=.189999 for a,b in zip(stamps,stamps[1:])))

    def test_deadline_does_not_charge_and_errors_release_reservation_lock(self):
        pacer=DispatchPacer();callback=Mock(return_value=[b"reserved"])
        with patch("pulse.collectors.preview_sources.time.monotonic",return_value=10):
            with self.assertRaisesRegex(RuntimeError,"worker_source_budget_exhausted"):
                pacer.reserve(transport.HOST,11,callback,{})
            callback.assert_not_called()
            with self.assertRaisesRegex(ValueError,"fixture"):
                pacer.reserve(transport.HOST,100,Mock(side_effect=ValueError("fixture")),{})
        for lock in pacer.locks.values():
            self.assertTrue(lock.acquire(blocking=False));lock.release()

    def test_coindcx_sleep_never_blocks_binance_permit(self):
        pacer=DispatchPacer();sleeping=threading.Event();release=threading.Event();charged=threading.Event()
        pacer.last["coindcx"]=10
        def sleep(delay):
            sleeping.set();self.assertTrue(release.wait(2))
        with patch("pulse.collectors.preview_sources.time.monotonic",return_value=10), \
             patch("pulse.collectors.preview_sources.time.sleep",side_effect=sleep):
            with ThreadPoolExecutor(max_workers=2) as pool:
                dcx=pool.submit(pacer.reserve,"https://api.coindcx.com",100,lambda c:True,{})
                self.assertTrue(sleeping.wait(2))
                bina=pool.submit(pacer.reserve,transport.HOST,100,lambda c:charged.set(),{})
                try:self.assertTrue(charged.wait(1),"independent provider must not wait for CoinDCX sleep")
                finally:release.set()
                dcx.result();bina.result()

    def test_transport_reserves_after_tls_setup_and_recomputes_timeout(self):
        clock=[10.0];events=[];admission=Mock()
        response=Mock();response.status=200;response.headers={};response.read.return_value=b"{}"
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=context
        def reserve(costs):
            events.append("reserve");clock[0]=14
            return [b"reserved"]
        def build(*args):events.append("tls_ready");return opener
        with patch.object(transport,"build_opener",side_effect=build), \
             patch.object(transport.time,"monotonic",side_effect=lambda:clock[0]):
            self.assertEqual(transport.fetch("/fixture",{},admission,{"history":1},None,
                reserve=reserve,deadline=15,timeout=8),{})
        self.assertEqual(events,["tls_ready","reserve"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"],1)
        admission.reserve.assert_not_called()

    def test_expired_after_permit_does_not_send_http_or_refund(self):
        admission=Mock();opener=Mock()
        with patch.object(transport,"build_opener",return_value=opener), \
             patch.object(transport.time,"monotonic",return_value=20):
            with self.assertRaisesRegex(RuntimeError,"worker_source_budget_exhausted"):
                transport.fetch("/fixture",{},admission,{"history":1},None,
                    reserve=lambda costs:[b"reserved"],deadline=15)
        opener.open.assert_not_called();admission.feedback.assert_called_once_with(0,{})
