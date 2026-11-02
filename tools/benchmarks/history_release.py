"""ADR-0171 real codecs over synthetic bounded frames; no Redis or exchange calls."""
import json,time,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.collectors.preview_pair import stores
from pulse.collectors.preview_history import prepare_histories
from pulse.store.bucket_ring import BucketRing
from pulse.store.capsule_model import footer
from pulse.wire.identity import digest


def run():
    now=1800000000000;universe=[];frames={};reads=[]
    for i in range(310,350):
        meta=dict(id=i,pair_id="B-P"+str(i)+"_USDT",symbol="P"+str(i)+"USDT",funding_hours=4);universe.append(meta)
        ss=stores(None,"a"*32,meta)
        for store in (ss[1],ss[2]):
            s=store.spec;ring=BucketRing(s.step_ms,s.capacity,s.columns)
            for j in range(288):
                value=1000.+i+j/100 if s.columns==1 else (1.5,.6,.4)
                ring.write(now-(287-j)*300000,(value,) if s.columns==1 else value,now_ms=now)
            frames[store.frame]=ring.to_bytes()+footer(s,bytes(s.derived_bytes),bytes(s.coverage_bytes))
    class Pipe:
        def __init__(self):self.names=[]
        def getrange(self,key,start,end):self.names.append((key,start,end));return self
        def execute(self):
            result=[frames[k][a:b+1] for k,a,b in self.names];reads.extend(map(len,result));return result
    client=SimpleNamespace(pipeline=lambda **kw:Pipe())
    pub=SimpleNamespace(mapping_version=digest(universe),register_source=lambda name,body:(1,digest(json.loads(body))))
    started=time.perf_counter()
    packages=prepare_histories(client,"a"*32,universe,{},pub,now_ms=now,retention_ms=720000,include_identity=False)
    return dict(scope="offline_synthetic40_pairs_real_frame_decoder_and_codecs",elapsed_ms=round((time.perf_counter()-started)*1000,2),
        frame_read_bytes=sum(reads),buckets=len(packages),artifact_bytes=sum(p.allocation_bytes for p in packages),exchange_calls=0,redis_calls=0)


if __name__=="__main__":print(json.dumps(run()))
