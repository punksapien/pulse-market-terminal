"""ADR-0064 real-data OI increments on new Pulse staging only; no publication."""
import argparse
from dataclasses import asdict
from decimal import Decimal,localcontext
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse.collectors.binance_oi import fetch_current,fetch_history
from pulse.collectors.oi_history import history_spec,build_history_ring
from pulse.metrics.open_interest import OIObservation,WINDOW_MS
from pulse.metrics.oi_selection import change_with_baselines
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.capsule_model import CapsuleSpec
from pulse.store.metric_capsule import MetricCapsule
from pulse.store.shared_admission import RollingLimit
from pulse.store.expansion_guard import guard


def raw_reference(current,rows,horizon):
    target=current["time"]-WINDOW_MS[horizon]
    eligible=[row for row in rows if 0<=target-int(row["timestamp"])<=300000]
    if not eligible:
        return {"status":"unavailable","reason":"baseline_missing"}
    baseline=max(eligible,key=lambda row:int(row["timestamp"]))
    before=Decimal(baseline["sumOpenInterest"])
    if before==0:
        return {"status":"unavailable","reason":"zero_baseline"}
    with localcontext() as context:
        context.prec=60
        value=(Decimal(current["openInterest"])-before)/before
    return {"status":"available","value_fraction_exact":str(value),"baseline":baseline,
            "baseline_error_ms":target-int(baseline["timestamp"])}


def ensure_lease(store):
    if store.owner is not None:
        try:
            store.renew()
            return
        except RuntimeError as error:
            if str(error)!="capsule_fenced_out":
                raise
    store.acquire()
    store.initialise()


