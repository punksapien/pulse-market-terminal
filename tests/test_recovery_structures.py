import unittest
import random
from dataclasses import replace

from pulse.store.memory_store import MemoryStore
from pulse.store.bucket_ring import BucketRing, BucketCollision, WriteStatus
from pulse.store.recovery import (DerivedRecord, IncrementalBuilder, DeadlineArtifacts,
                                  RepairQueue, PressureLadder, RollingScrubber)
from pulse.store.admission import BudgetTracker, BudgetLimit, CircuitBreaker
from pulse.store.coverage import CoverageBitmap


def record(row=0,version=1,source=100,expiry=1000):
    return DerivedRecord(str(row).encode(),source,expiry,(("source",version),))


class RecoveryTests(unittest.TestCase):
    def test_rolling_coverage_popcount_matches_tag_aware_ring_oracle(self):
        rng=random.Random(54)
        ring=BucketRing(60000,64)
        coverage=CoverageBitmap(self.store,"coverage",64,1)
        for bucket in range(1000):
            if rng.random()<.2:
                continue
            ring.write(bucket*60000,(1.,),now_ms=bucket*60000)
            coverage.observe(bucket,status=WriteStatus.WRITTEN,token=1)
            if rng.random()<.1:
                with self.assertRaises(BucketCollision):
                    ring.write(bucket*60000,(2.,),now_ms=bucket*60000)
                coverage.observe(bucket,status=WriteStatus.CONFLICT,token=1)
                coverage.observe(bucket,status=WriteStatus.DROPPED_OLDER,token=1)
            first=max(0,bucket-63)
            self.assertEqual(coverage.count(first,bucket),ring.coverage_buckets(first,bucket))
        coverage.observe(10**12,status=WriteStatus.WRITTEN,token=1)
        self.assertEqual(coverage.count(10**12-63,10**12),(1,64))
        with self.assertRaises(ValueError):
            coverage.observe(0,status=WriteStatus.WRITTEN,token=1)

    def test_coverage_retry_cannot_clear_conflict_and_loss_is_unknown(self):
        coverage=CoverageBitmap(self.store,"coverage",8,1)
        coverage.observe(1,status=WriteStatus.WRITTEN,token=1)
        coverage.observe(1,status=WriteStatus.CONFLICT,token=1)
        for status in (WriteStatus.DUPLICATE,WriteStatus.DROPPED_OLDER):
            coverage.observe(1,status=status,token=1)
            self.assertEqual(coverage.count(1,1),(0,1))
        coverage.observe(1,status=WriteStatus.WRITTEN,token=1)
        self.assertEqual(coverage.count(1,1),(1,1))
        self.store.atomic_control(1,lambda state:state.clear())
        self.assertIsNone(coverage.count(1,1))

    def setUp(self):
        self.store=MemoryStore(21000000)
        self.store.claim(1)

    def test_builder_cold_start_expiry_and_cached_rows(self):
        builder=IncrementalBuilder(self.store,"rows",5,1)
        self.assertEqual(builder.dirty_count,5)
        self.assertIsNone(builder.read(0,now_ms=100))
        calls=[]
        self.assertTrue(builder.build(lambda row:(calls.append(row),record(row))[1],1,now_ms=100))
        self.assertEqual(calls,list(range(5)))
        builder.mark(3,1)
        self.assertTrue(builder.build(lambda row:record(row,2),1,now_ms=100))
        self.assertEqual(builder.read(2,now_ms=999).versions,(("source",1),))
        self.assertIsNone(builder.read(2,now_ms=1000))
        self.assertEqual(builder.dirty_count,0)

    def test_builder_mid_calculation_update_cannot_be_lost(self):
        builder=IncrementalBuilder(self.store,"rows",2,1)
        def derive(row):
            if row==1:
                builder.mark(0,1)
            return record(row)
        self.assertFalse(builder.build(derive,1,now_ms=100))
        self.assertEqual(builder.dirty_count,1)
        self.assertIsNone(builder.read(0,now_ms=100))
        self.assertIsNotNone(builder.read(1,now_ms=100))
        self.assertTrue(builder.build(record,1,now_ms=100))

    def test_builder_failure_fence_regression_and_conflict_keep_dirty(self):
        builder=IncrementalBuilder(self.store,"rows",1,1)
        self.assertFalse(builder.build(lambda row:(_ for _ in ()).throw(RuntimeError("crash")),1,now_ms=100))
        self.assertEqual(builder.failure(0)[1],"derive_failed")
        self.assertEqual(builder.dirty_count,0)
        builder.mark(0,1)
        builder.build(record,1,now_ms=100)
        for bad in (record(version=0),replace(record(),payload=b"changed"),record(version=2,source=99),replace(record(),payload=b"x"*257)):
            builder.mark(0,1)
            self.assertFalse(builder.build(lambda row:bad,1,now_ms=100))
            self.assertEqual(builder.dirty_count,0)
            self.assertIsNone(builder.read(0,now_ms=100))
        self.store.claim(2)
        with self.assertRaises(PermissionError):
            builder.build(record,1,now_ms=100)

    def test_builder_recovers_after_empty_store(self):
        builder=IncrementalBuilder(self.store,"rows",2,1)
        builder.build(record,1,now_ms=100)
        # Fault injection only; no production or Redis deletion.
        self.store.atomic_control(1,lambda state:state.clear())
        self.assertIsNone(builder.read(0,now_ms=100))
        replacement=IncrementalBuilder(self.store,"rows",2,1)
        self.assertEqual(replacement.dirty_count,2)
        replacement.build(record,1,now_ms=100)
        self.assertIsNotNone(replacement.read(1,now_ms=100))

    def test_artifact_orphan_reaper_bounded_and_retained_expired_not_fresh(self):
        artifacts=DeadlineArtifacts(self.store,"artifacts",1,max_bytes=20,max_items=5)
        live=artifacts.stage(b"active",expires_ms=200,deadline_ms=299,now_ms=100,token=1)
        orphan=artifacts.stage(b"orphan",expires_ms=200,deadline_ms=300,now_ms=100,token=1)
        artifacts.retain("active",(live,),1)
        self.assertEqual(artifacts.reap(now_ms=300,token=1,batch=1),())
        self.assertEqual(artifacts.reap(now_ms=300,token=1,batch=1),(orphan,))
        self.assertIsNone(artifacts.read(live,now_ms=200))
        artifacts.retain("active",(),1)
        self.assertEqual(artifacts.reap(now_ms=1300,token=1),(live,))

    def test_artifact_identity_capacity_and_restart_deadline(self):
        artifacts=DeadlineArtifacts(self.store,"artifacts",1,max_bytes=8,max_items=2)
        digest=artifacts.stage(b"abc",expires_ms=200,deadline_ms=300,now_ms=100,token=1)
        self.assertEqual(artifacts.stage(b"abc",expires_ms=200,deadline_ms=300,now_ms=100,token=1),digest)
        self.assertEqual(artifacts.stage(b"abc",expires_ms=201,deadline_ms=301,now_ms=100,token=1),digest)
        with self.assertRaises(MemoryError):
            artifacts.stage(b"123456",expires_ms=200,deadline_ms=300,now_ms=100,token=1)
        restarted=DeadlineArtifacts(self.store,"artifacts",1,max_bytes=8,max_items=2)
        self.assertEqual(restarted.reap(now_ms=300,token=1),())
        self.assertEqual(restarted.reap(now_ms=301,token=1),(digest,))

    def test_repair_priority_dedup_crash_and_ack_epochs(self):
        queue=RepairQueue(self.store,"repairs",1,capacity=2)
        a,b=("BTC","oi","1H"),("ETH","oi","1H")
        self.assertTrue(queue.enqueue(a,expires_ms=1000,importance=1,now_ms=0,token=1))
        self.assertFalse(queue.enqueue(a,expires_ms=900,importance=2,now_ms=0,token=1))
        queue.enqueue(b,expires_ms=200,importance=1,now_ms=0,token=1)
        with self.assertRaises(MemoryError):
            queue.enqueue(("DOGE","oi","1H"),expires_ms=100,importance=1,now_ms=0,token=1)
        first=queue.claim(now_ms=0,lease_ms=100,token=1)
        self.assertEqual(first[0][0],b)
        second=queue.claim(now_ms=50,lease_ms=100,token=1)
        self.assertEqual(second[0][0],a)
        self.assertEqual(queue.claim(now_ms=99,lease_ms=100,token=1),())
        retry=RepairQueue(self.store,"repairs",1,capacity=2).claim(now_ms=100,lease_ms=100,token=1)
        self.assertFalse(queue.ack(*first[0],1,now_ms=101))
        self.assertTrue(queue.ack(*retry[0],1,now_ms=101))

    def test_changed_repair_while_leased_cannot_be_lost_to_ack(self):
        queue=RepairQueue(self.store,"repairs",1)
        key=("BTC","oi","1H")
        queue.enqueue(key,expires_ms=1000,importance=1,now_ms=0,token=1)
        claim=queue.claim(now_ms=0,lease_ms=100,token=1)[0]
        queue.enqueue(key,expires_ms=500,importance=1,now_ms=1,token=1)
        self.assertFalse(queue.ack(*claim,1,now_ms=2))
        self.assertTrue(queue.claim(now_ms=100,lease_ms=100,token=1))

    def test_rolling_scrub_checks_headers_and_detects_missing(self):
        self.store.create_ring("oi:BTC",60000,4,1,1)
        self.store.create_ring("oi:ETH",60000,4,1,1)
        scrub=RollingScrubber(self.store,"scrub",("oi:BTC","oi:ETH","missing"),1)
        self.assertEqual(scrub.tick(1),(("oi:BTC","valid"),))
        self.store._rings["oi:ETH"]._data[0]^=1  # fault injection
        self.assertEqual(scrub.tick(1),(("oi:ETH","integrity_failed"),))
        self.assertEqual(scrub.tick(1),(("missing","missing"),))

    def test_exact_rolling_and_monthly_budgets_all_or_none(self):
        limits={"requests":BudgetLimit(2,1000),"bytes":BudgetLimit(10,billing_start_ms=0,billing_end_ms=10000)}
        budget=BudgetTracker(self.store,"budget",limits,1)
        budget.reconcile({"requests":0,"bytes":0},now_ms=0,evidence_id="offline-fixture",token=1)
        self.assertEqual(budget.reserve({"requests":1,"bytes":4},now_ms=0,token=1),"reserved")
        self.assertEqual(budget.reserve({"requests":1,"bytes":7},now_ms=1,token=1),"budget_exhausted:bytes")
        self.assertEqual(budget.reserve({"requests":1,"bytes":4},now_ms=2,token=1),"reserved")
        self.assertEqual(budget.reserve({"requests":1,"bytes":0},now_ms=999,token=1),"budget_exhausted:requests")
        self.assertEqual(budget.reserve({"requests":1,"bytes":2},now_ms=1000,token=1),"reserved")
        self.assertEqual(budget.reserve({"requests":0,"bytes":1},now_ms=1001,token=1),"budget_exhausted:bytes")
        self.assertEqual(budget.reserve({"requests":1,"bytes":0},now_ms=999,token=1),"clock_regression")
        self.assertEqual(budget.reserve({"requests":1,"bytes":0},now_ms=10000,token=1),"billing_window_unverified")

    def test_budget_ledger_bound_and_quota_before_repair_claim(self):
        budget=BudgetTracker(self.store,"budget",{"requests":BudgetLimit(100,1000,max_entries=1)},1)
        budget.reconcile({"requests":0},now_ms=0,evidence_id="offline-fixture",token=1)
        queue=RepairQueue(self.store,"repairs",1)
        queue.enqueue(("BTC","oi","1H"),expires_ms=1000,importance=1,now_ms=0,token=1)
        def admitted(now):
            return self.store.atomic_control(1,lambda _:queue.claim(now_ms=now,lease_ms=10,token=1) if budget.reserve({"requests":1},now_ms=now,token=1)=="reserved" else ())
        self.assertTrue(admitted(0))
        self.assertFalse(admitted(20))
        self.assertTrue(queue.claim(now_ms=20,lease_ms=10,token=1))

    def test_circuit_bootstrap_single_probe_jitter_and_expiry(self):
        circuit=CircuitBreaker(self.store,"provider-family",1)
        self.assertEqual(circuit.allow(now_ms=0,token=1),(False,"reconciliation_required"))
        circuit.reconcile(now_ms=0,token=1)
        permitted,probe=circuit.allow(now_ms=0,token=1)
        self.assertTrue(permitted)
        self.assertFalse(circuit.allow(now_ms=0,token=1)[0])
        self.assertEqual(circuit.outcome(now_ms=1,token=1,status=429,draw=.5,probe=probe,retry_after_ms=2000),2000)
        self.assertFalse(circuit.allow(now_ms=2000,token=1)[0])
        allowed,probe=circuit.allow(now_ms=2001,token=1)
        self.assertTrue(allowed)
        circuit.outcome(now_ms=2002,token=1,status=200,draw=0,probe=probe)
        self.assertEqual(circuit.allow(now_ms=2003,token=1),(True,None))
        self.assertFalse(circuit.allow(now_ms=302002,token=1)[0])
        with self.assertRaises(ValueError):
            circuit.outcome(now_ms=302002,token=1,status=200,draw=0)

    def test_lost_budget_requires_evidence_and_cannot_reset_active_usage(self):
        budget=BudgetTracker(self.store,"quota",{"requests":BudgetLimit(2,1000)},1)
        self.assertEqual(budget.reserve({"requests":1},now_ms=0,token=1),"budget_reconciliation_required")
        budget.reconcile({"requests":2},now_ms=0,evidence_id="offline-prior-usage",token=1)
        self.assertEqual(budget.reserve({"requests":1},now_ms=999,token=1),"budget_exhausted:requests")
        with self.assertRaises(ValueError):
            budget.reconcile({"requests":0},now_ms=999,evidence_id="unsafe-reset",token=1)
        self.assertEqual(budget.reserve({"requests":1},now_ms=1000,token=1),"reserved")
        self.store.atomic_control(1,lambda state:state.clear())
        self.assertEqual(budget.reserve({"requests":1},now_ms=1001,token=1),"budget_reconciliation_required")

    def test_circuit_hard_errors_and_crashed_probe_recovery(self):
        for status in (403,418,429,451):
            circuit=CircuitBreaker(self.store,"provider"+str(status),1)
            circuit.reconcile(now_ms=0,token=1)
            _,probe=circuit.allow(now_ms=0,token=1)
            delay=circuit.outcome(now_ms=1,token=1,status=status,draw=1,probe=probe)
            self.assertEqual(delay,1000)
            _,probe=circuit.allow(now_ms=1001,token=1)
            self.assertFalse(circuit.allow(now_ms=11000,token=1)[0])
            allowed,newprobe=circuit.allow(now_ms=11001,token=1)
            self.assertTrue(allowed)
            with self.assertRaises(ValueError):
                circuit.outcome(now_ms=11002,token=1,status=200,draw=0,probe=probe)
            circuit.outcome(now_ms=11003,token=1,status=200,draw=0,probe=newprobe)

    def test_pressure_gate_hysteresis_and_invalid_readings(self):
        pressure=PressureLadder(self.store,"pressure",1)
        for used,level in ((0,0),(14700000,1),(16800000,2),(18900000,3),(21000000,4),(20500000,4),(19949999,3),(17849999,2),(15749999,1),(13649999,0)):
            self.assertEqual(pressure.observe(used,1)["level"],level)
        with self.assertRaises(ValueError):
            pressure.observe(-1,1)

    def test_circuit_error_threshold_and_negative_clock_fail_closed(self):
        circuit=CircuitBreaker(self.store,"family",1,threshold=3)
        circuit.reconcile(now_ms=0,token=1)
        _,probe=circuit.allow(now_ms=0,token=1)
        circuit.outcome(now_ms=1,token=1,status=200,draw=0,probe=probe)
        for now in (2,3):
            self.assertTrue(circuit.allow(now_ms=now,token=1)[0])
            self.assertEqual(circuit.outcome(now_ms=now,token=1,status=500,draw=1),0)
        self.assertEqual(circuit.outcome(now_ms=4,token=1,status=500,draw=1),1000)
        self.assertEqual(circuit.allow(now_ms=3,token=1),(False,"clock_regression"))
        self.assertFalse(circuit.allow(now_ms=5,token=1)[0])

    def test_control_identity_dimensions_and_old_fence_are_refused(self):
        IncrementalBuilder(self.store,"rows",2,1)
        with self.assertRaises(ValueError):
            IncrementalBuilder(self.store,"rows",3,1)
        pressure=PressureLadder(self.store,"pressure",1)
        pressure.observe(18900000,1)
        self.assertEqual(PressureLadder(self.store,"pressure",1).observe(18500000,1)["level"],3)
        self.store.claim(2)
        with self.assertRaises(PermissionError):
            pressure.observe(0,1)

    def test_gap_popcount_does_not_count_stale_wrapped_or_conflicting_slots(self):
        ring=BucketRing(60000,4)
        for stamp in (0,120000,240000):
            ring.write(stamp,(1.,),now_ms=240000)
        self.assertEqual(ring.coverage_buckets(1,4),(2,4))
        with self.assertRaises(BucketCollision):
            ring.write(240000,(2.,),now_ms=240000)
        self.assertEqual(ring.coverage_buckets(1,4),(1,4))
