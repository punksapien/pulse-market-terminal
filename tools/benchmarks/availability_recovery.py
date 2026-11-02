"""ADR-0122/0124 offline CPU benchmark; no Redis/network latency claim."""
from decimal import Decimal
import json,sys,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.collectors import preview_sources as sources
from pulse.metrics.price_change import CandleOpen,WINDOW_MS
from pulse.wire.rows import empty_row
from pulse.wire.reference_rows import attach_ladder

def main():
    now=1800000000000;times=[]
    for _ in range(30):
        start=time.perf_counter()
        for i in range(50):
            meta=dict(id=i,pair_id=f"B-P{i}_USDT",symbol=f"P{i}USDT",funding_hours=8)
            row=empty_row(meta,"fixture")
            opens={now+j*60000-window:CandleOpen(meta["pair_id"],now+j*60000-window,Decimal(100))
                for j in range(5) for window in WINDOW_MS.values()}
            attach_ladder(row,opens,pair_id=meta["pair_id"],mapping_version="fixture",now_ms=now)
            assert row["references"]["fallback"] is None
        times.append((time.perf_counter()-start)*1000)
    src=sources.Sources.__new__(sources.Sources)
    src.deadline=time.monotonic()+60;src.last=0;src.calls=0;src.ca_file=None
    src.check_lease=lambda:None;src.dcx=SimpleNamespace(last_feedback={})
    attempts=[0]
    def fetch(*args,**kwargs):
        attempts[0]+=1
        if attempts[0]<=12:
            exc=sources.AdmissionDenied("maintenance");exc.admission_reason="budget_pruned_retry";raise exc
        return {}
    start=time.perf_counter()
    with patch.object(sources,"fetch",fetch):
        src._dispatch_inner("/fixture",{},host="https://public.coindcx.com",weight=0,family=False)
    print(json.dumps({"mode":"offline_no_network_or_redis","python":sys.version.split()[0],
        "fifty_ladders_median_ms":round(sorted(times)[15],3),"fifty_ladders_max_ms":round(max(times),3),
        "maintenance_steps":src.prune_steps,"http_dispatches":src.calls,
        "maintenance_mock_cpu_ms":round((time.perf_counter()-start)*1000,3),
        "lua_work_per_call":"unchanged: <=64 expired entries per dimension"}))

if __name__=="__main__":main()
