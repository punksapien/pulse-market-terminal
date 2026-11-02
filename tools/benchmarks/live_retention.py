"""ADR-0121 synthetic byte-retention model against MemoryStore; no Redis RAM claim."""
import json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.store.memory_store import MemoryStore

def measure(ttl):
    store=MemoryStore(10000000);store.claim(1)
    body=b"x"*50000
    started=time.perf_counter()
    for now in range(0,600000,2000):
        def update(values):
            for key in tuple(values):
                if values[key][0]<=now:del values[key]
            values[now]=(now+ttl,body)
        store.atomic_control(1,update)
    count,payload=store.read_control(lambda values:(len(values),sum(len(body) for expiry,body in values.values())))
    return dict(ttl_ms=ttl,update_ms=2000,frames=count,retained_payload_bytes=payload,
        simulated_updates=300,elapsed_ms=round((time.perf_counter()-started)*1000,3))

if __name__=="__main__":
    for ttl in (180000,60000,30000):print(json.dumps(measure(ttl)))
