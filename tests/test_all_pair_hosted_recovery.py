import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0,str(Path(__file__).parents[1]/"tools/probes"))
from pulse_all_pair_hosted import recovery


class HostedRecoveryTests(TestCase):
    def test_transport_recovery_is_quiet_bounded_and_resets_only_on_health(self):
        body={"stopped":"source_transport_failed"}
        self.assertEqual(recovery(200,body,0),(True,1,60))
        self.assertEqual(recovery(200,body,1),(True,2,60))
        self.assertEqual(recovery(200,body,2),(False,2,0))
        self.assertEqual(recovery(200,{"stopped":"time_budget"},2),(True,0,0))

    def test_bans_quota_corruption_memory_and_http_errors_do_not_retry(self):
        for stop in ("admission:circuit_open","admission:history_budget_exhausted",
                     "staging_memory_gate_stop","admission:budget_ledger_lost",
                     "admission:coindcx_circuit_open","oi_source_http_429"):
            self.assertEqual(recovery(200,{"stopped":stop},0),(False,0,0))
        for status in (403,418,429,451,503):
            self.assertFalse(recovery(status,{"stopped":"source_transport_failed"},0)[0])
        for count in (True,-1,3):
            with self.assertRaises(ValueError):recovery(200,{},count)
