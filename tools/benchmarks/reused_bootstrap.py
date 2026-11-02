"""ADR-0145 offline repeated320 versus gap14 writes; no Redis/network/files."""
import json
import platform
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.store.bucket_ring import BucketRing


def run():
    step=300000;now=1800000000000;gap=12
    base=BucketRing(step,320)
    for index in range(320):
        stamp=now-(319+gap-index)*step
        base.write(stamp,(float(stamp//step),),now_ms=now)
    raw=base.to_bytes();results={};outputs=[]
    for count in (320,14):
        samples=[]
        for _ in range(50):
            ring=BucketRing.from_bytes(raw)
            start=time.perf_counter_ns()
            for index in range(count):
                stamp=now-(count-1-index)*step
                ring.write(stamp,(float(stamp//step),),now_ms=now)
            samples.append((time.perf_counter_ns()-start)/1000)
        outputs.append(ring.to_bytes())
        results[str(count)]={"candidate_writes":count,"median_us":round(statistics.median(samples),3),
            "sample_p95_us":round(sorted(samples)[47],3)}
    assert outputs[0]==outputs[1]
    return dict(python=platform.python_version(),samples_per_case=50,gap_ms=gap*step,
        byte_identical=True,results=results,redis_calls=0,exchange_calls=0,
        note="Python ring CPU, not hosted Lua/network timing")


if __name__=='__main__':print(json.dumps(run(),sort_keys=True))
