import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

PROBES = Path(__file__).resolve().parents[1]/"tools/probes"
spec = importlib.util.spec_from_file_location("staging_gate_test_module",PROBES/"pulse_staging_gate.py")
gate = importlib.util.module_from_spec(spec)
sys.path.insert(0,str(PROBES))
try:
    spec.loader.exec_module(gate)
finally:
    sys.path.pop(0)


class StagingPreflightTests(unittest.TestCase):
    def responses(self, scheme="redis"):
        project=gate.PROJECT
        resource={"id":"store_UEHUkH5XFJYP1hur","name":"pulse-redis","status":"available",
                  "billingPlan":{"id":"26515"},
                  "metadata":{"StorageType":"RAM only","Region":"bom1","HighAvailability":"Single zone"},
                  "projectsMetadata":[{"projectId":project,"environments":["preview"],"envVarPrefix":"PULSE_",
                                       "environmentVariables":["PULSE_REDIS_URL"]}]}
        metadata={"id":"synthetic-preview-id","key":"PULSE_REDIS_URL","target":["preview"]}
        entry=dict(metadata,value=scheme+"://default:synthetic-password@fixture.invalid:6380/0")
        return [{"store":resource},{"envs":[metadata]},entry]

    def test_plaintext_without_exception_never_constructs_client(self):
        with patch.object(gate,"api",side_effect=self.responses()),patch("redis.Redis.from_url") as connect:
            result=gate.run("fixture-cli")
        self.assertFalse(result["redis_connected"])
        connect.assert_not_called()

    def test_wrong_resource_never_fetches_env_or_connects(self):
        responses=self.responses()
        responses[0]["store"]["id"]="legacy"
        with patch.object(gate,"api",side_effect=responses) as api,patch("redis.Redis.from_url") as connect:
            with self.assertRaises(ValueError):
                gate.run("fixture-cli",approved_plaintext=True)
        self.assertEqual(api.call_count,1)
        connect.assert_not_called()

    def test_production_entry_never_decrypts_or_connects(self):
        responses=self.responses()
        responses[1]["envs"][0]["target"]=["production"]
        with patch.object(gate,"api",side_effect=responses) as api,patch("redis.Redis.from_url") as connect:
            with self.assertRaisesRegex(RuntimeError,"preview_entry_refused"):
                gate.run("fixture-cli",approved_plaintext=True)
        self.assertEqual(api.call_count,2)
        connect.assert_not_called()

    def test_approved_exceptions_are_labelled_and_close_pooled_client(self):
        client=Mock()
        client.info.return_value={"used_memory":5000000,"maxmemory_policy":"noeviction"}
        client.config_get.return_value={}
        with patch.object(gate,"api",side_effect=self.responses()),patch("redis.Redis.from_url",return_value=client) as connect:
            result=gate.run("fixture-cli",approved_plaintext=True,approved_plan_fallback=True,diagnose_memory=True)
        self.assertEqual(result["capacity_source"],"verified_plan_staging_exception")
        self.assertFalse(result["tls_certificate_validated"])
        self.assertIsNone(result["observed_maxmemory"])
        self.assertEqual(result["config_maxmemory_returned_entries"],0)
        self.assertEqual(result["redis_writes"],0)
        self.assertNotIn("synthetic-password",str(result))
        self.assertEqual(connect.call_args.kwargs["max_connections"],1)
        client.close.assert_called_once()

    def test_secure_connection_enforces_certificate_and_hostname_validation(self):
        client=Mock()
        client.info.return_value={"used_memory":5000000,"maxmemory":125000000,"maxmemory_policy":"noeviction"}
        with patch.object(gate,"api",side_effect=self.responses("rediss")),patch("redis.Redis.from_url",return_value=client) as connect:
            result=gate.run("fixture-cli")
        self.assertTrue(result["tls_certificate_validated"])
        self.assertEqual(connect.call_args.kwargs["ssl_cert_reqs"],"required")
        self.assertTrue(connect.call_args.kwargs["ssl_check_hostname"])

    def test_capacity_exception_is_not_implicitly_enabled_with_plaintext(self):
        client=Mock()
        client.info.return_value={"used_memory":5000000,"maxmemory_policy":"noeviction"}
        with patch.object(gate,"api",side_effect=self.responses()),patch("redis.Redis.from_url",return_value=client):
            result=gate.run("fixture-cli",approved_plaintext=True)
        self.assertEqual(result["status"],"blocked_memory_ceiling_unverified")
        client.close.assert_called_once()
