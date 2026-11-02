"""ADR-0063 approved exact fixture manifest. No broad discovery or flush."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse.store.staging_policy import staging_key

FILES=("pulse_staging_capsule_parity_2026-10-26.json","pulse_memory_slope_2026-10-26.json",
       "pulse_key_layout_2026-10-26.json","pulse_shared_capsule_2026-10-26.json",
       "pulse_shared_admission_2026-10-26.json","pulse_source_control_2026-10-26.json")

COMPARE_UNLINK="""if string.sub(KEYS[1],1,10)~='pulse:stg:' or string.sub(KEYS[2],1,10)~='pulse:stg:' then return 'scope_refused' end
if redis.call('PTTL',KEYS[2])>=0 then return 'active_lease_refused' end
local value=redis.call('DUMP',KEYS[1])
if not value then return 'missing' end
if redis.sha1hex(value)~=ARGV[1] then return 'changed_value_refused' end
redis.call('UNLINK',KEYS[1])
return 'deleted'"""


def manifest():
    keys=set()
    def collect(value):
        if isinstance(value,dict):
            for child in value.values():
                collect(child)
        elif isinstance(value,list):
            for child in value:
                collect(child)
        elif isinstance(value,str) and value.startswith("pulse:stg:") and value!="pulse:stg:":
            keys.add(staging_key(value))
    for name in FILES:
        collect(json.loads((ROOT/"docs/evidence"/name).read_text()))
    for key in tuple(keys):
        if key.endswith(":frame"):
            keys.update((key[:-5]+"lease",key[:-5]+"fence"))
    if not keys or len(keys)>300 or any("*" in key or "pulse:prod:" in key for key in keys):
        raise ValueError("cleanup_manifest_refused")
    return sorted(keys)


def lease_for(key):
    if key.endswith((":frame",":fence",":lease")):
        return key.rsplit(":",1)[0]+":lease"
    # Memory/container fixtures had no per-key writer lease. Do not invent a scan.
    return key+":cleanup-lease-guard"


def expected_type(key):
    if ":source:" in key:
        suffix=key.rsplit(":",1)[1]
        if suffix in ("meta","control"):
            return b"hash"
        if suffix in ("history","weight"):
            return b"zset"
        raise ValueError("unexpected_cleanup_key_shape")
    return b"string"


def run(cli,*,apply=False,approved_plaintext=False):
    keys=manifest()
    client,identity=connect_staging(cli,approved_plaintext=approved_plaintext)
    result={"resource":"pulse-redis","environment":"preview","namespace":"pulse:stg:",
            "apply":apply,"exchange_calls":0,"keys":[],"unmanifested_keys":"not_discovered_or_deleted"}
    try:
        result["memory_before"]=client.info("memory")
        for start in range(0,len(keys),16):
            group=keys[start:start+16]
            pipe=client.pipeline(transaction=False)
            for key in group:
                pipe.type(key); pipe.memory_usage(key); pipe.pttl(lease_for(key))
            values=pipe.execute()
            for index,key in enumerate(group):
                kind,size,lease_ttl=values[3*index:3*index+3]
                if (kind not in (b"none",expected_type(key)) or lease_ttl>=0
                        or size is not None and size>524288):
                    raise RuntimeError("cleanup_candidate_validation_refused")
            pipe=client.pipeline(transaction=False)
            for key in group:
                pipe.dump(key)
            for key,dump in zip(group,pipe.execute()):
                if dump is not None and len(dump)>524288:
                    raise RuntimeError("cleanup_snapshot_size_refused")
                result["keys"].append({"key":key,"sha1":hashlib.sha1(dump).hexdigest() if dump else None,
                                       "outcome":"missing" if dump is None else "validated"})
        # No UNLINK has occurred before the complete candidate list passes.
        if apply:
            for start in range(0,len(result["keys"]),16):
                group=[row for row in result["keys"][start:start+16] if row["sha1"]]
                pipe=client.pipeline(transaction=False)
                for row in group:
                    pipe.eval(COMPARE_UNLINK,2,row["key"],lease_for(row["key"]),row["sha1"])
                for row,status in zip(group,pipe.execute()):
                    row["outcome"]=status.decode("ascii")
                if any(row["outcome"] not in ("deleted","missing") for row in group):
                    raise RuntimeError("cleanup_changed_or_busy_stop")
        result["memory_after"]=client.info("memory")
        result["deleted"]=sum(row["outcome"]=="deleted" for row in result["keys"])
        result["missing"]=sum(row["outcome"]=="missing" for row in result["keys"])
        result["status"]="cleanup_complete" if apply else "cleanup_dry_run"
        return result
    except Exception as error:
        result.update(status="cleanup_stopped",reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__)
        return result
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--apply-approved-manifest",action="store_true")
    parser.add_argument("--approved-staging-plaintext",action="store_true")
    args=parser.parse_args()
    result=run(args.vercel_cli,apply=args.apply_approved_manifest,approved_plaintext=args.approved_staging_plaintext)
    print(json.dumps(result,indent=2))
    if result["status"]=="cleanup_stopped":
        raise SystemExit(1)
