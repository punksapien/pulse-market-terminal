"""ADR-0180 offline full350 due-heap CPU; no Redis or exchange calls."""
import heapq,json,statistics,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.collectors.all_pair_schedule import due_jobs
from pulse.collectors.job_deadlines import priority

def run():
    now=1800000000000
    universe=[{'symbol':'P'+str(i)} for i in range(350)]
    records={m['symbol']:{'last_collected_ms':now-86400000,'last_hot_ms':now-120000,
        'instrument_ms':now-120000,'global_source_ms':now-900000,
        'global_tip_refreshed_ms':now-600000,'top_source_ms':now-1800000,
        'top_refreshed_ms':now-1800000,'detail_refreshed_ms':now-600000,
        'legacy_source_ms':now-900000,'references_prefetch_until_ms':now,
        'nft':now-60000,'funding_report':'verified','funding_history':[1],
        'ls_periods':{h:{'source_ms':now-86400000,'expires_ms':now+900000} for h in ('1H','4H','24H')}}
        for m in universe}
    result={}
    for name,rank in (('oldest_due',lambda item:item),('deadline',lambda item:priority(item,records[item[1]]))):
        samples=[]
        for _ in range(100):
            start=time.perf_counter_ns();jobs=due_jobs(universe,records,now_ms=now)
            heap=[rank(j) for j in jobs];heapq.heapify(heap)
            for _ in range(32):heapq.heappop(heap)
            samples.append((time.perf_counter_ns()-start)/1e6)
        result[name]={'jobs':len(jobs),'median_ms':round(statistics.median(samples),3),'p95_ms':round(sorted(samples)[94],3)}
    return {'scope':'offline CPU only, not source or hosted throughput','pairs':350,'results':result}

if __name__=='__main__':print(json.dumps(run()))
