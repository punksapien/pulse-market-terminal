"""ADR-0062 synthetic shared-capsule parity. No exchanges, deletes or production."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse_staging_gate import connect_staging
from metric_capsule_reference import MetricCapsuleReference
from pulse.store.capsule_model import CapsuleSpec, observation
from pulse.store.metric_capsule import MetricCapsule
from pulse.store.staging_policy import staging_key


def descriptors():
    return (
        CapsuleSpec("B-ETH_USDT","ETHUSDT","oi","quantity",60000,1536,900000,900000,True),
        CapsuleSpec("B-DOGE_USDT","DOGEUSDT","oi_cold","quantity",300000,320,900000,96000000,True),
        CapsuleSpec("B-1000PEPE_USDT","1000PEPEUSDT","ls_global","ratio_shares",300000,320,900000,96000000),
        CapsuleSpec("B-BTC_USDT","BTCUSDT","funding_settled","fraction",60000,9,900000,96000000),
        CapsuleSpec("B-XAU_USDT","XAUUSDT","funding_estimate","fraction",60000,9,120000,120000),
        CapsuleSpec("B-ETH_USDT","ETHUSDT","volatility","percent",60000,9,900000,900000),
    )


def vectors(spec,now):
    anchor=(now//spec.step_ms-1)*spec.step_ms+100
    if spec.metric=="funding_estimate" or spec.max_ingress_age_ms<now-anchor+1000:
        anchor=now-1000
    initial=("1.20","0.6","0.4") if spec.columns==3 else (("-0.0001",) if spec.allow_negative else ("100.00",))
    changed=("1.21","0.6","0.4") if spec.columns==3 else (("0.0002",) if spec.allow_negative else ("101",))
    return tuple(observation(spec,stamp,source) for stamp,source in (
        (anchor,initial),(anchor,initial),(anchor,changed),(anchor+100,changed),
        (anchor-50,initial),(anchor+200,initial)))


def run(cli,*,approved_plaintext=False,approved_plan_fallback=False):
    client,identity=connect_staging(cli,approved_plaintext=approved_plaintext)
    evidence=[]
    try:
        for spec in descriptors():
            store=MetricCapsule(client,uuid.uuid4().hex,spec,approved_plan_fallback=approved_plan_fallback)
            case={"pair_id":spec.pair_id,"metric":spec.metric,"columns":spec.columns,
                  "frame_key":store.frame,"bytes":spec.nbytes,"statuses":[]}
            evidence.append(case)
            store.acquire()
            store.initialise()
            store.renew()
            seconds,micros=client.time()
            now=seconds*1000+micros//1000
            reference=MetricCapsuleReference(spec)
            statuses=[]
            for point in vectors(spec,now):
                response=store.write(point)
                actual=response[0].decode("ascii")
                expected=reference.write(point,now_ms=int(response[1]) if len(response)>1 else now)
                if actual!=expected or client.get(staging_key(store.frame))!=reference.to_bytes():
                    raise RuntimeError("shared_capsule_parity_failed:"+spec.metric+":"+actual)
                result=store.read()
                if reference.state==b"Q":
                    if result.get("reason")!="quarantined":
                        raise RuntimeError("conflict_readable")
                elif result.get("status")!="available" or result["sources"]!=point.sources:
                    # A duplicate or dropped older point need not be the current source.
                    from pulse.store.capsule_model import decode_derived
                    current,_=decode_derived(spec,reference.derived)
                    if result.get("status")!="available" or result["sources"]!=current.sources:
                        raise RuntimeError("shared_capsule_read_failed")
                statuses.append(actual)
                case["statuses"]=list(statuses)
            before=client.get(store.frame)
            point=vectors(spec,now)[-1]
            selected=store.baseline(point.source_ms,spec.step_ms)
            if selected.get("source_ms")!=point.source_ms or selected.get("values")!=point.values:
                raise RuntimeError("bounded_baseline_wrong")
            if store.baseline(point.source_ms-100,spec.step_ms).get("reason")!="discarded_baseline_candidate":
                raise RuntimeError("discarded_baseline_guard_failed")
            case["bounded_baseline_and_discard_guard"]=True
            import struct
            from pulse.store.capsule_model import source_bytes
            result=client.evalsha(store._script("capsule_write.lua"),2,store.frame,store.lease,b"old-owner",
                point.source_ms,struct.pack("<"+"d"*spec.columns,*point.values),source_bytes(point),
                spec.mapping,*spec.wire_config,point.expires_ms)
            if result!=[b"fenced_out"] or client.get(store.frame)!=before or client.ttl(store.frame)!=-1:
                raise RuntimeError("fence_or_lifecycle_failed")
            case.update({"statuses":statuses,"ttl":-1,
                "sha256":hashlib.sha256(before).hexdigest(),"stale_owner_refused":True,
                "memory":store.last_memory})
        return {"status":"synthetic_shared_capsule_parity_passed","transport":identity["transport"],
            "exchange_calls":0,"deletions":0,"namespace":"pulse:stg:","cases":evidence,
            "staging_complete":False,"hosted":False}
    except Exception as error:
        return {"status":"failed","reason":str(error) if isinstance(error,RuntimeError) else type(error).__name__,
                "cases":evidence,"exchange_calls":0,"deletions":0,"staging_complete":False}
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--approved-staging-plaintext",action="store_true")
    parser.add_argument("--approved-plan-capacity",action="store_true")
    args=parser.parse_args()
    try:
        result=run(args.vercel_cli,approved_plaintext=args.approved_staging_plaintext,
            approved_plan_fallback=args.approved_plan_capacity)
        print(json.dumps(result,indent=2))
        if result["status"]=="failed":
            raise SystemExit(1)
    except Exception as error:
        print(json.dumps({"status":"failed","reason":str(error) if isinstance(error,RuntimeError) else type(error).__name__}))
        raise SystemExit(1) from None
