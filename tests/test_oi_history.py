import unittest
from dataclasses import replace
from pulse.collectors.oi_history import history_spec, parse_history, build_history_ring, cold_start_changes
from pulse.metrics.open_interest import OIObservation, WINDOW_MS


class OIHistoryTests(unittest.TestCase):
    now=1793000000000

    def setUp(self):
        self.spec=history_spec("B-1000PEPE_USDT","1000PEPEUSDT")

    def row(self,stamp,quantity="100"):
        return {"symbol":"1000PEPEUSDT","timestamp":stamp,"sumOpenInterest":quantity,
                "sumOpenInterestValue":"999999999"}

    def test_three_windows_recompute_from_native_quantities(self):
        current=OIObservation(self.now,110.,self.spec.pair_id)
        rows=[self.row(self.now-window) for window in WINDOW_MS.values()]
        points=parse_history(list(reversed(rows)),self.spec,now_ms=self.now)
        ring=build_history_ring(points,self.spec,now_ms=self.now)
        for result in cold_start_changes(current,ring,self.spec,now_ms=self.now).values():
            self.assertEqual(result.status,"available")
            self.assertEqual(result.value_fraction,.1)
            self.assertEqual(result.baseline_error_ms,0)
            self.assertEqual(result.mode,"cold-start")

    def test_inclusive_tolerance_missing_zero_and_conflict(self):
        current=OIObservation(self.now,110.,self.spec.pair_id)
        for error,expected in ((300000,"available"),(300001,"unavailable")):
            rows=[self.row(self.now-3600000-error)]
            ring=build_history_ring(parse_history(rows,self.spec,now_ms=self.now),self.spec,now_ms=self.now)
            result=cold_start_changes(current,ring,self.spec,now_ms=self.now)
            self.assertEqual(result["1H"].status,expected)
            self.assertEqual(result["24H"].reason,"baseline_missing")
        rows=[self.row(self.now-3600000,"0")]
        ring=build_history_ring(parse_history(rows,self.spec,now_ms=self.now),self.spec,now_ms=self.now)
        self.assertEqual(cold_start_changes(current,ring,self.spec,now_ms=self.now)["1H"].reason,"zero_baseline")
        with self.assertRaisesRegex(ValueError,"conflict"):
            parse_history([self.row(self.now),self.row(self.now,"101")],self.spec,now_ms=self.now)

    def test_identity_precision_and_timestamps(self):
        stamp=self.now-3600000
        rows=[self.row(str(stamp),"100.00"),self.row(stamp,"100")]
        self.assertEqual(len(parse_history(rows,self.spec,now_ms=self.now)),1)
        for row in (dict(self.row(stamp),symbol="BTCUSDT"),self.row(True),self.row(str(stamp)+".0"),
                    self.row(self.now+5001),self.row(stamp,"-1"),self.row(stamp,"0.1234567890123456789")):
            with self.assertRaises(ValueError):
                parse_history([row],self.spec,now_ms=self.now)
        for rows in ({},[self.row(stamp)]*501):
            with self.assertRaises(ValueError):
                parse_history(rows,self.spec,now_ms=self.now)
        self.assertEqual(parse_history([self.row(self.now-100000000)],self.spec,now_ms=self.now),())

    def test_discarded_candidate_does_not_return_wrong_older_baseline(self):
        target=(self.now//300000)*300000+100
        current=OIObservation(target+3600000,110.,self.spec.pair_id)
        rows=[self.row(target-300000),self.row(target-50),self.row(target+50)]
        points=parse_history(rows,self.spec,now_ms=current.source_time_ms)
        ring=build_history_ring(points,self.spec,now_ms=current.source_time_ms)
        result=cold_start_changes(current,ring,self.spec,now_ms=current.source_time_ms)
        self.assertEqual(result["1H"].status,"unavailable")
        self.assertEqual(result["1H"].reason,"discarded_baseline_candidate")
        with self.assertRaises(ValueError):
            cold_start_changes(replace(current,pair_id="B-BTC_USDT"),ring,self.spec,now_ms=self.now)
