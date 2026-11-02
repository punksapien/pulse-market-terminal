"""Offline control-path benchmark: no Redis, exchange calls or simulated latency claims."""
import ast
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from pulse.collectors import preview_pair as pair,preview_sources as sources

NOW=1800000000000


def dispatch(gap,count=470):
    clock=[10.0];pacer=sources.DispatchPacer(binance_gap=gap)
    with patch.object(sources.time,"monotonic",side_effect=lambda:clock[0]),\
            patch.object(sources.time,"sleep",side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
        for _ in range(count):pacer.reserve(sources.HOST,1000,lambda costs:True,{"weight":1})
    return {"dispatches":count,"pacing_only_seconds":round(clock[0]-10,6)}


def repairs(text):
    definition=next(node for node in ast.parse(text).body if isinstance(node,ast.FunctionDef) and node.name=="collect_detail")
    calls=[];missing=lambda *args:dict(status="unavailable",reason="baseline_missing")
    own=SimpleNamespace(read=lambda:dict(status="available",source_ms=NOW),baseline=missing)
    cold=SimpleNamespace(baseline=missing,spec=None)
    env=dict(vars(pair));env.update(stores=lambda *args:(own,cold,None,None),parse_history=lambda *args,**kw:(),save_points=lambda *args:None)
    exec(compile(ast.Module(body=[definition],type_ignores=[]),"<offline-detail-benchmark>","exec"),env)
    source=SimpleNamespace(clock=lambda:NOW,request=lambda path,params,**kw:calls.append((path,params)) or [])
    env["collect_detail"](None,"a"*32,{"pair_id":"B-BTC_USDT","symbol":"BTCUSDT"},source,{"top_refreshed_ms":NOW})
    return {"history_requests":len(calls),"requested_rows":sum(params["limit"] for _,params in calls)}


if __name__=="__main__":
    old=subprocess.run(["git","show","9e8c666:pulse/collectors/preview_pair.py"],cwd=ROOT,capture_output=True,text=True,check=True).stdout
    print(json.dumps({"scope":"offline_no_network_no_redis","dispatch_before":dispatch(.15),
        "dispatch_after":dispatch(.084),"three_missing_oi_before":repairs(old),
        "three_missing_oi_after":repairs((ROOT/"pulse/collectors/preview_pair.py").read_text()),
        "limits":"Pacing arithmetic and mocked request count only; no actual network speed or all-pair SLO claim."},indent=2))
