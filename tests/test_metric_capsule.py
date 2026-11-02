from dataclasses import replace
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock,patch

from pulse.store.capsule_model import CapsuleSpec, observation, empty_capsule, derived_bytes, decode_derived, footer
from pulse.store.metric_capsule import MetricCapsule

ROOT=Path(__file__).resolve().parents[1]
module_spec=importlib.util.spec_from_file_location("metric_reference",ROOT/"tools/probes/metric_capsule_reference.py")
reference_module=importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(reference_module)


def spec(**kwargs):
    return CapsuleSpec(**dict(dict(pair_id="B-ETH_USDT",symbol="ETHUSDT",metric="oi",unit="quantity",
        step_ms=60000,capacity=1536,ttl_ms=900000,max_ingress_age_ms=900000,inclusive_expiry=True),**kwargs))


class ModelTests(unittest.TestCase):
    def test_scalar_layout_and_pinned_identity(self):
        one=spec()
        self.assertEqual(one.nbytes,25152)
        self.assertEqual(len(empty_capsule(one)),one.nbytes)
        for changed in (replace(one,pair_id="B-BTC_USDT"),replace(one,symbol="BTCUSDT"),
                        replace(one,ttl_ms=100),replace(one,mapping_version="v2")):
            self.assertNotEqual(changed.mapping,one.mapping)
        self.assertEqual(empty_capsule(one)[one.ring_bytes+16:one.ring_bytes+32],one.mapping)

    def test_invalid_configuration(self):
        for kwargs in ({"metric":[]},{"unit":[]},{"metric":"unknown"},{"unit":"fraction"},
                       {"capacity":True},{"columns":2},{"step_ms":0},{"capacity":1537},
                       {"pair_id":"BTC"},{"symbol":"BTCUSD"},{"mapping_version":"../x"}):
            with self.assertRaises((ValueError,TypeError)):
                spec(**kwargs)

    def test_signed_multicolumn_roundtrip_and_precision_rejection(self):
        signed=spec(metric="funding_settled",unit="fraction")
        point=observation(signed,1000,("-0.00010",))
        self.assertEqual(decode_derived(signed,derived_bytes(signed,point,3)),(point,3))
        multiple=spec(metric="ls_global",unit="ratio_shares",capacity=320,step_ms=300000)
        point=observation(multiple,1000,("1.50","0.6","0.4"))
        self.assertEqual(multiple.derived_bytes,240)
        self.assertEqual(decode_derived(multiple,derived_bytes(multiple,point,1)),(point,1))
        for source in ("-1","NaN","Infinity","0.123456789012345678901","1e-320","1e999","no","", "1"*64):
            with self.assertRaises(ValueError):
                observation(spec(),1000,(source,))
        for stamp in (True,-1,2**53):
            with self.assertRaises(ValueError):
                observation(spec(),stamp,("1",))
        with self.assertRaises(ValueError):
            observation(spec(),1000,["1"])

    def test_expiry_and_stored_source_tampering(self):
        one=spec()
        point=observation(one,1000,("100.00",),expires_ms=1100)
        self.assertEqual(decode_derived(one,derived_bytes(one,point,1))[0],point)
        for expiry in (1000,901002,True):
            with self.assertRaises(ValueError):
                observation(one,1000,("1",),expires_ms=expiry)
        for version in (0,True,2**53):
            with self.assertRaises(ValueError):
                derived_bytes(one,point,version)
        raw=bytearray(derived_bytes(one,point,1))
        struct.pack_into("<d",raw,8,101.)
        with self.assertRaisesRegex(ValueError,"mismatch"):
            decode_derived(one,raw)
        raw=bytearray(derived_bytes(one,point,1)); raw[-1]=1
        with self.assertRaises(ValueError):
            decode_derived(one,raw)
        with self.assertRaises(ValueError):
            derived_bytes(one,replace(point,values=(999.,)),1)
        with self.assertRaises(ValueError):
            footer(one,b"",b"")

    def test_generic_oracle_collisions_and_non_byte_coverage(self):
        one=spec(capacity=9)
        ref=reference_module.MetricCapsuleReference(one)
        self.assertEqual(ref.to_bytes(),empty_capsule(one))
        outcomes=[]
        for stamp,source in ((100,"100"),(100,"100"),(100,"101"),(200,"102"),(50,"90"),(600100,"103")):
            outcomes.append(ref.write(observation(one,stamp,(source,)),now_ms=max(stamp,600100)))
        self.assertEqual(outcomes,["written","duplicate","exact_timestamp_conflict","written","dropped_older","written"])
        self.assertEqual(int.from_bytes(ref.to_bytes()[-2:],"little"),1)
        before=ref.to_bytes()
        self.assertEqual(ref.write(observation(one,9999999,("1",)),now_ms=600100),"future_timestamp")
        self.assertEqual(ref.to_bytes(),before)


