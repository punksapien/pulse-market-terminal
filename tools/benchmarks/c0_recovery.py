"""ADR-0054 bounded synthetic control benchmarks; no network, Redis or file writes."""
import json
import math
import platform
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.store.memory_store import MemoryStore
from pulse.store.bucket_ring import BucketRing, WriteStatus
from pulse.store.recovery import DerivedRecord, IncrementalBuilder, DeadlineArtifacts, RepairQueue, PressureLadder, RollingScrubber
from pulse.store.admission import BudgetLimit, BudgetTracker, CircuitBreaker
from pulse.store.coverage import CoverageBitmap


def measure(function,n=100,prepare=None,check=None):
    times=[]
    for _ in range(n):
        if prepare:
            prepare()
        start=time.perf_counter_ns()
        result=function()
        times.append((time.perf_counter_ns()-start)/1000)
        if check:
            check(result)
    return {"samples":n,"median_us":statistics.median(times),"sample_p95_us":sorted(times)[math.ceil(.95*n)-1]}


def cohort(rows):
    store=MemoryStore(21000000)
    store.claim(1)
    builder=IncrementalBuilder(store,"rows",rows,1)
    version=0
    def build(dirty):
        nonlocal version
        version+=1
        for row in range(dirty):
            builder.mark(row,1)
        assert builder.build(lambda row:DerivedRecord(str(row).encode(),100,1000000,(("v",version),)),1,now_ms=100,batch=rows)
    build(rows)
    results={"rows":rows,"builder_all":measure(lambda:build(rows)),"builder_one":measure(lambda:build(1))}
    queue=RepairQueue(store,"repairs",1,capacity=rows)
    keys=[(str(row),"oi","1H") for row in range(rows)]
    for row,key in enumerate(keys):
        queue.enqueue(key,expires_ms=1000+row,importance=1+row%5,now_ms=0,token=1)
    tick=0
    def prepare_repair():
        nonlocal tick
        tick+=100
    def check_repair(selected):
        # Independent full-sort oracle and assertion are OUTSIDE claim's timer.
        def ordering(key):
            slack=1000+int(key[0])-tick
            weight=1+int(key[0])%5
            return (0,slack*weight,key) if slack<0 else (1,slack/weight,key)
        expected=sorted(keys,key=ordering)[:10]
        assert [key for key,_ in selected]==expected
    results["repair_claim10_only"]=measure(lambda:queue.claim(now_ms=tick,lease_ms=1,token=1,batch=10),prepare=prepare_repair,check=check_repair)
    artifacts=DeadlineArtifacts(store,"artifacts",1,max_bytes=1000000,max_items=rows)
    epoch=0
    def reaper():
        nonlocal epoch
        epoch+=1
        now=epoch*1000
        for row in range(10):
            artifacts.stage(f"{epoch}:{row}".encode(),expires_ms=now+1,deadline_ms=now+2,now_ms=now,token=1)
        assert len(artifacts.reap(now_ms=now+2,token=1,batch=10))==10
    results["stage_and_reap10"]=measure(reaper)
    budget=BudgetTracker(store,"budget",{"requests":BudgetLimit(1000,1000),"bytes":BudgetLimit(1000000,billing_start_ms=0,billing_end_ms=1000000)},1)
    budget.reconcile({"requests":0,"bytes":0},now_ms=0,evidence_id="offline-zero-fixture",token=1)
    clock=0
    def reserve():
        nonlocal clock
        clock+=1
        assert budget.reserve({"requests":1,"bytes":10},now_ms=clock,token=1)=="reserved"
    results["budget_two_dimensions"]=measure(reserve)
    circuit=CircuitBreaker(store,"circuit",1)
    circuit.reconcile(now_ms=0,token=1)
    _,probe=circuit.allow(now_ms=0,token=1)
    circuit.outcome(now_ms=0,token=1,status=200,draw=0,probe=probe)
    results["circuit_allow"]=measure(lambda:circuit.allow(now_ms=100,token=1))
    pressure=PressureLadder(store,"pressure",1)
    results["pressure"]=measure(lambda:pressure.observe(15000000,1))
    ring=BucketRing(60000,1536)
    for row in range(1536):
        ring.write(row*60000,(float(row),),now_ms=row*60000)
    results["coverage1536_tag_scan_oracle"]=measure(lambda:ring.coverage_buckets(0,1535))
    coverage=CoverageBitmap(store,"coverage",1536,1)
    for row in range(1536):
        coverage.observe(row,status=WriteStatus.WRITTEN,token=1)
    assert coverage.count(0,1535)==ring.coverage_buckets(0,1535)
    results["coverage1536_bitmap_popcount"]=measure(lambda:coverage.count(0,1535))
    results["coverage_sidecar_packed_budget_bytes"]=coverage.packed_budget_bytes
    manifest=tuple(f"oi:{row}" for row in range(rows))
    for key in manifest:
        store.create_ring(key,60000,4,1,1)
    scrub=RollingScrubber(store,"scrub",manifest,1)
    results["scrub_tick"]=measure(lambda:scrub.tick(1))
    # Fences are checked on both mutation and scrub. Nothing is charged as Redis RSS.
    return results


if __name__ == "__main__":
    results=[cohort(n) for n in (50,503)]
    # Separate allocation pass: its latency samples are discarded, never reported.
    tracemalloc.start()
    cohort(503)
    _,peak=tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps({"python":platform.python_version(),"synthetic":True,"tracemalloc_enabled_during_timings":False,
                      "python_traced_peak_bytes_separate_503_pass":peak,"results":results},indent=2))
