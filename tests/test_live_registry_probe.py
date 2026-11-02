import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

PROBES=Path(__file__).resolve().parents[1]/"tools/probes"
spec=importlib.util.spec_from_file_location("live_registry_probe",PROBES/"pulse_live_registry.py")
probe=importlib.util.module_from_spec(spec)
sys.path.insert(0,str(PROBES))
try:spec.loader.exec_module(probe)
finally:sys.path.pop(0)


class RegistryProbeTests(unittest.TestCase):
    def test_invalid_bounds_do_not_connect(self):
        with patch.object(probe,"connect_staging") as connect:
            for options in ({"max_steps":0},{"max_steps":129},{"max_steps":True},
                    {"all_pairs":1},{"max_seconds":True},{"max_seconds":9},{"max_seconds":601}):
                with self.assertRaises(ValueError):probe.run("cli",None,"a"*32,**options)
            connect.assert_not_called()

    def test_full_bootstrap_stops_at_ready_and_reports_without_credentials(self):
        client=Mock();client.info.return_value={"used_memory":10000000}
        source=Mock(calls=19)
        values=[dict(status="incomplete",completed=8,total=12,excluded=[]),
                dict(status="registry_ready",universe=[{}]*12,mapping_version="sha256:"+"a"*64,excluded=["B-UNMAPPED_USDT"])]
        with patch.object(probe,"connect_staging",return_value=(client,{})),patch.object(probe,"Sources",return_value=source), \
                patch.object(probe,"bootstrap_step",side_effect=values) as step,patch.object(probe,"bootstrap") as legacy:
            result=probe.run("cli",None,"b"*32,all_pairs=True,max_steps=128)
        self.assertEqual((result["status"],result["pairs"],result["source_dispatches"]),("registry_ready",12,19))
        self.assertEqual(result["excluded"],["B-UNMAPPED_USDT"])
        self.assertFalse(result["production_access"])
        self.assertEqual(step.call_count,2)
        legacy.assert_not_called()
        client.close.assert_called_once()

    def test_partial_step_bound_and_close_on_failure(self):
        client=Mock();client.info.return_value={"used_memory":10000000}
        source=Mock(calls=10)
        with patch.object(probe,"connect_staging",return_value=(client,{})),patch.object(probe,"Sources",return_value=source), \
                patch.object(probe,"bootstrap_step",return_value=dict(status="incomplete",completed=8,total=504,excluded=[])) as step:
            result=probe.run("cli",None,"b"*32,all_pairs=True,max_steps=1)
            self.assertEqual((result["completed"],result["total"]),(8,504))
            self.assertEqual(step.call_count,1)
            step.side_effect=RuntimeError("source_circuit_open")
            with self.assertRaises(RuntimeError):probe.run("cli",None,"b"*32,all_pairs=True,max_steps=1)
        self.assertEqual(client.close.call_count,2)
