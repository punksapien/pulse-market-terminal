import unittest
from unittest.mock import Mock
from pulse.store.shared_admission import SharedAdmission, RollingLimit


class SharedAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock()
        self.client.info.return_value={"used_memory":5000000,"maxmemory_policy":"noeviction"}
        self.limits={"weight":RollingLimit(10,60000),"history":RollingLimit(5,300000)}
        self.store=SharedAdmission(self.client,"synthetic-egress",self.limits,approved_plan_fallback=True)

    def test_shared_identity_and_construction_no_network(self):
        self.assertEqual(self.client.mock_calls,[])
        other=SharedAdmission(self.client,"synthetic-egress",dict(reversed(list(self.limits.items()))))
        self.assertEqual(self.store.keys,other.keys)
        self.assertEqual(self.store.mapping,other.mapping)
        self.assertTrue(all(key.startswith("pulse:stg:") for key in self.store.keys))
        for scope in ("", "x{prod}",None):
            with self.assertRaises(ValueError):
                SharedAdmission(self.client,scope,self.limits)
        for value in (RollingLimit,{}, {"bad-key":RollingLimit(1,1)}):
            with self.assertRaises(ValueError):
                SharedAdmission(self.client,"x",value)
        for args in ((True,1),(0,1),(1,3600001),(1,1,8193)):
            with self.assertRaises(ValueError):
                RollingLimit(*args)

    def test_reconciliation_is_explicit_with_no_reset(self):
        self.client.eval.return_value=[b"reconciled",b"1000"]
        self.assertEqual(self.store.reconcile({"weight":2,"history":1},evidence_id="synthetic-fixture")[0],b"reconciled")
        args=self.client.eval.call_args.args
        self.assertIn("cannot_reset_live_budget",args[0])
        self.assertIn("synthetic-fixture",args)
        for usage in ({},{"weight":0,"history":True},{"weight":11,"history":0}):
            with self.assertRaises(ValueError):
                self.store.reconcile(usage,evidence_id="synthetic")
        with self.assertRaises(ValueError):
            self.store.reconcile({"weight":0,"history":0},evidence_id="")

    def test_reservation_denial_and_maintenance_are_not_permits(self):
        for outcome in (b"budget_reconciliation_required",b"budget_exhausted:1",b"budget_pruned_retry",b"reserved"):
            self.client.evalsha.return_value=[outcome]
            self.assertEqual(self.store.reserve({"weight":1,"history":1}),[outcome])
        self.assertEqual(self.client.script_load.call_count,1)
        first=self.client.evalsha.call_args_list[0].args
        second=self.client.evalsha.call_args_list[1].args
        self.assertNotEqual(first[6],second[6])
        for costs in ({},{"weight":0,"history":0},{"weight":1,"history":-1},{"weight":True,"history":1}):
            with self.assertRaises(ValueError):
                self.store.reserve(costs)

    def test_memory_stop_prevents_all_dispatch(self):
        self.client.info.return_value["used_memory"]=120000000
        with self.assertRaises(RuntimeError):
            self.store.reserve({"weight":1,"history":1})
        self.client.evalsha.assert_not_called()
        self.client.script_load.assert_not_called()

    def test_mutated_key_is_refused_before_network_dispatch(self):
        self.store.keys[0]="pulse:prod:meta"
        with self.assertRaisesRegex(ValueError,"scope_refused"):
            self.store.reserve({"weight":1,"history":1})
        with self.assertRaisesRegex(ValueError,"scope_refused"):
            self.store.reconcile({"weight":0,"history":0},evidence_id="fixture")
        self.assertEqual(self.client.mock_calls,[])
