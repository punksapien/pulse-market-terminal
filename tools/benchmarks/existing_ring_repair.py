"""ADR-0188 existing-ring MemoryStore repair CPU/size bound, no network."""
import json
import statistics
import time
from pulse.store.memory_store import MemoryStore


def run():
    now=1800000000000;key="pulse:stg:offline:oi_cold"
    store=MemoryStore(100000);store.claim(1)
    store.create_ring(key,300000,320,1,1)
    before=store.used_bytes
    samples=[]
    for _ in range(25):
        start=time.perf_counter()
        for batch in range(0,320,16):
            for i in range(batch,batch+16):
                store.write_ring(key,now-(319-i)*300000,(100.,),1,now_ms=now)
        samples.append((time.perf_counter()-start)*1000)
    assert store.used_bytes==before
    for hours in (1,4,24):
        assert store.baseline(key,now-hours*3600000,300000)[1:]==(100.,)
    return dict(iterations=25,points=320,batches=20,max_batch=16,
        before_bytes=before,after_bytes=store.used_bytes,new_ring_bytes=0,
        median_ms=statistics.median(samples),p95_ms=sorted(samples)[23],
        exchange_calls=0,redis_calls=0,
        not_measured=["Redis native Lua time","network RTT","hosted availability"])


if __name__=="__main__":print(json.dumps(run(),indent=2))
