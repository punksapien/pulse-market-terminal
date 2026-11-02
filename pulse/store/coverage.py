"""ADR-0054: rolling coverage sidecar; never a baseline selector.

C1 must update it in the same fenced script as its ring. Missing sidecar is
unknown coverage until rebuilt; this module alone is not a Redis writer.
"""
from .recovery import integer, name
from .bucket_ring import WriteStatus


class CoverageBitmap:
    def __init__(self,store,namespace,capacity,token):
        if not name(namespace) or not integer(capacity,1) or capacity>65536:
            raise ValueError("invalid_coverage_capacity")
        self.store,self.namespace,self.capacity=store,namespace,capacity
        self.mask=(1<<capacity)-1
        def init(state):
            data=state.setdefault(namespace,{"capacity":capacity,"latest":-1,"valid":0})
            if data["capacity"]!=capacity:
                raise ValueError("coverage_identity_mismatch")
        store.atomic_control(token,init)

    @property
    def packed_budget_bytes(self):
        return 16+(self.capacity+7)//8

    def observe(self,bucket,*,status,token):
        if not integer(bucket) or not isinstance(status,WriteStatus):
            raise ValueError("invalid_bucket_outcome")
        def update(state):
            data=state[self.namespace]
            if status in (WriteStatus.DUPLICATE,WriteStatus.DROPPED_OLDER):
                return
            if bucket<=data["latest"]-self.capacity:
                raise ValueError("outside_retention")
            advance=max(bucket-data["latest"],0)
            if advance>=self.capacity:
                data["valid"]=0
            elif advance:
                data["valid"]=(data["valid"]<<advance)&self.mask
            data["latest"]=max(bucket,data["latest"])
            bit=1<<(data["latest"]-bucket)
            if status == WriteStatus.CONFLICT:
                data["valid"]&=~bit
            else:
                data["valid"]|=bit
        self.store.atomic_control(token,update)

    def count(self,first,last):
        if not integer(first) or not integer(last) or last<first or last-first+1>self.capacity:
            raise ValueError("invalid_coverage_window")
        def count(state):
            data=state.get(self.namespace)
            if not data:
                return None
            low=max(first,data["latest"]-self.capacity+1)
            high=min(last,data["latest"])
            if low>high:
                return (0,last-first+1)
            selection=((1<<(high-low+1))-1)<<(data["latest"]-high)
            return ((data["valid"] & selection).bit_count(),last-first+1)
        return self.store.read_control(count)
