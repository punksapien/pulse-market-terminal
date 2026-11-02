"""ADR-0150 compare pre-change and current candle validation offline; no secrets/network."""
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import types

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse.collectors import preview_pair as current


def run():
    old=types.ModuleType("pulse.collectors.clock_benchmark_old")
    old.__package__="pulse.collectors"
    code=subprocess.run(["git","show","bc14298^:pulse/collectors/preview_pair.py"],
        cwd=ROOT,capture_output=True,text=True,check=True).stdout
    exec(compile(code,"<pre-ADR0150-preview-pair>","exec"),old.__dict__)
    keys=("volatility_score","regime","vol_direction","rsi_14","vol_momentum","vol_velocity_label",
          "trade_signal","vol_outlook","vol_outlook_confidence","vol_outlook_reasons")
    outputs={h:{k:1 for k in keys} for h in ("1H","4H","24H")}
    now=1800000000000
    class Source:
        last_source_ms=now
        def __init__(self,size):
            self.calls=0
            self.rows=[dict(time=now-i*60000,open=1,high=1,low=1,close=1,volume=1) for i in range(size)]
        def clock(self):self.calls+=1;return now
        def request(self,path,params,**kw):
            return [] if 'openInterestHist' in path else {"data":self.rows}
    results={}
    original=current.evaluate
    try:
        for size in (40,3000):
            rendered=[];result={}
            for name,module in (("before",old),("after",current)):
                module.evaluate=lambda *args:outputs
                times=[]
                for _ in range(5):
                    source=Source(size);start=time.perf_counter_ns()
                    value=module.collect_legacy(source,"B-X_USDT","XUSDT",{},{} )
                    times.append((time.perf_counter_ns()-start)/1e6)
                rendered.append(value)
                result[name]={"trusted_clock_calls":source.calls,"median_offline_ms":round(statistics.median(times),3)}
            assert rendered[0]==rendered[1]
            results[str(size)]={**result,"identical_output":True}
    finally:current.evaluate=original
    return dict(python=platform.python_version(),results=results,exchange_calls=0,redis_calls=0,
        note="Clock calls counted with offline fake; CPU timings do not measure network latency; unchanged scorer stubbed")


if __name__=='__main__':print(json.dumps(run(),sort_keys=True))
