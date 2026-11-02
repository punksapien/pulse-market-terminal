import unittest
from unittest.mock import Mock

from pulse.store.staging_capsule import StagingCapsule
from pulse.store.staging_policy import MeasuredCapacity


class StagingCapsuleTests(unittest.TestCase):
    def client(self):
        client = Mock()
        client.info.return_value = {"used_memory":5000000,"maxmemory_policy":"noeviction"}
        client.eval.return_value = [b"initialised"]
        return client

    def test_plan_capacity_requires_explicit_exception_and_tracks_provenance(self):
        info = {"used_memory":5000000}
        with self.assertRaises(ValueError):
            MeasuredCapacity.from_info(info, provider_usable_bytes=125000000)
        capacity = MeasuredCapacity.from_info(info, provider_usable_bytes=125000000, approved_plan_fallback=True)
        self.assertEqual(capacity.capacity_source,"verified_plan_staging_exception")
        self.assertEqual(capacity.thresholds[70],87500000)
        for bad in (-1, True, "125000000"):
            with self.assertRaises(ValueError):
                MeasuredCapacity.from_info(dict(info,maxmemory=bad),provider_usable_bytes=125000000,approved_plan_fallback=True)

    def test_no_ring_ttl_and_source_calls_disabled_until_shared_admission(self):
        client = self.client()
        store = StagingCapsule(client,"a"*32,approved_plan_fallback=True)
        store.initialise()
        script = client.eval.call_args.args[0]
        self.assertIn("redis.call('SET',KEYS[1],ARGV[2])",script)
        self.assertNotIn("'EX'",script)
        self.assertTrue(store.frame.startswith("pulse:stg:"))
        with self.assertRaisesRegex(RuntimeError,"shared_source_admission"):
            store.reserve_source()

    def test_foreign_keys_are_refused_before_read_or_write_dispatch(self):
        client = self.client()
        store = StagingCapsule(client,"a"*32,approved_plan_fallback=True)
        for key in ("pulse:prod:ring","vol:v2:ring","pulse:stg:{"+"b"*32+"}:btc:oi:frame"):
            with self.assertRaises(ValueError):
                store.read(frame=key)
            with self.assertRaises(ValueError):
                store.write(Mock(),frame=key)
        client.evalsha.assert_not_called()

    def test_unknown_capacity_or_unsafe_eviction_refuses_initialisation(self):
        client = self.client()
        store = StagingCapsule(client,"a"*32)
        with self.assertRaises(ValueError):
            store.initialise()
        client.eval.assert_not_called()
        store = StagingCapsule(client,"b"*32,approved_plan_fallback=True)
        client.info.return_value["maxmemory_policy"] = "allkeys-lru"
        with self.assertRaisesRegex(RuntimeError,"eviction"):
            store.initialise()
        client.eval.assert_not_called()

    def test_existing_tls_default_does_not_implicitly_adopt_exceptions(self):
        client = self.client()
        store = StagingCapsule(client,"a"*32,approved_plan_fallback=True)
        client.info.return_value["used_memory"] = 120000000
        with self.assertRaisesRegex(RuntimeError,"memory_gate"):
            store.initialise()
        client.eval.assert_not_called()

    def test_existing_expiring_frame_is_refused_not_silently_persisted(self):
        client = self.client()
        client.eval.return_value = [b"invalid_ring_lifecycle"]
        store = StagingCapsule(client,"a"*32,approved_plan_fallback=True)
        with self.assertRaisesRegex(RuntimeError,"initialisation_refused"):
            store.initialise()
        script = client.eval.call_args.args[0]
        self.assertIn("redis.call('PTTL',KEYS[1])~=-1",script)
        self.assertNotIn("'PERSIST'",script)
