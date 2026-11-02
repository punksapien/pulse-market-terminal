import unittest
from pathlib import Path
from unittest.mock import Mock

from pulse.store.production_scope import ProductionScope, compile_script, connection_url, scope_client, release_enabled, dcx12_enabled
from pulse.store import runtime_document, artifact_publisher, worker_state


class ProductionScopeTests(unittest.TestCase):
    def test_release_and_quota_require_independent_production_approvals(self):
        preview={"VERCEL_ENV":"preview","PULSE_RELEASE_TOP350":"1","PULSE_PREVIEW_DCX12":"1"}
        self.assertTrue(release_enabled(preview));self.assertTrue(dcx12_enabled(preview))
        prod=dict(preview,VERCEL_ENV="production")
        self.assertFalse(release_enabled(prod))
        with self.assertRaises(ValueError):dcx12_enabled(prod)
        prod.pop("PULSE_PREVIEW_DCX12")
        prod.update(PULSE_PRODUCTION_ENABLED="1",PULSE_UNIVERSE_MODE="all")
        self.assertTrue(release_enabled(prod));self.assertFalse(dcx12_enabled(prod))
        prod["PULSE_PRODUCTION_DCX12_APPROVED"]="1"
        self.assertTrue(dcx12_enabled(prod))

    def setUp(self):
        self.raw = Mock()
        self.store = ProductionScope(self.raw)
        self.key = "pulse:stg:{" + "a" * 32 + "}:data"
        self.physical = self.key.replace("pulse:stg:", "pulse:prod:", 1)

    def test_only_keys_change_never_payload_or_hash_fields(self):
        for name in ("get", "getrange", "set", "hget", "hmget", "strlen", "pttl", "ttl"):
            getattr(self.store, name)(self.key, "pulse:stg:payload")
            getattr(self.raw, name).assert_called_once_with(self.physical, "pulse:stg:payload")
        for key in ("pulse:prod:foreign", "vol:v2:legacy", "other", b"pulse:stg:x", "pulse:stg:"):
            with self.assertRaises(ValueError):
                self.store.get(key)
        self.assertEqual(self.raw.get.call_count, 1)

    def test_eval_translates_declared_keys_only_and_compiles_guards(self):
        self.store.eval(runtime_document.FAILURE, 2, self.key, self.key, "pulse:stg:payload")
        args = self.raw.eval.call_args.args
        self.assertNotIn("pulse:stg:", args[0])
        self.assertIn("string.sub(KEYS[1],1,11)", args[0])
        self.assertEqual(args[1:], (2, self.physical, self.physical, "pulse:stg:payload"))
        for count in (0, True, 168, "1"):
            with self.assertRaises(ValueError):
                self.store.eval("return 1", count, self.key)
        with self.assertRaises(ValueError):
            self.store.eval("return 'pulse:stg:unknown_guard'", 1, self.key)

    def test_all_current_script_prefix_guards_compile(self):
        scripts = list(Path("pulse/store/lua").glob("*.lua"))
        self.assertTrue(scripts)
        for script in scripts:
            self.assertNotIn(b"pulse:stg:", compile_script(script.read_bytes()))
        for module in (runtime_document, artifact_publisher, worker_state):
            for value in vars(module).values():
                if isinstance(value, str) and "redis.call(" in value:
                    self.assertNotIn("pulse:stg:", compile_script(value))

    def test_evalsha_requires_a_script_loaded_by_this_scope(self):
        self.raw.script_load.return_value = b"synthetic-sha"
        with self.assertRaises(ValueError):
            self.store.evalsha(b"synthetic-sha", 1, self.key)
        sha = self.store.script_load(b"return redis.call('GET',KEYS[1])")
        self.store.evalsha(sha, 1, self.key)
        self.raw.evalsha.assert_called_once_with(sha, 1, self.physical)

    def test_pipeline_is_bounded_and_namespace_checked_before_queuing(self):
        pipe = self.store.pipeline(transaction=False)
        for _ in range(17):
            self.assertIs(pipe.get(self.key), pipe)
        with self.assertRaises(ValueError):
            pipe.get(self.key)
        pipe.execute()
        pipe.get(self.key)
        self.assertEqual(self.raw.pipeline.return_value.get.call_args.args, (self.physical,))
        with self.assertRaises(ValueError):
            pipe.get("pulse:prod:x")
        with self.assertRaises(ValueError):
            self.store.pipeline(transaction=True)

    def test_full_sixteen_sample_capsule_batch_includes_lease_renewal(self):
        from pulse.store.metric_capsule import MetricCapsule
        from pulse.store.capsule_model import CapsuleSpec, observation
        self.raw.info.return_value={"used_memory":10000000,"maxmemory_policy":"noeviction"}
        self.raw.script_load.return_value=b"a"*40
        self.raw.pipeline.return_value.execute.return_value=[1]+[[b"written"]]*16
        spec=CapsuleSpec("B-BTC_USDT","BTCUSDT","oi","quantity",60000,1536,900000,900000,True)
        store=MetricCapsule(self.store,"a"*32,spec,approved_plan_fallback=True)
        store.owner=b"owner"
        points=[observation(spec,1800000000000+i*60000,("100",)) for i in range(16)]
        self.assertEqual(len(store.write_many(points)),16)
        self.assertEqual(self.raw.pipeline.return_value.eval.call_count,1)
        self.assertEqual(self.raw.pipeline.return_value.evalsha.call_count,16)

    def test_no_arbitrary_or_destructive_command_escape_hatch(self):
        for method in ("execute_command", "delete", "scan", "keys", "flushdb", "hgetall"):
            with self.assertRaises(AttributeError):
                getattr(self.store, method)
        with self.assertRaises(ValueError):
            self.store.info("all")

    def test_environment_and_plaintext_exceptions_are_independent(self):
        preview = {"VERCEL_ENV":"preview", "PULSE_REDIS_URL":"redis://default:synthetic@fixture.invalid:6380",
                   "PULSE_STAGING_PLAINTEXT_APPROVED":"1"}
        self.assertIs(scope_client(self.raw, preview), self.raw)
        self.assertTrue(connection_url(preview).startswith("redis://"))
        production = dict(preview, VERCEL_ENV="production", PULSE_PRODUCTION_ENABLED="1", PULSE_UNIVERSE_MODE="all")
        self.assertIsInstance(scope_client(self.raw, production), ProductionScope)
        with self.assertRaises(ValueError):
            connection_url(production)
        production["PULSE_PRODUCTION_PLAINTEXT_APPROVED"] = "1"
        self.assertTrue(connection_url(production).startswith("redis://"))
        production["PULSE_PRODUCTION_ENABLED"] = "0"
        with self.assertRaises(ValueError):
            scope_client(self.raw, production)
        production.update(PULSE_PRODUCTION_ENABLED="1",PULSE_UNIVERSE_MODE="cohort")
        with self.assertRaises(ValueError):scope_client(self.raw,production)
