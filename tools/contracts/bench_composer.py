"""ADR-0089 offline fixture benchmark; no network, Redis or production writes."""
import json
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"tests"))
from test_wire_publication import brotli
from test_composer import ComposerTests
from pulse.wire.composer import compose
from pulse.collectors.worker_loop import BoundedLoop,Job


def main():
    case=ComposerTests();case.setUp()
    elapsed=[]
    for _ in range(5):
        start=time.perf_counter_ns()
        package=compose(case.s,validate=case.snapshot_gate,brotli_compress=brotli,
                        input_revisions={"fixture":1},now_ms=case.now)
        elapsed.append((time.perf_counter_ns()-start)/1e6)
    wall=[0];calls=[]
    def wait(ms): wall[0]+=ms
    loop=BoundedLoop([Job("five-second",5000,lambda **_:calls.append(wall[0]))],
        clock=lambda:wall[0],wait=wait,lease_alive=lambda:True,admit=lambda _:True)
    start=time.perf_counter_ns();result=loop.run()
    loop_us=(time.perf_counter_ns()-start)/1000
    print(json.dumps({"python":sys.version.split()[0],"fixture_rows":len(case.s["rows"]),
        "composer_median_ms":statistics.median(elapsed),"composer_max_ms":max(elapsed),
        "prepared_bytes_all_variants":package.allocation_bytes,
        "compressor":"Node Brotli11 subprocess per part; includes process startup, not worker CPU estimate",
        "loop_us":loop_us,"loop_simulated_invocation_ms":55000,"loop_calls_ms":calls,
        "loop_reason":result["reason"],"scope":"offline fixture; no full-scale, store, network or hosted claim"},indent=2))


if __name__=="__main__": main()
