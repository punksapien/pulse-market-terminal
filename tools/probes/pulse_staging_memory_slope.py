"""ADR-0060: exactly 8 existing +24 new synthetic frames; no deletes/source calls."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse.store.staging_policy import staging_key,MeasuredCapacity
from pulse.store.one_pair_oi import CAPSULE_BYTES


def slope(before,after):
    count=after["frames"]-before["frames"]
    if count<=0:
        raise ValueError("invalid_slope_counts")
    delta=after["used_memory_median"]-before["used_memory_median"]
    lower=(min(after["used_memory_samples"])-max(before["used_memory_samples"]))/count
    upper=(max(after["used_memory_samples"])-min(before["used_memory_samples"]))/count
    per_frame=delta/count
    # Non-positive increments and broad bounds indicate confounded/delayed INFO.
    conclusive=lower>0 and upper-lower<=CAPSULE_BYTES*.1
    return {"from":before["frames"],"to":after["frames"],"delta_bytes":delta,
            "bytes_per_added_frame":per_frame,"raw_ratio":per_frame/CAPSULE_BYTES,
            "lower_per_frame":lower,"upper_per_frame":upper,
            "conclusive":conclusive,"passes_1_3x":conclusive and upper<=CAPSULE_BYTES*1.3}


def observe(client,keys):
    import redis
    sizes=[]
    for key in keys:
        key=staging_key(key)
        if client.strlen(key)!=CAPSULE_BYTES or client.ttl(key)!=-1:
            raise RuntimeError("memory_fixture_identity_failed")
        try:
            sizes.append(client.memory_usage(key))
        except redis.ResponseError:
            sizes.append(None)
    samples=[]
    for _ in range(5):
        info=client.info("memory")
        if type(info.get("used_memory")) is not int or info.get("maxmemory_policy")!="noeviction":
            raise RuntimeError("memory_observation_refused")
        samples.append({"used_memory":info["used_memory"],"used_memory_lua":info.get("used_memory_lua"),
                        "used_memory_rss":info.get("used_memory_rss"),"dbsize":client.dbsize()})
        time.sleep(.25)
    used=[row["used_memory"] for row in samples]
    return {"frames":len(keys),"used_memory_median":statistics.median(used),
            "used_memory_samples":used,"samples":samples,"memory_usage_by_frame":sizes}


def run(cli,*,approved_plaintext=False,approved_plan_fallback=False):
    client,identity=connect_staging(cli,approved_plaintext=approved_plaintext)
    try:
        manifest=json.loads((ROOT/"docs/evidence/pulse_staging_capsule_parity_2026-10-26.json").read_text())
        keys=[staging_key(row["frame_key"]) for row in manifest["cases"]]
        if len(keys)!=8 or len(set(keys))!=8:
            raise RuntimeError("invalid_eight_frame_manifest")
        info=client.info("memory")
        capacity=MeasuredCapacity.from_info(info,provider_usable_bytes=125000000,
                                          approved_plan_fallback=approved_plan_fallback)
        if info.get("maxmemory_policy")!="noeviction" or not capacity.admit(24*CAPSULE_BYTES,reserve_bytes=1000000):
            raise RuntimeError("memory_slope_admission_denied")
        owner=uuid.uuid4().hex
        lock=staging_key("pulse:stg:experiments:memory-slope")
        if not client.set(lock,owner,nx=True,ex=120):
            raise RuntimeError("memory_slope_experiment_busy")
        template=client.get(keys[0])
        if len(template)!=CAPSULE_BYTES:
            raise RuntimeError("invalid_template_length")
        # Warm diagnostics; no script load, flush, or policy mutation.
        client.info("memory")
        stages=[observe(client,keys)]
        added=[]
        for target in (16,32):
            capacity=MeasuredCapacity.from_info(client.info("memory"),provider_usable_bytes=125000000,
                                               approved_plan_fallback=approved_plan_fallback)
            if not capacity.admit((target-len(keys))*CAPSULE_BYTES,reserve_bytes=1000000):
                raise RuntimeError("memory_slope_admission_denied")
            for index in range(len(keys),target):
                if client.get(lock)!=owner.encode("ascii") or client.pttl(lock)<=0:
                    raise RuntimeError("memory_slope_lock_lost")
                key=staging_key("pulse:stg:{"+owner+"}:memory:"+str(index))
                if not client.set(key,template,nx=True):
                    raise RuntimeError("memory_fixture_collision")
                keys.append(key)
                added.append(key)
            time.sleep(1)
            stages.append(observe(client,keys))
        slopes=[slope(stages[i],stages[i+1]) for i in range(2)]
        return {"status":"measured_not_scale_acceptance","namespace":"pulse:stg:",
                "transport":identity["transport"],"capacity_source":capacity.capacity_source,
                "raw_capsule_bytes":CAPSULE_BYTES,"stages":stages,"slopes":slopes,
                "strict_1_3x_gate_passed":all(row["passes_1_3x"] for row in slopes),
                "new_persistent_frames":len(added),"new_frame_keys":added,
                "exchange_calls":0,"deletions":0,"script_loads":0,
                "quiet_instance_proven":False,"fifty_pair_run_authorised_by_result":False}
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
        print(json.dumps({"status":"memory_slope_failed","error_type":type(error).__name__,
                          "reason":str(error) if isinstance(error,RuntimeError) else "details_redacted"}))
        raise SystemExit(1) from None
