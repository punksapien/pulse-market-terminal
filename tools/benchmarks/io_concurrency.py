"""ADR-0172 sampled-duration queue model, NOT provider/hosted throughput proof."""
import heapq,json,random


def simulate(jobs,workers):
    queue=list(jobs);active=[];now=0;heavy=0;done=0;peak=0;peak_heavy=0
    while queue or active:
        deferred=[]
        while queue and len(active)<workers:
            i,kind,duration=queue.pop(0);is_heavy=kind=="legacy"
            if is_heavy and heavy>=6:deferred.append((i,kind,duration));continue
            heavy+=is_heavy;heapq.heappush(active,(now+duration,i,is_heavy))
            peak=max(peak,len(active));peak_heavy=max(peak_heavy,heavy)
        queue=deferred+queue
        if not active:raise AssertionError("no progress")
        now,_,was_heavy=heapq.heappop(active);heavy-=was_heavy;done+=1
    assert done==len(jobs) and peak<=workers and peak_heavy<=6
    return dict(modeled_seconds=round(now,3),jobs=done,peak_jobs=peak,peak_heavy=peak_heavy)


def run():
    jobs=[]
    for kind,n,seconds in (("oi",350,.95),("instrument",350,1.2),("global",70,1.1),
            ("detail",24,1.2),("legacy",30,7),("references",18,4.5),("period",7,1)):
        for _ in range(n):jobs.append((len(jobs),kind,seconds))
    random.Random(172).shuffle(jobs)
    return {"scope":"offline_sampled_duration_model","workers16":simulate(jobs,16),"workers32":simulate(jobs,32),
        "exchange_calls":0,"redis_calls":0,"limits":"No source pacing/quotas/network variance modeled; hosted evidence required."}


if __name__=="__main__":print(json.dumps(run()))
