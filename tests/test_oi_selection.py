from dataclasses import replace
import unittest
from pulse.metrics.oi_selection import change_with_baselines
from pulse.metrics.open_interest import OIObservation,WINDOW_MS


class OISelectionTests(unittest.TestCase):
    def test_table_all_windows(self):
        now=1793001600000
        current=OIObservation(now,110.,"B-BTC_USDT")
        # own error, cold error, own quantity, expected mode, expected reason
        cases=((0,0,100.,"own",None),(60000,0,100.,"own",None),
               (60001,0,100.,"cold-start",None),(-1,300000,100.,"cold-start",None),
               (None,300000,100.,"cold-start",None),(None,300001,100.,"cold-start","baseline_outside_tolerance"),
               (None,None,100.,"cold-start","baseline_missing"),(0,0,0.,"own","zero_baseline"))
        for horizon,window in WINDOW_MS.items():
            for own_error,cold_error,quantity,mode,reason in cases:
                with self.subTest(horizon=horizon,own=own_error,cold=cold_error,quantity=quantity):
                    own=None if own_error is None else OIObservation(now-window-own_error,quantity,current.pair_id)
                    cold=None if cold_error is None else OIObservation(now-window-cold_error,100.,current.pair_id)
                    result=change_with_baselines(current,own,cold,horizon=horizon,now_ms=now)
                    self.assertEqual((result.mode,result.reason),(mode,reason))
                    if reason is None:
                        self.assertEqual(result.value_fraction,.1)
                        self.assertEqual(result.baseline_error_ms,own_error if mode=="own" else cold_error)
                        self.assertIsNotNone(result.actual_duration_ms)
        own=OIObservation(now-3600000,100.,"wrong-pair")
        cold=replace(own,pair_id=current.pair_id)
        self.assertEqual(change_with_baselines(current,own,cold,horizon="1H",now_ms=now).reason,"pair_mismatch")
