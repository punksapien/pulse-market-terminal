"""ADR-0151 source-free store-call benchmark using real OI/L/S calculations."""
import json
from pathlib import Path
import platform
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.collectors import preview_compose as module


def run():
    now=1800000000000;calls=[]
    meta=dict(pair_id="B-BTC_USDT",symbol="BTCUSDT",mapping_version="mapping")
    class Store:
        def __init__(self,metric):
            self.spec=SimpleNamespace(metric=metric,**meta)
        def point(self,stamp):
            ls=self.spec.metric.startswith('ls_')
            return dict(status="available",source_ms=stamp,expires_ms=stamp+900000,
                values=(1.,.5,.5) if ls else (100.,),sources=("1","0.5","0.5") if ls else ("100",))
        def read(self):calls.append('read');return self.point(now)
        def baseline(self,target,tolerance):calls.append('baseline');return self.point(target)
    stores=tuple(Store(k) for k in ('oi','oi_cold','ls_global','ls_top'))
    with patch.object(module,'stores',return_value=stores):
        full=module.retain_derived(None,'a'*32,meta,{},now_ms=now)
        results={}
        for update in ('all','oi','ls'):
            calls.clear()
            value=module.retain_derived(None,'a'*32,meta,full,now_ms=now,update=update)
            assert value==full
            results[update]=dict(store_calls=len(calls),identical_output=True)
    return dict(python=platform.python_version(),results=results,redis_calls=0,exchange_calls=0,
        note="Real calculations over fake store; counts baseline/current operations, not network latency")


if __name__=='__main__':print(json.dumps(run(),sort_keys=True))
