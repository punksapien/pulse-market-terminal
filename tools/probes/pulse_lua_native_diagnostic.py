"""Read-only native timing visibility; never print/persist command arguments."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_cohort_sources import write
from pulse_lua_budget import instrument
from pulse_cohort_bootstrap import CURSOR
from pulse.store.metric_capsule import ACQUIRE,RENEW,INITIALISE


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.parent!=ROOT/"docs/evidence" or output.exists(): raise SystemExit("invalid_evidence_path")
    client,_=connect_staging(args.vercel_cli,approved_plaintext=True)
    result={"redis_data_writes":0,"exchange_calls":0,"deletions":0,
            "internal_time_wrapper_assessment":"invalid_all_zero_not_latency_proof"}
    known={hashlib.sha1(instrument(p.read_bytes()).encode()).hexdigest():p.name
           for p in (ROOT/"pulse/store/lua").glob("*.lua")}
    for name,body in (("acquire",ACQUIRE),("renew",RENEW),("initialise",INITIALISE),("cursor",CURSOR)):
        known[hashlib.sha1(instrument(body).encode()).hexdigest()]=name
    try:
        for section in ("commandstats","latencystats"):
            try:
                data=client.info(section)
                result[section]={k:v for k,v in data.items() if "eval" in k.lower()}
            except Exception as error:
                result[section]={"unavailable":type(error).__name__}
        try:
            result["slowlog_threshold_config"]=client.config_get("slowlog-log-slower-than")
        except Exception as error:
            result["slowlog_threshold_config"]={"unavailable":type(error).__name__}
        try:
            rows=[]
            for record in client.slowlog_get(64):
                command=record.get("command",b"")
                parts=command.split(None,2)
                if len(parts)>1 and parts[0].upper()==b"EVALSHA" and parts[1].decode("ascii",errors="ignore") in known:
                    rows.append({"script":known[parts[1].decode()],"duration_us":record["duration"],"id":record["id"]})
            result["matching_native_slowlog_samples"]=rows
        except Exception as error:
            result["matching_native_slowlog_samples"]={"unavailable":type(error).__name__}
        result["status"]="native_visibility_recorded_not_per_script_budget_proof"
    finally:
        write(output,result);client.close()
    print(json.dumps(result))
