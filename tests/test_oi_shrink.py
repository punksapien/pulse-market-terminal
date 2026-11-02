import importlib.util,random,unittest,zlib,struct
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch,Mock
from pulse.store.oi_shrink import shrink,specs
from pulse.store.bucket_ring import BucketRing,BucketCollision,DiscardedBaseline
from pulse.store.capsule_model import empty_capsule,footer,observation

path=Path(__file__).resolve().parents[1]/"tools/probes/metric_capsule_reference.py"
modspec=importlib.util.spec_from_file_location("shrink_reference",path)
mod=importlib.util.module_from_spec(modspec);modspec.loader.exec_module(mod)


class ShrinkTests(unittest.TestCase):
    def test_activation_is_explicit_preview_release_only(self):
        from pulse.collectors.preview_pair import stores
        for environment,release,approval,capacity in (("preview","1","1",300),
                ("production","1","1",1536),("preview","0","1",1536),("preview","1","0",1536)):
            with patch.dict('os.environ',{'VERCEL_ENV':environment,'PULSE_RELEASE_TOP350':release,
                    'PULSE_OWN_OI_300_APPROVED':approval}):
                store=stores(Mock(),'a'*32,{'pair_id':'B-ETH_USDT','symbol':'ETHUSDT'})[0]
                self.assertEqual(store.spec.capacity,capacity)

    def test_approved_production_preserves_the_migrated_descriptor(self):
        from pulse.collectors.preview_pair import stores
        with patch.dict('os.environ',{'VERCEL_ENV':'production','PULSE_PRODUCTION_ENABLED':'1',
                'PULSE_UNIVERSE_MODE':'all','PULSE_RELEASE_TOP350':'1','PULSE_OWN_OI_300_APPROVED':'1'},clear=True):
            store=stores(Mock(),'a'*32,{'pair_id':'B-ETH_USDT','symbol':'ETHUSDT'})[0]
            self.assertEqual(store.spec.capacity,300)

    def test_empty_idempotent_and_sizes(self):
        old,new=specs("B-BTC_USDT","BTCUSDT")
        result=shrink(empty_capsule(old),old.pair_id,old.symbol)
        self.assertEqual(result,empty_capsule(new));self.assertEqual((old.nbytes,new.nbytes),(25152,5068))
        self.assertEqual(shrink(result,old.pair_id,old.symbol),result)

    def test_wrapped_collision_conflict_and_baseline_parity(self):
        old,new=specs("B-ETH_USDT","ETHUSDT");ref=mod.MetricCapsuleReference(old)
        for i in range(1700):
            t=i*60000+100
            ref.write(observation(old,t,(str(i+1),)),now_ms=t)
            if i%31==0:ref.write(observation(old,t+100,(str(i+2),)),now_ms=t+100)
            if i%47==0:ref.write(observation(old,t,(str(i+3),)),now_ms=t)
        raw=ref.to_bytes();result=shrink(raw,old.pair_id,old.symbol)
        a=BucketRing.from_bytes(raw[:old.ring_bytes]);b=BucketRing.from_bytes(result[:new.ring_bytes])
        def lookup(r,t,e):
            try:return r.baseline(t,e)
            except (BucketCollision,DiscardedBaseline) as exc:return type(exc).__name__,str(exc)
        for i in range(1401,1700):
            for jitter in (0,50,100,150,1000,59999):
                self.assertEqual(lookup(a,i*60000+jitter,60000),lookup(b,i*60000+jitter,60000))
        self.assertIsNone(b.baseline(1300*60000,60000))
        self.assertEqual(a.collision_buckets,b.collision_buckets)
        self.assertEqual(raw[old.ring_bytes+32:old.ring_bytes+32+old.derived_bytes],
            result[new.ring_bytes+32:new.ring_bytes+32+new.derived_bytes])

    def test_refuse_bad_identity_crc_partial_state_and_length(self):
        old,new=specs("B-ETH_USDT","ETHUSDT");raw=empty_capsule(old)
        variants=[raw[:-1],raw[:60]+b"bad!"+raw[64:]]
        for offset,value in ((8,ord('W')),(16,99),(10,3)):
            data=bytearray(raw);data[old.ring_bytes+offset]=value;variants.append(bytes(data))
        for data in variants:
            with self.assertRaises(ValueError):shrink(data,old.pair_id,old.symbol)
        with self.assertRaises(ValueError):shrink(raw,"B-BTC_USDT","BTCUSDT")

    def test_quarantine_and_dirty_state_are_not_healed(self):
        old,new=specs("B-ETH_USDT","ETHUSDT");raw=empty_capsule(old)
        for state in (b"R",b"Q"):
            data=raw[:old.ring_bytes]+footer(old,bytes(old.derived_bytes),bytes(old.coverage_bytes),state,1)
            result=shrink(data,old.pair_id,old.symbol)
            self.assertEqual(result[new.ring_bytes+8:new.ring_bytes+10],state+b"\x01")
