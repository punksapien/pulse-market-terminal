"""ADR-0107 offline MemoryStore benchmark. Synthetic RTT/I/O, NOT a Redis or exchange latency claim."""
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.store.memory_store import MemoryStore
from pulse.store.worker_state import WorkerState
from pulse.collectors.preview_batch import collect_batch
from pulse.collectors.preview_compose import prepare_funding
from pulse.wire.identity import digest


def main():
    memory=MemoryStore(1000000);memory.claim(1)
    names=[f"P{i}USDT" for i in range(50)]
    memory.atomic_control(1,lambda values:values.update({n:json.dumps({"instrument_ms":1800000000000,"sample":"x"*1000}).encode() for n in names}))
    class Client:
        trips=0
        def pipeline(self,transaction):
            client=self
            class Pipe:
                def __init__(self):self.names=[]
                def eval(self,script,count,key,name):self.names.append(name)
                def execute(self):
                    client.trips+=1;time.sleep(.001)
                    return memory.read_control(lambda values:[[b"available",values[n]] for n in self.names])
            return Pipe()
    client=Client();state=WorkerState(SimpleNamespace(prefix="pulse:stg:{"+"a"*32+"}:publication:",client=client))
    start=time.perf_counter()
    for name in names:
        time.sleep(.001);memory.read_control(lambda values:len(values[name]))
        time.sleep(.001);json.loads(memory.read_control(lambda values:values[name]))
    old=(time.perf_counter()-start)*1000
    start=time.perf_counter();state.read_many(names);new=(time.perf_counter()-start)*1000
    print(json.dumps({"benchmark":"retained_reads","modelled_rtt_ms":1,"old_trips":100,"new_trips":client.trips,"old_ms":old,"new_ms":new}))
    class Source:
        calls=0
        def clock(self):return 1800000000000
    def collect(meta,source,prior):
        time.sleep(.005);source.calls+=1
        value={"last_collected_ms":source.clock()}
        memory.atomic_control(1,lambda values:values.update({meta["symbol"]:value}))
        return value
    for workers in (1,8):
        start=time.perf_counter()
        result=collect_batch([{"symbol":n} for n in names],dict.fromkeys(names),collect=collect,make_source=Source,check_lease=lambda:None,workers=workers)
        print(json.dumps({"benchmark":"pair_workers","workers":workers,"synthetic_io_ms":5,"elapsed_ms":(time.perf_counter()-start)*1000,"collected":result["collected"]}))
    now=1800000000000
    universe=[dict(id=i,pair_id=f"B-P{i}_USDT",symbol=n,funding_hours=(1,4,8)[i%3]) for i,n in enumerate(names)]
    class Publisher:
        mapping_version=digest(universe)
        def register_source(self,name,body):return 1,"sha256:"+hashlib.sha256(body).hexdigest()
    records={m["symbol"]:{"instrument":{"pair":m["pair_id"],"quote_currency_short_name":"USDT","settle_currency_short_name":"USDT","funding_frequency":m["funding_hours"]},"instrument_ms":now} for m in universe}
    bulk={"ts":now,"prices":{m["pair_id"]:{"mkt":m["symbol"],"fr":"0.0001","efr":"-0.00002","bmST":now} for m in universe}}
    info={"source_ms":now,"data":{m["symbol"]:{"fundingIntervalHours":m["funding_hours"]} for m in universe}}
    premium={"source_ms":now,"data":{m["symbol"]:{"symbol":m["symbol"],"time":now,"nextFundingTime":now+m["funding_hours"]*3600000} for m in universe}}
    start=time.perf_counter()
    package=prepare_funding(universe,records,Publisher(),bulk,info,premium,now_ms=now)
    print(json.dumps({"benchmark":"funding_lane_cpu","pairs":50,"elapsed_ms":(time.perf_counter()-start)*1000,"prepared_bytes":package.allocation_bytes,"variants":json.loads(package.pointer)["variants"]}))


if __name__=="__main__":main()
