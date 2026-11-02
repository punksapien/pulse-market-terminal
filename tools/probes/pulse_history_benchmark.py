"""ADR-0126 synthetic retained-history cost; no Redis or exchange access."""
import json,sys,time
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from pulse.collectors.preview_history import prepare_histories
from pulse.collectors.preview_pair import stores
from pulse.store.bucket_ring import BucketRing
from pulse.store.capsule_model import footer
from pulse.wire.identity import digest

def run():
    now=1800000000000;universe=[];frames=[]
    for i in range(50):
        meta=dict(id=i,pair_id=f"B-T{i}_USDT",symbol=f"T{i}USDT",funding_hours=(1,4,8)[i%3]);universe.append(meta)
        ss=stores(None,"a"*32,meta)
        for store in (ss[1],ss[2]):
            spec=store.spec;ring=BucketRing(spec.step_ms,spec.capacity,spec.columns)
            for n in range(288):
                value=1.+i/100+n/10000
                ring.write(now-(287-n)*300000,(value,) if spec.columns==1 else (value,.6,.4),now_ms=now)
            frames.append(ring.to_bytes()+footer(spec,bytes(spec.derived_bytes),bytes(spec.coverage_bytes)))
    class Pipeline:
        def getrange(self,*args):pass
        def execute(self):return frames
    client=SimpleNamespace(pipeline=lambda **kw:Pipeline())
    publisher=SimpleNamespace(mapping_version=digest(universe),register_source=lambda name,body:(1,digest(body.decode())))
    start=time.perf_counter();packages=prepare_histories(client,"a"*32,universe,{},publisher,now_ms=now)
    return dict(status="synthetic_offline",pairs=50,points_per_ring=288,ring_read_bytes=sum(map(len,frames)),
        worker_ms=round((time.perf_counter()-start)*1000,2),allocation_bytes=sum(p.allocation_bytes for p in packages),
        buckets=[{key:variant["bytes"] for key,variant in json.loads(p.pointer)["variants"].items()} for p in packages],
        table_bytes_added=0,table_requests_added=0,exchange_calls=0,redis_commands=0)

if __name__=="__main__":print(json.dumps(run(),indent=2))