class AdapterTests(unittest.TestCase):
    def test_existing_only_never_builds_a_frame_and_reserves_no_new_payload(self):
        client,store=self.store();store.owner=b"1:nonce"
        client.eval.return_value=[b"already_initialised"]
        with patch("pulse.store.metric_capsule.empty_capsule",side_effect=AssertionError("allocation")),\
                patch.object(store,"preflight") as preflight:
            self.assertEqual(store.initialise(require_existing=True),[b"already_initialised"])
        self.assertEqual(preflight.call_args_list[0].args,(0,))
        args=client.eval.call_args.args
        self.assertEqual(args[5],b"")
        self.assertEqual(args[-1],"existing_only")

    def test_existing_only_refusals_never_turn_into_bootstrap(self):
        for reason in (b"existing_ring_required",b"invalid_ring_lifecycle",b"mapping_invalid",b"fenced_out",b"initialised"):
            with self.subTest(reason=reason):
                client,store=self.store();store.owner=b"1:nonce"
                client.eval.return_value=[reason]
                with self.assertRaisesRegex(RuntimeError,"initialisation_refused"):
                    store.initialise(require_existing=True)
                self.assertEqual(client.eval.call_count,1)
                self.assertEqual(client.eval.call_args.args[-1],"existing_only")
        client,store=self.store();store.owner=b"1:nonce"
        for policy in (1,None,"existing_only"):
            with self.assertRaisesRegex(ValueError,"existing_ring_policy"):
                store.initialise(require_existing=policy)
        client.eval.assert_not_called()

    def test_existing_only_keeps_the_hard_memory_gate(self):
        client,store=self.store();store.owner=b"1:nonce"
        for used in (119000000,120000000):
            client.info.return_value["used_memory"]=used
            with self.assertRaises(RuntimeError):store.initialise(require_existing=True)
        client.eval.assert_not_called()

    def test_lua_load_reused_only_for_same_client(self):
        client,one=self.store()
        other=MetricCapsule(client,"a"*32,spec(pair_id="B-BTC_USDT",symbol="BTCUSDT"),approved_plan_fallback=True)
        self.assertEqual(one._script("capsule_read.lua"),other._script("capsule_read.lua"))
        self.assertEqual(client.script_load.call_count,1)
        separate,third=self.store()
        third._script("capsule_read.lua")
        self.assertEqual(separate.script_load.call_count,1)
        self.assertEqual(client.script_load.call_count,1)

    def store(self):
        client=Mock()
        client.info.return_value={"used_memory":5000000,"maxmemory_policy":"noeviction"}
        return client,MetricCapsule(client,"a"*32,spec(),approved_plan_fallback=True)

    def test_construction_is_network_free_and_scope_fixed(self):
        client,store=self.store()
        client.assert_not_called()
        self.assertEqual(client.mock_calls,[])
        self.assertTrue(store.frame.startswith("pulse:stg:"))
        for run in ("bad","b"*31,"G"*32):
            with self.assertRaises(ValueError):
                MetricCapsule(client,run,spec())
        for operation in (store.initialise,store.renew,lambda:store.write(Mock())):
            with self.assertRaisesRegex(RuntimeError,"no_lease"):
                operation()

    def test_initialise_renew_and_typed_write(self):
        client,store=self.store()
        client.eval.return_value=b"1:nonce"
        store.acquire()
        client.eval.return_value=[b"initialised"]
        store.initialise()
        client.eval.return_value=1
        store.renew()
        client.evalsha.return_value=[b"written",b"1000"]
        point=observation(spec(),1000,("100.00",))
        self.assertEqual(store.write(point)[0],b"written")
        args=client.evalsha.call_args.args
        self.assertEqual(args[-8:-1],spec().wire_config)
        client.evalsha.return_value=[b"available",derived_bytes(spec(),point,1)]
        self.assertEqual(store.read()["sources"],("100.00",))
        client.evalsha.return_value=[b"expired"]
        self.assertEqual(store.read()["reason"],"expired")
        client.evalsha.return_value=[b"available",b"bad"]
        self.assertEqual(store.read()["reason"],"derived_integrity_failed")
        client.evalsha.return_value=[b"available",struct.pack("<Qd",1000,100.),b"3"]
        self.assertEqual(store.baseline(1000,60000)["version"],3)
        client.evalsha.return_value=[b"discarded_baseline_candidate"]
        self.assertEqual(store.baseline(1000,60000)["reason"],"discarded_baseline_candidate")
        with self.assertRaises(ValueError):
            store.baseline(1000,300000)
        client.eval.return_value=0
        with self.assertRaisesRegex(RuntimeError,"fenced_out"):
            store.renew()

    def test_write_and_memory_fail_closed(self):
        client,store=self.store()
        client.eval.return_value=False
        with self.assertRaisesRegex(RuntimeError,"lease_busy"):
            store.acquire()
        store.owner=b"1:nonce"
        point=observation(spec(),1000,("1",))
        with self.assertRaises(ValueError):
            store.write(replace(point,values=(2.,)))
        client.evalsha.assert_not_called()
        client.info.return_value["used_memory"]=120000000
        with self.assertRaises(RuntimeError):
            store.initialise()

    def test_mutated_key_is_refused_before_network_dispatch(self):
        client,store=self.store()
        store.frame="pulse:prod:frame"
        store.owner=b"owner"
        for operation in (store.acquire,store.initialise,store.read,store.renew,
                          lambda:store.baseline(1000,60000),
                          lambda:store.write(observation(spec(),1000,("1",)))):
            with self.assertRaisesRegex(ValueError,"scope_refused"):
                operation()
        self.assertEqual(client.mock_calls,[])

    def test_bounded_batch_prevalidates_then_renews_before_writes(self):
        client,store=self.store()
        store.owner=b"owner"
        point=observation(spec(),1000,("1",))
        pipe=client.pipeline.return_value
        pipe.execute.return_value=[1,[b"written",b"1000"],[b"duplicate"]]
        self.assertEqual(len(store.write_many([point,point])),2)
        client.pipeline.assert_called_with(transaction=False)
        self.assertEqual(pipe.eval.call_count,1)
        self.assertEqual(pipe.evalsha.call_count,2)
        for points in ([],[point]*17,[replace(point,values=(2.,))]):
            pipe.reset_mock()
            with self.assertRaises(ValueError):
                store.write_many(points)
            pipe.execute.assert_not_called()
        pipe.execute.return_value=[0,[b"fenced_out"]]
        with self.assertRaisesRegex(RuntimeError,"fenced_out"):
            store.write_many([point])
