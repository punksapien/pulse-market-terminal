"""ADR-0062 bounded offline history/parser/storage benchmark. No network."""
import json
from pathlib import Path
import platform
import statistics
import sys
import timeit

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.collectors.oi_history import history_spec,parse_history,build_history_ring,cold_start_changes
from pulse.metrics.open_interest import OIObservation
from pulse.store.capsule_model import observation,derived_bytes


def run():
    spec=history_spec("B-BTC_USDT","BTCUSDT")
    now=1793001600000
    rows=[{"symbol":"BTCUSDT","timestamp":now-(319-i)*300000,"sumOpenInterest":"100.00"} for i in range(320)]
    points=parse_history(rows,spec,now_ms=now)
    ring=build_history_ring(points,spec,now_ms=now)
    current=OIObservation(now,110.,spec.pair_id)
    def measure(function,count):
        samples=timeit.repeat(function,number=count,repeat=5)
        return {"median_us":statistics.median(samples)*1e6/count,"max_us":max(samples)*1e6/count,
                "repeats":5,"iterations_per_repeat":count}
    point=observation(spec,now,("110.00",))
    return {"python":platform.python_version(),"platform":platform.platform(),"network_calls":0,
        "rows":320,"capsule_bytes":spec.nbytes,
        "parse_320":measure(lambda:parse_history(rows,spec,now_ms=now),20),
        "build_and_scrub_320":measure(lambda:build_history_ring(points,spec,now_ms=now),20),
        "three_window_lookup_and_calculation":measure(lambda:cold_start_changes(current,ring,spec,now_ms=now),1000),
        "exact_source_derived_encode":measure(lambda:derived_bytes(spec,point,1),2000),
        "not_measured":["network_latency","hosted_throughput","concurrent_collectors"]}


if __name__=="__main__":
    print(json.dumps(run(),indent=2))
