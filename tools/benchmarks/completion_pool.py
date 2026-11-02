"""ADR-0169 deterministic queue makespan, not a claim about hosted throughput."""
import heapq,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.collectors.all_pair_schedule import next_wave


def run():
    jobs=[(i,str(i),"legacy" if i%16==0 else "oi") for i in range(350)]
    cost=lambda j:8 if j.kind=="legacy" else 1
    heap=list(jobs);heapq.heapify(heap);barrier=0;members=[]
    while heap:
        wave=next_wave(heap,limit=16,homogeneous=True)
        barrier+=max(map(cost,wave));members.extend((j.symbol,j.kind) for j in wave)
    servers=[0]*16;heapq.heapify(servers);completed=[]
    for _,symbol,kind in sorted(jobs):
        start=heapq.heappop(servers);heapq.heappush(servers,start+(8 if kind=="legacy" else 1));completed.append((symbol,kind))
    assert sorted(members)==sorted(completed)
    return dict(jobs=350,workers=16,barrier_cost_units=barrier,continuous_cost_units=max(servers),
        unchanged_membership=True,exchange_calls=0,redis_calls=0,note="Synthetic8-unit slow and1-unit fast jobs; no hosted SLO claim.")


if __name__=="__main__":print(json.dumps(run()))
