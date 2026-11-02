"""ADR-0199 deterministic adversarial model harness, not a deployed Redis parity claim."""
import random
from unittest import TestCase
from pulse.store.memory_store import MemoryStore
from pulse.store.recovery import DeadlineArtifacts,RepairQueue,IncrementalBuilder,DerivedRecord

class ReliabilityRaceTests(TestCase):
    def test_ten_seed_retention_interleavings_never_prune_referenced_bytes(self):
        for seed in range(10):
            rng=random.Random(seed);store=MemoryStore(65536);store.claim(1)
            artifacts=DeadlineArtifacts(store,'artifacts',1,max_bytes=4096,max_items=16)
            for now in range(300):
                payload=bytes([rng.randrange(8)])*32
                digest=artifacts.stage(payload,expires_ms=now+20,deadline_ms=now+40,now_ms=now,token=1)
                artifacts.retain(rng.choice(('active','previous','candidate')),(digest,),1)
                protected=store.read_control(lambda s:set(d for v in s['artifacts']['retained'].values() for d in v))
                removed=artifacts.reap(now_ms=now,token=1,batch=rng.randrange(1,5))
                self.assertTrue(protected.isdisjoint(removed))
                state=store.read_control(lambda s:s['artifacts'])
                self.assertTrue(protected.issubset(state['blobs']))
                self.assertEqual(state['bytes'],sum(len(v[0]) for v in state['blobs'].values()))
                self.assertLessEqual(state['bytes'],4096)
                self.assertLessEqual(len(state['deadlines']),2*max(1,len(state['blobs'])))
                for key,(_,expiry,_) in state['blobs'].items():
                    self.assertEqual(artifacts.read(key,now_ms=now) is None,now>=expiry)

    def test_changed_and_reclaimed_repair_never_accepts_an_old_ack(self):
        for seed in range(100):
            store=MemoryStore(65536);store.claim(1);queue=RepairQueue(store,'jobs',1)
            key=('B-BTC_USDT','oi','24H');now=seed*100
            queue.enqueue(key,expires_ms=now+1000,importance=1,now_ms=now,token=1)
            first=queue.claim(now_ms=now,lease_ms=10,token=1)[0]
            queue.enqueue(key,expires_ms=now+900,importance=2,now_ms=now+1,token=1)
            self.assertFalse(queue.ack(*first,1,now_ms=now+2))
            self.assertFalse(queue.ack(key,0,1,now_ms=now+2))
            second=queue.claim(now_ms=now+2,lease_ms=10,token=1)[0]
            third=queue.claim(now_ms=now+12,lease_ms=10,token=1)[0]
            self.assertFalse(queue.ack(*second,1,now_ms=now+13))
            self.assertTrue(queue.ack(*third,1,now_ms=now+13))

    def test_lease_handoff_mid_derive_cannot_commit_or_erase_the_dirty_update(self):
        store=MemoryStore(65536);store.claim(1);builder=IncrementalBuilder(store,'rows',2,1)
        def derive(row):
            if row==1:
                builder.mark(0,1);store.claim(2)
            return DerivedRecord(str(row).encode(),100,1000,(('source',1),))
        with self.assertRaises(PermissionError):builder.build(derive,1,now_ms=100)
        self.assertEqual(builder.dirty_count,2)
        self.assertIsNone(builder.read(0,now_ms=100))
        builder.build(lambda row:DerivedRecord(str(row).encode(),101,1000,(('source',2),)),2,now_ms=101)
        self.assertEqual(builder.dirty_count,0)
        self.assertEqual(builder.read(0,now_ms=101).versions,(('source',2),))
