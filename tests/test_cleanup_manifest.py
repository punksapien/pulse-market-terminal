import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

path=Path(__file__).resolve().parents[1]/"tools/probes/pulse_cleanup_manifest.py"
import sys
sys.path.insert(0,str(path.parent))
spec=importlib.util.spec_from_file_location("cleanup_manifest",path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CleanupManifestTests(unittest.TestCase):
    def test_exact_candidates_only_and_companions(self):
        keys=module.manifest()
        self.assertEqual(len(keys),len(set(keys)))
        self.assertGreater(len(keys),70)
        for key in keys:
            self.assertTrue(key.startswith("pulse:stg:"))
            self.assertNotIn("*",key)
            if key.endswith(":frame"):
                self.assertIn(key[:-5]+"fence",keys)
                self.assertEqual(module.lease_for(key),key[:-5]+"lease")
        self.assertNotIn("pulse:stg:{real}:BTC:oi:frame",keys)

    def test_compare_before_unlink_and_no_broad_commands(self):
        script=module.COMPARE_UNLINK
        self.assertLess(script.index("redis.sha1hex"),script.index("redis.call('UNLINK'"))
        self.assertIn("active_lease_refused",script)
        self.assertNotIn("SCAN",script)
        self.assertNotIn("FLUSH",script)

    def test_active_zero_ms_lease_and_unexpected_type_stop_before_deletion(self):
        first=module.manifest()[0]
        wrong=b"hash" if module.expected_type(first)==b"string" else b"string"
        for kind,ttl in ((module.expected_type(first),0),(wrong,-2)):
            client=Mock()
            client.info.return_value={"used_memory":5000000}
            pipe=client.pipeline.return_value
            pipe.execute.return_value=[kind,100,ttl]+[b"none",None,-2]*15
            with patch.object(module,"connect_staging",return_value=(client,{})):
                result=module.run("unused",apply=True)
            self.assertEqual(result["status"],"cleanup_stopped")
            pipe.eval.assert_not_called()
            client.close.assert_called_once()
