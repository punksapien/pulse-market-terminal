import copy
import unittest

from pulse.store.staging_policy import (
    MeasuredCapacity, require_identity, require_tls_url, require_staging_url, staging_key,
)
from pulse.store.recovery import PressureLadder


class StagingPolicyTests(unittest.TestCase):
    def test_plaintext_exception_is_explicit_and_never_changes_tls_default(self):
        url = "redis://default:synthetic-password@fixture.invalid:6380/0"
        for check in (require_tls_url, require_staging_url):
            with self.assertRaises(ValueError):
                check(url)
        self.assertEqual(require_staging_url(url, approved_plaintext=True), url)
        with self.assertRaises(ValueError):
            require_staging_url(url, approved_plaintext=1)
        with self.assertRaises(ValueError):
            require_staging_url(url+"?ssl_cert_reqs=none", approved_plaintext=True)

    def test_all_key_categories_are_staging_only(self):
        for suffix in ("{abc}:btc:oi:ring", "lease", "quota", "repair", "artifact:123"):
            self.assertEqual(staging_key("pulse:stg:" + suffix), "pulse:stg:" + suffix)
        for key in ("pulse:prod:ring", "vol:v2:ring", "pulse:stg", "pulse:stg:",
                    "pulse:stg:*", "pulse:stg:row\n", b"pulse:stg:ring", "pulse:stg:"+"x"*257):
            with self.assertRaisesRegex(ValueError, "scope_refused"):
                staging_key(key)

    def test_tls_is_required_and_options_cannot_disable_validation(self):
        url = "rediss://default:synthetic-password@fixture.invalid:6380/0"
        self.assertEqual(require_tls_url(url), url)
        for bad in (url.replace("rediss:", "redis:"), url+"?ssl_cert_reqs=none",
                    url+"?socket_timeout=0", url.replace(":6380", ""),
                    url.replace("/0", "/1"), url+"#fragment", None):
            with self.assertRaisesRegex(ValueError, "tls_url_refused") as error:
                require_tls_url(bad)
            self.assertNotIn("synthetic-password", str(error.exception))

    def test_resource_and_preview_binding_are_pinned(self):
        project = "prj_Iu3ENBLVf3uLtfVjshOISANFzrPN"
        resource = {"id":"store_UEHUkH5XFJYP1hur", "name":"pulse-redis",
                    "status":"available", "billingPlan":{"id":"26515"},
                    "metadata":{"StorageType":"RAM only", "Region":"bom1", "HighAvailability":"Single zone"},
                    "projectsMetadata":[{"projectId":project, "environments":["preview"],
                                         "envVarPrefix":"PULSE_", "environmentVariables":["PULSE_REDIS_URL"]}]}
        require_identity(resource, project)
        for field, value in (("id","legacy"), ("status","pending"), ("name","other")):
            bad = dict(resource, **{field:value})
            with self.assertRaises(ValueError):
                require_identity(bad, project)
        for envs in (["production"], ["production","preview"]):
            bad = copy.deepcopy(resource)
            bad["projectsMetadata"][0]["environments"] = envs
            with self.assertRaises(ValueError):
                require_identity(bad, project)

    def test_missing_zero_or_invalid_maxmemory_never_falls_back_to_plan(self):
        for maxmemory in (None, 0, -1, True, "125000000"):
            with self.assertRaisesRegex(ValueError, "unverified"):
                MeasuredCapacity.from_info({"used_memory":1, "maxmemory":maxmemory}, provider_usable_bytes=125000000)

    def test_measured_ceiling_and_provider_entitlement_both_bound_admission(self):
        for actual, ceiling in ((125000000,125000000), (100000000,100000000), (250000000,125000000)):
            capacity = MeasuredCapacity.from_info({"used_memory":1000000, "maxmemory":actual}, provider_usable_bytes=125000000)
            self.assertEqual(capacity.ceiling_bytes, ceiling)
            self.assertEqual(capacity.thresholds[70], ceiling*70//100)
        capacity = MeasuredCapacity.from_info({"used_memory":87000000, "maxmemory":125000000}, provider_usable_bytes=125000000)
        self.assertEqual(capacity.thresholds, {70:87500000,80:100000000,85:106250000,90:112500000,100:125000000})
        self.assertTrue(capacity.admit(100000,reserve_bytes=100000))
        self.assertTrue(capacity.admit(400000,reserve_bytes=100000))
        capacity = MeasuredCapacity(125000000,119900000)
        self.assertFalse(capacity.admit(0,reserve_bytes=100000))

    def test_authorised_staging_ladder_and_recovery_hysteresis(self):
        for used, expected in ((100000000,"normal"),(109999999,"normal"),
                (110000000,"bootstrap_repair_paused"),(115000000,"bootstrap_repair_paused"),
                (119999999,"bootstrap_repair_paused"),(120000000,"hard_stop")):
            self.assertEqual(MeasuredCapacity(125000000,used).pressure(),expected)
        self.assertEqual(MeasuredCapacity(125000000,105000000).pressure(previous="hard_stop"),"hard_stop")
        self.assertEqual(MeasuredCapacity(125000000,105000000).pressure(previous="bootstrap_repair_paused"),"bootstrap_repair_paused")
        self.assertEqual(MeasuredCapacity(125000000,104999999).pressure(previous="hard_stop"),"normal")
        self.assertFalse(MeasuredCapacity(30000000,24000000).admit(0,reserve_bytes=0))
        self.assertEqual(MeasuredCapacity(30000000,27000000).pressure(),"hard_stop")
        with self.assertRaises(ValueError):
            MeasuredCapacity(125000000,1).pressure(previous="bad")

    def test_capacity_is_raw_ceiling_not_seventy_percent_applied_twice(self):
        # PressureLadder itself owns the percentages; integration must pass the
        # measured ceiling, not the 70% admission threshold.
        from pulse.store.memory_store import MemoryStore
        store = MemoryStore(1000000)
        token = 1
        store.claim(token)
        ladder = PressureLadder(store, "pressure", token, hard_ceiling=125000000)
        for used, level in ((87500000,1), (100000000,2), (112500000,3), (125000000,4)):
            self.assertEqual(ladder.observe(used,token)["level"],level)
