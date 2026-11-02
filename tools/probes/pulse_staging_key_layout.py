"""ADR-0061/0129: equal-byte synthetic layouts under the configured memory stop."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse.collectors.one_pair_oi import CurrentOI
from pulse.store.staging_capsule import StagingCapsule
from pulse.store.staging_policy import staging_key
from pulse.store.one_pair_oi import CAPSULE_BYTES
from pulse.store.expansion_guard import guard, ExpansionStopped, STOP_BYTES


def sample(client,keys):
    _,observation=guard(client.info("memory"),approved_plan_fallback=True)
    pipe=client.pipeline(transaction=False)
    for key in keys:
        pipe.memory_usage(staging_key(key))
    usage=pipe.execute() if keys else []
    return dict(observation,objects=len(keys),memory_usage=usage,
                key_memory_total=sum(usage) if all(type(n) is int for n in usage) else None)


def run(cli):
    client,identity=connect_staging(cli,approved_plaintext=True)
    result={"status":"started","namespace":"pulse:stg:","stop_bytes":STOP_BYTES,
            "exchange_calls":0,"deletions":0,"tls_probes":0,"live_pairs":0,
            "attempted_frame_keys":[],"container_keys":[],"small_stages":[],
            "provider_memory_root_cause":"unresolved","capacity_503_pairs":"open"}
    try:
        guard(client.info("memory"),additional_bytes=2*32*CAPSULE_BYTES,approved_plan_fallback=True)
        owner=uuid.uuid4().hex
        lock=staging_key("pulse:stg:experiments:key-layout")
        if not client.set(lock,owner,nx=True,ex=300):
            raise RuntimeError("key_layout_experiment_busy")
        result["baseline"]=sample(client,[])
        clock=client.time()
        point=CurrentOI("BTCUSDT",clock[0]*1000+clock[1]//1000,"100",100.)
        frames=[]
        payloads=[]
        for index in range(32):
            if client.get(lock)!=owner.encode("ascii") or client.pttl(lock)<=0:
                raise RuntimeError("key_layout_lock_lost")
            store=StagingCapsule(client,uuid.uuid4().hex,approved_plan_fallback=True)
            result["attempted_frame_keys"].append(store.frame)
            store.acquire()
            store.initialise()
            response=store.write(point)
            if response[0]!=b"written":
                raise RuntimeError("real_lua_fixture_write_failed")
            payload=client.get(staging_key(store.frame))
            if len(payload)!=CAPSULE_BYTES or client.ttl(store.frame)!=-1:
                raise RuntimeError("frame_layout_failed")
            if store.read().get("quantity")!=100.:
                raise RuntimeError("frame_read_failed")
            payloads.append(payload)
            frames.append(store.frame)
            if index+1 in (1,8,16,32):
                result["small_stages"].append(sample(client,frames))
        joined=b"".join(payloads)
        result["small_group_raw_bytes"]=len(joined)
        result["small_group_sha256"]=hashlib.sha256(joined).hexdigest()
        result["before_large"]=sample(client,[])
        containers=[]
        for index in range(4):
            payload=b"".join(payloads[index*8:(index+1)*8])
            guard(client.info("memory"),additional_bytes=len(payload),approved_plan_fallback=True)
            key=staging_key("pulse:stg:{"+owner+"}:layout:large:"+str(index))
            result["container_keys"].append(key)
            script="""if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return 0 end
            return redis.call('SET',KEYS[1],ARGV[2],'NX') and 1 or 0"""
            if client.eval(script,2,key,lock,owner,payload)!=1:
                raise RuntimeError("container_creation_refused")
            guard(client.info("memory"),approved_plan_fallback=True)
            if client.get(key)!=payload or client.ttl(key)!=-1:
                raise RuntimeError("container_bytes_failed")
            containers.append(key)
        result["large_stage"]=sample(client,containers)
        result["large_group_raw_bytes"]=len(joined)
        result["large_group_sha256"]=hashlib.sha256(joined).hexdigest()
        result["status"]="structural_test_passed_not_50_pair_acceptance"
        result["real_lua_slot_writes"]=len(frames)
        result["not_checked"]=["live_50_pair_collection","shared_quota","history_backfill",
                               "general_metric_capsule","hosted_run","causal_memory_attribution"]
        return result
    except ExpansionStopped as error:
        return dict(result,status="stopped_memory_experiment",reason=str(error),stop_observation=error.observation)
    except Exception as error:
        return dict(result,status="experiment_failed",error_type=type(error).__name__,
                    reason=str(error) if isinstance(error,RuntimeError) else "details_redacted")
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.vercel_cli),indent=2))
