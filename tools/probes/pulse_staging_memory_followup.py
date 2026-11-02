"""Read-only shared-overhead diagnostics after the bounded allocation slope."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging


def numeric_fields(data):
    return {key:value for key,value in data.items()
            if type(value) in (int,float) or value is None}


def run(cli):
    import redis
    client,identity=connect_staging(cli,approved_plaintext=True)
    try:
        result={"namespace":"pulse:stg:","writes":0,"deletions":0,"exchange_calls":0}
        try:
            result["memory_stats"]=numeric_fields(client.memory_stats())
        except redis.ResponseError:
            result["memory_stats"]="provider_unavailable"
        for section in ("replication","persistence","clients","stats"):
            try:
                result[section]=numeric_fields(client.info(section))
            except redis.ResponseError:
                result[section]="provider_unavailable"
        observations=[]
        start=time.monotonic()
        for delay in (0,5,10,15):
            if delay:
                time.sleep(delay)
            info=client.info("memory")
            observations.append({"elapsed_s":round(time.monotonic()-start,3),
                                 "memory":numeric_fields(info),"dbsize":client.dbsize()})
        result["settled_observations"]=observations
        result["interpretation"]="diagnostic_only_no_scale_acceptance"
        return result
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(run(args.vercel_cli),indent=2))
    except Exception as error:
        print(json.dumps({"status":"diagnostic_failed","error_type":type(error).__name__}))
        raise SystemExit(1) from None
