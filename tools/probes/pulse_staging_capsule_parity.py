"""ADR-0059: bounded synthetic capsule parity, no exchange requests/deletion.

Exactly eight unpublished persistent staging frames, at most 500 KB payload/control
budget. Full GETs are parity evidence only, not runtime reader behaviour.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from capsule_reference import boundary_cases
from pulse_staging_gate import connect_staging
from pulse.store.staging_capsule import StagingCapsule
from pulse.store.staging_policy import MeasuredCapacity, staging_key


def run(cli, *, approved_plaintext=False, approved_plan_fallback=False):
    client, identity = connect_staging(cli,approved_plaintext=approved_plaintext)
    try:
        before = client.info("memory")
        capacity = MeasuredCapacity.from_info(before,provider_usable_bytes=125000000,
                                             approved_plan_fallback=approved_plan_fallback)
        if not capacity.admit(500000,reserve_bytes=1000000):
            raise RuntimeError("parity_memory_gate_denied")
        if before.get("maxmemory_policy") != "noeviction":
            raise RuntimeError("parity_eviction_policy_refused")
        seconds, micros = client.time()
        now = seconds*1000+micros//1000
        evidence=[]
        writes=0
        for name, reference, points in boundary_cases(now):
            store=StagingCapsule(client,uuid.uuid4().hex,approved_plan_fallback=approved_plan_fallback)
            store.acquire()
            store.initialise()
            # Seed fixture is unpublished and synthetic; a fenced test operation,
            # not history ingestion, source truth or a repair implementation.
            script="""if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return 0 end
            redis.call('SET',KEYS[1],ARGV[2]); return 1"""
            if client.eval(script,2,staging_key(store.frame),staging_key(store.lease),store.owner,reference.to_bytes()) != 1:
                raise RuntimeError("parity_fixture_fenced_out")
            statuses=[]
            for point in points:
                response=store.write(point)
                actual=response[0].decode("ascii")
                trusted_now=int(response[1]) if len(response)>1 else now
                expected=reference.write(point,now_ms=trusted_now)
                if actual != expected or client.get(staging_key(store.frame)) != reference.to_bytes():
                    raise RuntimeError("capsule_parity_failed_"+name+"_"+actual)
                if client.ttl(staging_key(store.frame)) != -1:
                    raise RuntimeError("persistent_ring_ttl_failed")
                result=store.read()
                if reference.state==b"Q" and result.get("reason")!="quarantined":
                    raise RuntimeError("conflict_was_readable")
                if reference.state==b"R" and result.get("status")!="available":
                    raise RuntimeError("ready_frame_unavailable")
                statuses.append(actual)
                writes+=1
            # Old owner must fail even on an otherwise valid exact retry.
            before_fence=client.get(staging_key(store.frame))
            if store.write(points[-1],owner=b"stale-owner")[0]!=b"fenced_out" or client.get(staging_key(store.frame))!=before_fence:
                raise RuntimeError("fencing_or_nonmutation_failed")
            evidence.append({"case":name,"frame_key":store.frame,"statuses":statuses,
                             "whole_capsule_sha256":hashlib.sha256(reference.to_bytes()).hexdigest(),
                             "ring_ttl":-1,"fenced_refusal_without_mutation":True})
        after=client.info("memory")
        return {"status":"synthetic_capsule_parity_passed_not_staging_acceptance",
                "execution":"operator_to_new_pulse_redis_not_hosted",
                "transport":identity["transport"],"capacity_source":capacity.capacity_source,
                "observed_maxmemory":before.get("maxmemory"),"ceiling_bytes":capacity.ceiling_bytes,
                "thresholds":capacity.thresholds,"used_memory_before":before.get("used_memory"),
                "used_memory_after":after.get("used_memory"),"eviction_policy":before.get("maxmemory_policy"),
                "namespace":"pulse:stg:","exchange_calls":0,"data_deletions":0,
                "persistent_frames":len(evidence),"sample_writes":writes,"cases":evidence,
                "staging_complete":False,
                "not_checked":["live_history_backfill","shared_quota_admission","hosted_bom1",
                               "sustained_cadence","process_kill","3_5_50_pairs","all_metrics"]}
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--approved-staging-plaintext",action="store_true")
    parser.add_argument("--approved-plan-capacity",action="store_true")
    args=parser.parse_args()
    try:
        print(json.dumps(run(args.vercel_cli,approved_plaintext=args.approved_staging_plaintext,
                             approved_plan_fallback=args.approved_plan_capacity),indent=2))
    except Exception as error:
        reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__
        print(json.dumps({"status":"parity_failed","reason":reason,"staging_complete":False}))
        raise SystemExit(1) from None
