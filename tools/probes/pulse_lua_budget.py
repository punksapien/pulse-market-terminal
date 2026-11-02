"""ADR-0072 small synthetic Lua timing, not market data or a cohort restart."""
import argparse
from collections import defaultdict
from email.utils import formatdate
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_cohort_sources import write
from pulse_cohort_bootstrap import CURSOR
from pulse.store.metric_capsule import MetricCapsule, ACQUIRE
from pulse.store.capsule_model import CapsuleSpec, observation
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.shared_admission import RollingLimit
from pulse.store.expansion_guard import guard


def instrument(body):
    if isinstance(body,bytes): body=body.decode()
    return ("local function measured()\n"+body+"\nend\nlocal t=redis.call('TIME'); "
        "local result=measured(); local u=redis.call('TIME'); "
        "return {result,(tonumber(u[1])-tonumber(t[1]))*1000000+tonumber(u[2])-tonumber(t[2])}")


class Timed:
    def __init__(self,client):
        self.client=client
        self.samples=defaultdict(list)
        self.labels={}
        self.cache={}
        for path in (ROOT/"pulse/store/lua").glob("*.lua"):
            self.labels[hashlib.sha1(path.read_bytes()).hexdigest()]=path.name

    def __getattr__(self,name): return getattr(self.client,name)

    def script_load(self,body):
        raw=body.encode() if isinstance(body,str) else body
        digest=hashlib.sha1(raw).hexdigest()
        if digest not in self.cache:
            sha=self.client.script_load(instrument(raw))
            self.cache[digest]=sha
            self.labels[sha]=self.labels.get(digest,"inline:"+digest)
        return self.cache[digest]

    def evalsha(self,sha,*args):
        result,duration=self.client.evalsha(sha,*args)
        if duration<0: raise RuntimeError("server_clock_regressed")
        self.samples[self.labels[sha]].append(duration)
        return result

    def eval(self,body,*args): return self.evalsha(self.script_load(body),*args)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.parent!=ROOT/"docs/evidence" or output.exists(): raise SystemExit("invalid_evidence_path")
    raw,_=connect_staging(args.vercel_cli,approved_plaintext=True)
    client=Timed(raw)
    run_id=uuid.uuid4().hex
    stores=[MetricCapsule(client,run_id,CapsuleSpec("B-BENCH_USDT","BENCHUSDT",metric,unit,step,capacity,900000,900000),
        approved_plan_fallback=True) for metric,unit,step,capacity in (
            ("oi","quantity",60000,1536),("ls_global","ratio_shares",300000,320))]
    prefix="pulse:stg:{"+run_id+"}:timing-cursor:"
    cursor,lease,fence=(prefix+s for s in ("frame","lease","fence"))
    admission=BinanceAdmission(client,"timing-"+run_id,
        {"history":RollingLimit(700,300000),"weight":RollingLimit(1680,60000)},
        approved_plan_fallback=True,approved_bootstrap=True)
    result={"status":"running","synthetic":True,"exchange_calls":0,"production_access":False,
            "keys":[key for store in stores for key in (store.frame,store.lease,store.fence)]+[cursor,lease,fence]+admission.keys}
    write(output,result)
    def clock():
        sec,micro=raw.time(); return sec*1000+micro//1000
    try:
        guard(raw.info("memory"),approved_plan_fallback=True)
        for store in stores:
            store.acquire(); store.initialise()
            for i in range(32):
                if i%8==0: store.renew()
                stamp=clock()
                sources=(str(10+i),) if store.spec.columns==1 else ("1.5","0.6","0.4")
                point=observation(store.spec,stamp,sources)
                if store.write(point)[0]!=b"written": raise RuntimeError("timing_write_refused")
                view=store.read()
                if view["values"]!=point.values: raise RuntimeError("timing_roundtrip_mismatch")
                if store.baseline(stamp,0)["values"]!=point.values: raise RuntimeError("timing_baseline_mismatch")
        owner=client.eval(ACQUIRE,2,lease,fence,uuid.uuid4().hex)
        for i in range(16):
            if client.eval(CURSOR,2,cursor,lease,owner,json.dumps({"fixture":i}))!=1:
                raise RuntimeError("timing_cursor_fenced_out")
        if admission.reserve({"history":0,"weight":1})[0]!=b"reserved": raise RuntimeError("timing_bootstrap_refused")
        admission.feedback(200,{"Date":formatdate(clock()/1000,usegmt=True),"X-MBX-USED-WEIGHT-1M":"1"})
        for i in range(16):
            if admission.reserve({"history":1,"weight":0})[0]!=b"reserved": raise RuntimeError("timing_admission_refused")
            admission.feedback(200,{"Date":formatdate(clock()/1000,usegmt=True)})
        timings={}
        for name,samples in client.samples.items():
            ordered=sorted(samples)
            timings[name]={"calls":len(samples),"p50_us":ordered[(len(samples)-1)//2],
                "p95_us":ordered[max(0,(95*len(samples)+99)//100-1)],"max_us":max(samples),
                "calls_at_least_1ms":sum(t>=1000 for t in samples)}
        valid=any(t>0 for samples in client.samples.values() for t in samples)
        result.update(status="timed_synthetic_bodies_completed" if valid else "timing_unresolved_clock_constant",
                      timing_method_valid=valid,timings=timings,memory=raw.info("memory"),
                      method="server TIME around exact body; wrapper+Redis call overhead included")
    except Exception as error:
        result.update(status="stopped",reason=str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__)
    finally:
        write(output,result); raw.close()
    print(json.dumps({k:v for k,v in result.items() if k not in ("keys","memory")}))
    if result["status"]!="timed_synthetic_bodies_completed": raise SystemExit(1)
