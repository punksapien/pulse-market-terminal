import threading
import time
import unittest

from pulse.collectors.preview_batch import collect_batch
from pulse.collectors.binance_transport import AdmissionDenied


class Source:
    calls=0
    def clock(self): return 1800000000000


class BatchTests(unittest.TestCase):
    def test_fifty_pairs_have_one_writer_each_and_at_most_eight_workers(self):
        order=[{"symbol":str(i)} for i in range(50)]
        counts={};active=[0,0];lock=threading.Lock()
        def collect(meta,source,prior):
            with lock:
                active[0]+=1;active[1]=max(active)
                counts[meta["symbol"]]=counts.get(meta["symbol"],0)+1
            time.sleep(.005)
            with lock: active[0]-=1
            source.calls+=1
            return {"last_collected_ms":source.clock()}
        result=collect_batch(order,{str(i):None for i in range(50)},collect=collect,make_source=Source,check_lease=lambda:None)
        self.assertEqual(result["collected"],50)
        self.assertTrue(1<active[1]<=8)
        self.assertTrue(all(n==1 for n in counts.values()))
        self.assertEqual(result["source_dispatches"],50)

    def test_denial_stops_queued_work_without_retry(self):
        calls=[]
        def collect(meta,source,prior):
            calls.append(meta["symbol"])
            raise AdmissionDenied("budget_exhausted:1")
        result=collect_batch([{"symbol":str(i)} for i in range(50)],{str(i):None for i in range(50)},
            collect=collect,make_source=Source,check_lease=lambda:None,workers=1)
        self.assertEqual(calls,["0"])
        self.assertEqual(result["stopped"],"admission:budget_exhausted:1")

    def test_one_bad_pair_does_not_block_valid_pairs_and_fencing_propagates(self):
        order=[{"symbol":"a"},{"symbol":"b"}]
        def collect(meta,*args):
            if meta["symbol"]=="a": raise ValueError("malformed")
            return {"valid":True}
        result=collect_batch(order,{"a":None,"b":None},collect=collect,make_source=Source,check_lease=lambda:None)
        self.assertEqual(result["collected"],1)
        self.assertEqual(result["failed"],{"a":"worker_pair_invalid"})
        with self.assertRaisesRegex(RuntimeError,"publication_fenced_out"):
            collect_batch(order,{"a":None,"b":None},collect=collect,make_source=Source,
                check_lease=lambda:(_ for _ in ()).throw(RuntimeError("publication_fenced_out")))

    def test_duplicate_pair_is_refused_before_work(self):
        with self.assertRaisesRegex(ValueError,"worker_batch_bound"):
            collect_batch([{"symbol":"a"}]*2,{},collect=None,make_source=None,check_lease=None)

    def test_transport_failure_is_stopped_and_typed_without_exception_text(self):
        def collect(*args): raise TimeoutError("credential/provider text must not escape")
        result=collect_batch([{"symbol":"a"}],{"a":None},collect=collect,make_source=Source,check_lease=lambda:None)
        self.assertEqual(result["stopped"],"source_transport_failed")
        self.assertEqual(result["transport_error_type"],"TimeoutError")
        self.assertNotIn("credential",str(result))
