"""ADR-0154 synthetic wave makespan, not hosted or Redis timings."""
import heapq
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.collectors.all_pair_schedule import next_wave


def run():
    jobs=[(i,str(i),'legacy' if i%16==0 else 'oi') for i in range(128)]
    results={};seen=[]
    for policy in (False,True):
        heap=list(jobs);heapq.heapify(heap);cost=0;selected=[];waves=0
        while heap:
            wave=next_wave(heap,limit=16,homogeneous=policy)
            cost+=max(8 if j.kind=='legacy' else 1 for j in wave)
            selected.extend((j.due_ms,j.symbol,j.kind) for j in wave);waves+=1
        seen.append(set(selected))
        results[str(policy)]={"synthetic_cost_units":cost,"waves":waves,"jobs":len(selected)}
    assert seen[0]==seen[1]==set(jobs)
    return dict(results=results,unchanged_membership_and_due_times=True,exchange_calls=0,redis_calls=0,
        note="Eight slow jobs cost8 units, other jobs1; not real latency")


if __name__=='__main__':print(json.dumps(run(),sort_keys=True))
