"""ADR-0062 generic whole-capsule oracle. Independent reviewed ring/coverage model."""
from pulse.store.bucket_ring import BucketRing, BucketCollision, WriteStatus
from pulse.store.coverage import CoverageBitmap
from pulse.store.memory_store import MemoryStore
from pulse.store.capsule_model import observation, derived_bytes, footer


class MetricCapsuleReference:
    def __init__(self,spec):
        self.spec=spec
        self.ring=BucketRing(spec.step_ms,spec.capacity,spec.columns)
        self.store=MemoryStore(100000)
        self.store.claim(1)
        self.coverage=CoverageBitmap(self.store,"coverage",spec.capacity,1)
        self.derived=bytes(spec.derived_bytes)
        self.current_time=None
        self.version=0
        self.state=b"R"
        self.dirty=0

    def write(self,point,*,now_ms):
        checked=observation(self.spec,point.source_ms,point.sources,expires_ms=point.expires_ms)
        if checked!=point:
            raise ValueError("source_value_mismatch")
        if point.source_ms>now_ms+5000:
            return "future_timestamp"
        if now_ms-point.source_ms>self.spec.max_ingress_age_ms:
            return "stale_timestamp"
        try:
            status=self.ring.write(point.source_ms,point.values,now_ms=now_ms)
        except BucketCollision:
            status=WriteStatus.CONFLICT
        if status==WriteStatus.DUPLICATE:
            return "duplicate"
        self.coverage.observe(point.source_ms//self.spec.step_ms,status=status,token=1)
        if status==WriteStatus.WRITTEN and (self.current_time is None or point.source_ms>self.current_time):
            self.current_time=point.source_ms
            self.version+=1
            self.derived=derived_bytes(self.spec,point,self.version)
            self.state=b"R"
        elif status==WriteStatus.CONFLICT and point.source_ms==self.current_time:
            self.state=b"Q"
        self.dirty=1
        return "exact_timestamp_conflict" if status==WriteStatus.CONFLICT else status.value

    def to_bytes(self):
        bits=self.store.read_control(lambda data:data["coverage"]["valid"])
        return self.ring.to_bytes()+footer(self.spec,self.derived,
            bits.to_bytes(self.spec.coverage_bytes,"little"),self.state,self.dirty)
