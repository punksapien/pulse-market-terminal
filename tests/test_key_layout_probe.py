import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

PROBES=Path(__file__).resolve().parents[1]/"tools/probes"
spec=importlib.util.spec_from_file_location("key_layout_probe",PROBES/"pulse_staging_key_layout.py")
module=importlib.util.module_from_spec(spec)
sys.path.insert(0,str(PROBES))
try:
    spec.loader.exec_module(module)
finally:
    sys.path.pop(0)


class KeyLayoutProbeTests(unittest.TestCase):
    def test_over_limit_aborts_before_any_key_is_created(self):
        client=Mock()
        client.info.return_value={"used_memory":120000000,"maxmemory_policy":"noeviction"}
        with patch.object(module,"connect_staging",return_value=(client,{})):
            result=module.run("fixture-cli")
        self.assertEqual(result["status"],"stopped_memory_experiment")
        self.assertEqual(result["stop_observation"]["used_memory"],120000000)
        self.assertEqual(result["attempted_frame_keys"],[])
        client.set.assert_not_called()
        client.close.assert_called_once()

    def test_samples_include_peak_rss_fragmentation_and_bound_key_scope(self):
        client=Mock()
        client.info.return_value={"used_memory":10000000,"maxmemory_policy":"noeviction",
                                  "used_memory_rss":11000000,"used_memory_peak":12000000,
                                  "mem_fragmentation_ratio":1.1,"used_memory_lua":1000}
        client.pipeline.return_value.execute.return_value=[28752]
        sample=module.sample(client,["pulse:stg:fixture"])
        self.assertEqual(sample["key_memory_total"],28752)
        self.assertEqual(sample["used_memory_rss"],11000000)
        self.assertEqual(sample["used_memory_peak"],12000000)
        self.assertEqual(sample["mem_fragmentation_ratio"],1.1)
        with self.assertRaises(ValueError):
            module.sample(client,["pulse:prod:fixture"])
