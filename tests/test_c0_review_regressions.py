"""Independent reproductions for Review review79cd683 / ADR-0055."""
import unittest
from dataclasses import replace

from pulse.metrics.funding import settled_report, settled_view, current_delta, HOUR, GRACE
from pulse.store.memory_store import MemoryStore
from pulse.store.recovery import IncrementalBuilder, DeadlineArtifacts, RepairQueue
from pulse.store.admission import CircuitBreaker
from test_funding_calculation import history, metadata, estimate, T
from test_recovery_structures import record


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.store=MemoryStore(21000000)
        self.store.claim(1)

    def test_funding_old_metadata_keeps_prior_verified_settlements_to_r6(self):
        rows=history(4)
        meta=replace(metadata(rows),source_ms=T-70000)
        report=settled_report(rows,meta,now_ms=T+10000)
        self.assertEqual(report.verified_at_ms,T+10000)
        # Review repro: settlement60s old, metadata130s old.
        view=settled_view(report,now_ms=T+60000)
        for key in ("last","cumulative_1d","cumulative_7d","delta_1d","delta_7d"):
            self.assertEqual(getattr(view,key),getattr(report,key))
            self.assertEqual(getattr(view,key).status,"available")
        # Old metadata alone cannot establish a new verification.
        self.assertEqual(settled_report(rows,meta,now_ms=T+60000).last.reason,"stale_metadata")
        due=T+4*HOUR
        self.assertEqual(settled_view(report,now_ms=due-1).last.status,"available")
        self.assertEqual(settled_view(report,now_ms=due).last.reason,"settlement_pending")
        self.assertEqual(settled_view(report,now_ms=due+GRACE).last.reason,"expired")

    def test_current_delta_three_minute_old_snapshot_fresh_estimate(self):
        rows=history(4)
        meta=metadata(rows)
        report=settled_report(rows,meta,now_ms=T+10000)
        now=T+190000
        fresh=replace(estimate(meta),source_ms=now-2000)
        result=current_delta(fresh,report,now_ms=now)
        self.assertEqual(result.status,"available")
        self.assertEqual(result.expires_at_ms,fresh.source_ms+120000)
        self.assertEqual(current_delta(replace(fresh,source_ms=now-120000),report,now_ms=now).reason,"stale_estimate")
        self.assertEqual(current_delta(fresh,replace(report,verified_at_ms=None),now_ms=now).reason,"settlement_unverified")
        self.assertEqual(current_delta(replace(fresh,mapping_version="new"),report,now_ms=now).reason,"cycle_identity_mismatch")

    def test_epoch_zero_and_changed_task_immediately_claimable(self):
        queue=RepairQueue(self.store,"jobs",1)
        key=("BTC","oi","1H")
        queue.enqueue(key,expires_ms=1000,importance=1,now_ms=0,token=1)
        first=queue.claim(now_ms=0,lease_ms=60000,token=1)[0]
        queue.enqueue(key,expires_ms=900,importance=2,now_ms=1,token=1)
        self.assertFalse(queue.ack(key,0,1,now_ms=1))
        self.assertFalse(queue.ack(key,True,1,now_ms=1))
        self.assertFalse(queue.ack(*first,1,now_ms=1))
        next_claim=queue.claim(now_ms=1,lease_ms=100,token=1)[0]
        self.assertTrue(queue.ack(*next_claim,1,now_ms=2))

    def test_overdue_importance_does_not_reverse(self):
        queue=RepairQueue(self.store,"jobs",1)
        for key,expiry,weight in ((("A","oi","1H"),0,1000),(("B","oi","1H"),90,1),(("C","oi","1H"),101,1000)):
            queue.enqueue(key,expires_ms=expiry,importance=weight,now_ms=0,token=1)
        claims=queue.claim(now_ms=100,lease_ms=100,token=1,batch=3)
        self.assertEqual([key[0] for key,_ in claims],["A","B","C"])

    def test_builder_rotation_and_per_row_failure_quarantine(self):
        builder=IncrementalBuilder(self.store,"rows",3,1)
        for _ in range(5):
            builder.mark(0,1)
            builder.build(record,1,now_ms=100,batch=1)
        self.assertIsNotNone(builder.read(1,now_ms=100))
        self.assertIsNotNone(builder.read(2,now_ms=100))
        for row in (0,1):
            builder.mark(row,1)
        self.assertFalse(builder.build(lambda row:record(row,0 if row==0 else 2),1,now_ms=100))
        self.assertEqual(builder.failure(0)[1],"input_version_regression")
        self.assertIsNone(builder.read(0,now_ms=100))
        self.assertEqual(builder.read(1,now_ms=100).versions,(("source",2),))
        builder.mark(0,1)
        self.assertTrue(builder.build(lambda row:record(row,2),1,now_ms=100))
        self.assertIsNone(builder.failure(0))

    def test_artifact_refresh_monotonic_and_stale_deadline_safe_and_bounded(self):
        artifacts=DeadlineArtifacts(self.store,"blobs",1,max_bytes=100,max_items=3)
        digest=artifacts.stage(b"same",expires_ms=200,deadline_ms=300,now_ms=100,token=1)
        for i in range(1000):
            self.assertEqual(artifacts.stage(b"same",expires_ms=201+i,deadline_ms=301+i,now_ms=100,token=1),digest)
        self.assertLessEqual(self.store.read_control(lambda state:len(state["blobs"]["deadlines"])),2)
        self.assertEqual(artifacts.read(digest,now_ms=400),b"same")
        self.assertEqual(artifacts.reap(now_ms=300,token=1),())
        artifacts.stage(b"same",expires_ms=200,deadline_ms=300,now_ms=100,token=1)
        self.assertEqual(artifacts.read(digest,now_ms=1199),b"same")
        self.assertEqual(artifacts.reap(now_ms=1300,token=1),(digest,))

    def test_full_queue_evicts_only_expired_unleased_item(self):
        queue=RepairQueue(self.store,"jobs",1,capacity=1)
        a,b=("A","oi","1H"),("B","oi","1H")
        queue.enqueue(a,expires_ms=10,importance=1,now_ms=0,token=1)
        claim=queue.claim(now_ms=0,lease_ms=100,token=1)[0]
        with self.assertRaises(MemoryError):
            queue.enqueue(b,expires_ms=200,importance=2,now_ms=11,token=1)
        queue.enqueue(b,expires_ms=200,importance=2,now_ms=100,token=1)
        self.assertFalse(queue.ack(*claim,1,now_ms=101))
        self.assertEqual(queue.claim(now_ms=100,lease_ms=100,token=1)[0][0],b)

    def test_late_hard_probe_blocks_new_probe_and_preserves_retry_after(self):
        for status in (403,418,429,451):
            circuit=CircuitBreaker(self.store,"provider"+str(status),1)
            circuit.reconcile(now_ms=0,token=1)
            _,first=circuit.allow(now_ms=0,token=1)
            _,second=circuit.allow(now_ms=10000,token=1)
            delay=circuit.outcome(now_ms=10001,token=1,status=status,draw=0,probe=first,retry_after_ms=30000)
            self.assertGreaterEqual(delay,30000)
            with self.assertRaises(ValueError):
                circuit.outcome(now_ms=10002,token=1,status=200,draw=0,probe=second)
            self.assertFalse(circuit.allow(now_ms=40000,token=1)[0])
            self.assertTrue(circuit.allow(now_ms=40001,token=1)[0])
        circuit=CircuitBreaker(self.store,"floor",1)
        circuit.reconcile(now_ms=0,token=1)
        _,probe=circuit.allow(now_ms=0,token=1)
        self.assertGreaterEqual(circuit.outcome(now_ms=10000,token=1,status=429,draw=0,probe=probe),1000)
