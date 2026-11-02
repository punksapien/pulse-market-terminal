"""ADR-0187 offline reference burst/CPU benchmark; no network/store imports."""
import argparse,json,statistics,time
from collections import Counter
from pathlib import Path
from pulse.collectors.all_pair_schedule import reference_due,REFERENCE_REFRESH_MS


def benchmark(symbols,stamp):
    if len(symbols)!=350 or len(set(symbols))!=350:raise ValueError('reference_benchmark_membership')
    record={'opens':[[stamp-86400000,'1']], 'references_refreshed_ms':stamp,
            'references_prefetch_until_ms':stamp+22*60000}
    times=[];due=[]
    for _ in range(100):
        start=time.perf_counter()
        due=[reference_due(s,record) for s in symbols]
        times.append((time.perf_counter()-start)*1000)
    assert all(stamp<d<=stamp+REFERENCE_REFRESH_MS for d in due)
    bins=Counter((d-stamp)//60000 for d in due)
    return {'pairs':350,'iterations':100,'phase_period_ms':REFERENCE_REFRESH_MS,
        'legacy_peak_pairs_per_minute':350,'phased_peak_pairs_per_minute':max(bins.values()),
        'minute_counts':dict(sorted(bins.items())), 'median_ms':statistics.median(times),
        'p95_ms':sorted(times)[94], 'steady_reference_calls_per_minute':1050/18,
        'status':'offline_not_hosted_acceptance'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--history-witness',required=True)
    a=p.parse_args();witness=json.loads(Path(a.history_witness).read_text())
    rows=[row for bucket in witness['buckets'] for row in bucket.get('rows',[])]
    pairs=[r['pair'] for r in rows]
    if any(not s.startswith('B-') or not s.endswith('_USDT') for s in pairs):raise ValueError('reference_benchmark_scope')
    print(json.dumps(benchmark([s[2:].replace('_','') for s in pairs],1800000000000),indent=2))


if __name__=='__main__':main()
