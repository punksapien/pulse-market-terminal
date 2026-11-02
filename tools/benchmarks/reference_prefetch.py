"""ADR-0113 synthetic MemoryStore cache benchmark; no exchange/Redis latency claim."""
from decimal import Decimal
import json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.store.memory_store import MemoryStore
from pulse.collectors.price_references import requests_for
from pulse.metrics.price_change import PriceTick,CandleOpen,build_ladder

def main():
    now=1800000000000
    for minutes,starts in ((5,1),(26,22)):
        cache={};requests=0
        for i in range(50):
            pair=f"B-P{i}_USDT";tick=PriceTick(pair,now-1000,Decimal(100))
            plans=requests_for(tick,now_ms=now,prefetch_minutes=minutes);requests+=len(plans)
            cache[pair]=[[stamp,"100"] for stamp in sorted({s for plan in plans for s in plan["needed"]})]
        payload=json.dumps(cache,separators=(",",":")).encode()
        memory=MemoryStore(1000000);memory.claim(1)
        memory.atomic_control(1,lambda values:values.update(cache=payload))
        began=time.perf_counter();missing=0
        for minute in range(starts):
            loaded=json.loads(memory.read_control(lambda values:values["cache"]))
            for pair,points in loaded.items():
                opens={stamp:CandleOpen(pair,stamp,Decimal(price)) for stamp,price in points}
                ladder=build_ladder(opens,pair_id=pair,mapping_version="fixture",definition_version="price_change.v1",
                    generation_id="fixture",now_ms=now+minute*60000)
                missing+=sum(point is None for row in ladder.slots for point in row)
        print(json.dumps({"pairs":50,"prefetch_minutes":minutes,"usable_ladder_starts":starts,
            "cache_bytes":len(payload),"opens_per_pair":len(next(iter(cache.values()))),
            "source_requests_per_population_prefetch":requests,
            "modelled_requests_for_22_starts":requests*(22 if minutes==5 else 1),
            "memory_store_read_and_ladder_ms":round((time.perf_counter()-began)*1000,3),"missing_points":missing}))

if __name__=="__main__":main()
