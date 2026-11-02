"""Read-only staging detail coverage; known cohort keys, no source calls or scans."""
import argparse,json,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from preview_redis_access import api,PROJECT
from pulse_staging_gate import connect_staging
from pulse.store.worker_state import WorkerState
from pulse.collectors.preview_registry import load
from pulse.collectors.preview_pair import stores
from pulse.collectors.preview_compose import oi_values
from pulse.store.ls_reader import read_metrics
from pulse.store.funding_cache import decode
from pulse.metrics.funding import settled_view

STAGE="config"
def run(cli):
    global STAGE
    envs=api(cli,f"/v10/projects/{PROJECT}/env?target=preview")["envs"]
    def config(key):
        rows=[e for e in envs if e["key"]==key and not e.get("gitBranch") and e.get("target")==["preview"]]
        if len(rows)!=1:raise ValueError("preview_config_scope")
        return api(cli,f"/v1/projects/{PROJECT}/env/{rows[0]['id']}")["value"]
    run_id,mapping=config("PULSE_RUNTIME_RUN_ID"),config("PULSE_MAPPING_VERSION")
    STAGE="connect"
    client,_=connect_staging(cli,approved_plaintext=True)
    try:
        s,us=client.time();now=s*1000+us//1000
        STAGE="registry"
        universe=load(client,run_id,mapping,now_ms=now)
        universe=[m for m in universe if m["symbol"] in ("BTCUSDT","DOGEUSDT","XAUUSDT","ARKUSDT","IOSTUSDT")]
        records=WorkerState(SimpleNamespace(prefix="pulse:stg:{"+run_id+"}:publication:",client=client)).read_many([m["symbol"] for m in universe])
        rows=[]
        for meta in universe:
            STAGE="retained:"+meta["symbol"]
            ss=stores(client,run_id,meta)
            current,changes,witness=oi_values(ss,meta["pair_id"],now_ms=now)
            ls=read_metrics(*ss[2:],now_ms=now)
            record=records[meta["symbol"]] or {}
            report=settled_view(decode(record["funding_report"].encode()),now_ms=now) if record.get("funding_report") else None
            rows.append(dict(pair=meta["pair_id"],oi={h:dict(status=v.status,reason=v.reason) for h,v in changes.items()},
                baselines={h:{k:dict(status=v["status"],reason=v.get("reason"),source_ms=v.get("source_ms")) for k,v in b.items()} for h,b in witness.items()},
                top_ratio=dict(status=ls["series"]["top"]["ratio"].status,reason=ls["series"]["top"]["ratio"].reason),
                funding_reason=report.last.reason if report else record.get("funding_refresh_reason","not_collected"),
                top_refresh_age_ms=now-record.get("top_refreshed_ms",0),cold_refresh_age_ms=now-record.get("cold_refreshed_ms",0)))
        return dict(at_ms=now,rows=rows,memory=client.info("memory").get("used_memory"),writes=0,exchange_calls=0)
    finally:client.close()

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--vercel-cli",required=True)
    try:print(json.dumps(run(parser.parse_args().vercel_cli)))
    except Exception as exc:
        print(json.dumps(dict(error_type=type(exc).__name__,stage=STAGE)))
        raise SystemExit(1) from None
