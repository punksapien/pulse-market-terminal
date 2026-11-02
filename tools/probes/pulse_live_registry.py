"""ADR-0097 real50 metadata bootstrap, staging only; no financial artifacts yet."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
import re

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse.collectors.preview_sources import Sources
from pulse.collectors.preview_registry import bootstrap
from pulse.collectors.universe_registry import bootstrap_step


def run(cli,ca_file,run_id,*,all_pairs=False,max_steps=1,max_seconds=480):
    if (type(all_pairs) is not bool or type(max_steps) is not int or not 1<=max_steps<=128
            or type(max_seconds) is not int or not 10<=max_seconds<=600):
        raise ValueError("registry_probe_bound")
    client,_=connect_staging(cli,approved_plaintext=True)
    try:
        deadline=time.monotonic()+max_seconds
        source=Sources(client,scope="operator-preview-c1",deadline=min(deadline,time.monotonic()+55),ca_file=ca_file)
        source.initialise()
        if all_pairs:
            value={"status":"incomplete"}
            for _ in range(max_steps):
                if time.monotonic()+10>=deadline:break
                source.deadline=min(deadline,time.monotonic()+55)
                value=bootstrap_step(client,run_id,source)
                if value["status"]=="registry_ready":break
        else:
            value=bootstrap(client,run_id,source)
        return {"status":value.get("status","registry_ready"),"run_id":run_id,"mapping_version":value.get("mapping_version"),
            "pairs":len(value.get("universe",[])),"completed":value.get("completed",len(value.get("universe",[]))),
            "total":value.get("total",len(value.get("universe",[]))),"excluded":value.get("excluded",[]),
            "source_dispatches":source.calls,"production_access":False,
            "deletions":0,"memory_used":client.info("memory")["used_memory"]}
    finally: client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--ca-file");parser.add_argument("--run-id",default=uuid.uuid4().hex)
    parser.add_argument("--all-pairs",action="store_true")
    parser.add_argument("--max-steps",type=int,default=1)
    parser.add_argument("--max-seconds",type=int,default=480)
    args=parser.parse_args()
    try: print(json.dumps(run(args.vercel_cli,args.ca_file,args.run_id,all_pairs=args.all_pairs,
        max_steps=args.max_steps,max_seconds=args.max_seconds)))
    except Exception as exc:
        print(json.dumps({"status":"failed","run_id":args.run_id,"error_type":type(exc).__name__,
            "cause_type":type(getattr(exc,"reason",None)).__name__,
            "reason":str(exc) if isinstance(exc,(ValueError,RuntimeError)) and re.fullmatch(r"[a-z0-9_:]{1,100}",str(exc)) else "details_redacted"}))
        raise SystemExit(1) from None
