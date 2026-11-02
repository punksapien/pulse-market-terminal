import importlib.util
from pathlib import Path
import threading
import unittest

spec = importlib.util.spec_from_file_location("preview_driver", Path(__file__).parents[1] / "tools/probes/pulse_preview_drive.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class DriverTests(unittest.TestCase):
    def test_stream_refusal_does_not_stop_rest_worker_or_retry_upstream(self):
        stop=threading.Event();calls=[]
        def refused(*args):calls.append(1);return 503,{"reason":"upstream_refused"}
        result=driver.run_lane(driver.PATHS[1],180,refused,stop,now=lambda:0,emit=lambda value:None)
        self.assertEqual(result["failures"],1);self.assertEqual(len(calls),1)
        self.assertFalse(stop.is_set())
    def test_maintenance_restart_is_bounded_and_other_denials_stop(self):
        clock=[0.0];stop=threading.Event();calls=[]
        def post(*args):
            calls.append(1)
            return 200,{"status":"incomplete","stopped":"admission:budget_pruned_retry"}
        result=driver.run_lane(driver.PATHS[0],180,post,stop,now=lambda:clock[0],
            wait=lambda delay:clock.__setitem__(0,clock[0]+delay),emit=lambda value:None)
        self.assertEqual(len(calls),4)
        self.assertEqual(result["failures"],1)
        self.assertTrue(stop.is_set())

    def run_lane(self, responses):
        clock = [0.0]
        waits, calls, logs = [], [], []
        stop = threading.Event()
        def post(path, timeout):
            calls.append((path, timeout))
            clock[0] += 55
            return responses.pop(0)
        def wait(seconds):
            waits.append(seconds)
            clock[0] += seconds
        result = driver.run_lane(driver.PATHS[0], 180, post, stop, now=lambda: clock[0], wait=wait, emit=logs.append)
        return result, waits, calls, logs, stop

    def test_busy_waits_then_retries_sequentially_and_stops_before_deadline(self):
        result, waits, calls, logs, stop = self.run_lane([(409, {"status": "lease_busy", "retry_after_ms": 5000}),
                                                       (200, {"status": "published"})])
        self.assertEqual(result["calls"], 2)
        self.assertEqual(waits, [5, .25])
        self.assertFalse(stop.is_set())

    def test_failure_stops_both_lanes_instead_of_hammering(self):
        result, waits, calls, logs, stop = self.run_lane([(503, {"reason": "worker_run_failed", "detail": "publication_promotion_refused:invalid_time"})])
        self.assertTrue(stop.is_set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(logs[0]["detail"], "publication_promotion_refused:invalid_time")

    def test_logs_never_copy_arbitrary_text_or_credentials(self):
        self.assertEqual(driver.summary('/api/pulse_worker', 503, {"detail": "redis://secret@example.invalid", "secret": "password"}),
                         {"path": "/api/pulse_worker", "http": 503})
        value=driver.summary('/api/pulse_worker',200,{'failed':{'BTCUSDT':'worker_pair_invalid','ETHUSDT':'redis://secret@example.invalid'}})
        self.assertEqual(value['failed_count'],2)
        self.assertEqual(value['failure_reasons'],{'worker_pair_invalid':1,'redacted':1})
        self.assertNotIn('secret',str(value))

    def test_partial_publication_with_admission_stop_stops_driver(self):
        result,waits,calls,logs,stop=self.run_lane([(200,{"status":"published","stopped":"admission:header_probe_busy"})])
        self.assertTrue(stop.is_set())
        self.assertEqual(result["failures"],1)
        self.assertEqual(len(calls),1)

    def test_transport_stop_is_not_reported_as_a_healthy_publish(self):
        result,waits,calls,logs,stop=self.run_lane([(200,{"status":"published","stopped":"source_transport_failed","transport_error_type":"TimeoutError"})])
        self.assertTrue(stop.is_set())
        self.assertEqual(result["failures"],1)
        self.assertEqual(logs[0]["transport_error_type"],"TimeoutError")

    def test_no_call_if_it_cannot_finish_inside_window(self):
        post = lambda *args: self.fail("must not start")
        self.assertEqual(driver.run_lane(driver.PATHS[1], 74, post, threading.Event(), now=lambda: 0)["calls"], 0)