def run(cli,*,symbols,stages,scope,run_id,ca_file,checkpoint=None):
    client,identity=connect_staging(cli,approved_plaintext=True)
    result={"status":"running","execution":"operator_to_staging_not_hosted","run_id":run_id,
            "scope":scope,"keys":[],"pairs":[],"stages":[],"exchange_attempts":0,
            "production_access":False,"deletions":0,"staging_complete":False}
    def clock():
        seconds,micros=client.time()
        return seconds*1000+micros//1000
    stores={}
    try:
        result["memory_baseline"]=client.info("memory")
        guard(result["memory_baseline"],approved_plan_fallback=True)
        admission=BinanceAdmission(client,scope,
            {"history":RollingLimit(700,300000),"weight":RollingLimit(1680,60000)},
            approved_plan_fallback=True,approved_bootstrap=True)
        result["quota_keys"]=admission.keys
        for count in stages:
            started=time.perf_counter()
            for symbol in symbols[:count]:
                pair="B-"+symbol[:-4]+"_USDT"
                if symbol not in stores:
                    own_spec=CapsuleSpec(pair,symbol,"oi","quantity",60000,1536,900000,900000,True)
                    cold_spec=history_spec(pair,symbol)
                    own=MetricCapsule(client,run_id,own_spec,approved_plan_fallback=True)
                    cold=MetricCapsule(client,run_id,cold_spec,approved_plan_fallback=True)
                    stores[symbol]=(own,cold)
                    result["keys"].extend((own.frame,own.lease,own.fence,cold.frame,cold.lease,cold.fence))
                    if checkpoint:
                        checkpoint(result)  # Persist exact keys before their first write.
                own,cold=stores[symbol]
                ensure_lease(own); ensure_lease(cold)
                raw={}
                result["exchange_attempts"]+=1
                current=fetch_current(own.spec,admission,clock=clock,ca_file=ca_file,
                                      evidence_sink=lambda data:raw.update(current=data))
                current_headers=dict(admission.last_feedback)
                write=own.write(current)
                if write[0] not in (b"written",b"duplicate"):
                    raise RuntimeError("real_current_write_refused:"+write[0].decode("ascii"))
                result["exchange_attempts"]+=1
                points=fetch_history(cold.spec,admission,clock=clock,ca_file=ca_file,
                                     evidence_sink=lambda data:raw.update(history=data))
                history_headers=dict(admission.last_feedback)
                build_history_ring(points,cold.spec,now_ms=clock())
                for offset in range(0,len(points),16):
                    cold.write_many(points[offset:offset+16])
                current_view=own.read()
                if current_view.get("source_ms")!=current.source_ms or current_view.get("values")!=current.values:
                    raise RuntimeError("real_current_roundtrip_mismatch")
                changes={}
                for horizon,window in WINDOW_MS.items():
                    target=current.source_ms-window
                    own_row=own.baseline(target,60000)
                    cold_row=cold.baseline(target,300000)
                    ordinary={"baseline_missing","discarded_baseline_candidate","exact_timestamp_conflict","quarantined"}
                    for row in (own_row,cold_row):
                        if row["status"]!="available" and row["reason"] not in ordinary:
                            raise RuntimeError("real_baseline_integrity_refused:"+row["reason"])
                    def convert(row):
                        return OIObservation(row["source_ms"],row["values"][0],pair) if row["status"]=="available" else None
                    change=change_with_baselines(OIObservation(current.source_ms,current.values[0],pair),
                        convert(own_row),convert(cold_row),horizon=horizon,now_ms=clock())
                    expected=raw_reference(raw["current"],raw["history"],horizon)
                    # This increment has no invented historical own observations.
                    if change.mode=="cold-start":
                        if change.status!=expected["status"]:
                            raise RuntimeError("raw_history_availability_mismatch")
                        if change.status=="available" and (
                            change.baseline_asof_ms!=int(expected["baseline"]["timestamp"])
                            or not math.isclose(change.value_fraction,float(expected["value_fraction_exact"]),rel_tol=1e-12,abs_tol=1e-15)):
                            raise RuntimeError("raw_history_numeric_mismatch")
                    changes[horizon]={"computed":asdict(change),"raw_reference":expected,
                                      "own_selection":own_row,"cold_selection":cold_row}
                memory=client.info("memory")
                guard(memory,approved_plan_fallback=True)
                report={"symbol":symbol,"pair_id":pair,"stage":count,"history_rows":len(points),
                        "current_headers":current_headers,"history_headers":history_headers,
                        "current":raw["current"],"changes":changes,"memory":memory,
                        "raw_history":raw["history"],"raw_history_sha256":hashlib.sha256(
                            json.dumps(raw["history"],sort_keys=True,separators=(",",":")).encode()).hexdigest(),
                        "ring_ttls":[client.ttl(own.frame),client.ttl(cold.frame)]}
                result["pairs"].append(report)
                if checkpoint:
                    checkpoint(result)
                print(json.dumps({"progress":"pair_verified","stage":count,"symbol":symbol,
                    "modes":{h:changes[h]["computed"]["mode"] for h in changes},"used_memory":memory["used_memory"]}),flush=True)
            memory=client.info("memory")
            guard(memory,approved_plan_fallback=True)
            result["stages"].append({"pairs":count,"elapsed_seconds":time.perf_counter()-started,
                "memory":memory,"raw_ring_bytes":sum(a.spec.nbytes+b.spec.nbytes for a,b in stores.values())})
            if checkpoint:
                checkpoint(result)
        result.update(status="real_oi_stages_verified",memory_final=client.info("memory"),
                      transport=identity["transport"],capacity_503_pairs="open")
        return result
    except Exception as error:
        result.update(status="stopped",reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__)
        try:
            result["memory_final"]=client.info("memory")
        except Exception:
            result["memory_final"]={"status":"unavailable"}
        return result
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--ca-file",required=True)
    parser.add_argument("--scope",default="operator-office-c1-20261026")
    parser.add_argument("--run-id",default=uuid.uuid4().hex)
    parser.add_argument("--symbols",nargs="+",default=["BTCUSDT"])
    parser.add_argument("--stages",nargs="+",type=int,default=[1])
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    path=Path(args.output).resolve()
    if path.parent!=ROOT/"docs/evidence" or any(n<1 or n>len(args.symbols) or n>50 for n in args.stages):
        raise SystemExit("invalid evidence path/stages")
    def checkpoint(result):
        # Generated public-market evidence, not application source/credentials.
        temporary=path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result,indent=2)+"\n")
        temporary.replace(path)
    result=run(args.vercel_cli,symbols=args.symbols,stages=args.stages,scope=args.scope,
               run_id=args.run_id,ca_file=args.ca_file,checkpoint=checkpoint)
    checkpoint(result)
    print(json.dumps({"status":result["status"],"reason":result.get("reason"),"file":str(path),
                      "exchange_attempts":result["exchange_attempts"],"stages":[s["pairs"] for s in result["stages"]]}))
    if result["status"]=="stopped":
        raise SystemExit(1)
