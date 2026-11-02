"""Offline repeated-fixture CPU benchmark; no source or storage traffic."""
import json
import platform
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pulse.metrics.funding import FundingMetadata, Settlement, settled_report

now = 1793001600000
rows = [Settlement("BTCUSDT",now-i*3600000,Decimal("0.0001")) for i in range(169)]
meta = FundingMetadata("BTCUSDT","v1",1,1,now+3600000,Decimal("0.0001"),now)
results = []
for pairs in (50,503):
    times = []
    for _ in range(20):
        start = time.perf_counter_ns()
        for _ in range(pairs):
            assert settled_report(rows,meta,now_ms=now).cumulative_7d.status == "available"
        times.append((time.perf_counter_ns()-start)/1e6)
    results.append({"repeated_pairs":pairs,"samples":len(times),"median_ms":statistics.median(times),"sample_p95_ms":sorted(times)[18]})
print(json.dumps({"python":platform.python_version(),"events_per_pair":169,"results":results},indent=2))
