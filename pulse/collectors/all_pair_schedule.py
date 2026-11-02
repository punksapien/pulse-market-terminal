"""ADR-0133 pure, restartable, per-pair due-work selection."""
from dataclasses import dataclass
import heapq
import hashlib

PERIODS={"1H":3600000,"4H":14400000,"24H":86400000}
REFERENCE_REFRESH_MS=18*60000


def reference_due(symbol,record):
    """ADR-0187: fixed phases prevent a universe-sized reference refresh burst."""
    until=record.get("references_prefetch_until_ms",0)
    refreshed=record.get("references_refreshed_ms",0)
    if type(until) is not int or until<0 or type(refreshed) is not int or refreshed<0:
        raise ValueError("schedule_timestamp")
    if not record.get("opens") or not refreshed or until-refreshed<REFERENCE_REFRESH_MS:
        return max(0,until-120000)
    phase=int.from_bytes(hashlib.sha256(symbol.encode("ascii")).digest()[:8],"big")%REFERENCE_REFRESH_MS
    return refreshed+REFERENCE_REFRESH_MS-(refreshed-phase)%REFERENCE_REFRESH_MS


@dataclass(frozen=True)
class DueJob:
    symbol: str
    kind: str
    due_ms: int


def due_jobs(universe,records,*,now_ms,attempted=frozenset(),next_funding=None):
    if (type(now_ms) is not int or now_ms<0 or not 1<=len(universe)<=1024
            or len({m["symbol"] for m in universe})!=len(universe)):
        raise ValueError("all_pair_schedule_bound")
    heap=[]
    for meta in universe:
        symbol=meta["symbol"];record=records.get(symbol) or {}
        retries=record.get("schedule_retry_ms",{})
        anchor=record.get("schedule_first_seen_ms",record.get("last_collected_ms",now_ms))
        if type(anchor) is not int or not 0<=anchor<=now_ms+5000:raise ValueError("schedule_anchor_timestamp")
        def add(kind,stamp,period):
            if type(stamp) is not int or stamp<0:raise ValueError("schedule_timestamp")
            due=stamp+period if stamp else anchor  # ADR-0155: missing work must accumulate waiting age.
            retry=retries.get(kind,0)
            if type(retry) is not int or retry<0:raise ValueError("schedule_retry_timestamp")
            due=max(due,retry)
            if due<=now_ms and (symbol,kind) not in attempted:
                heapq.heappush(heap,(due,symbol,kind))
        if not record.get("last_collected_ms"):
            add("bootstrap",0,0)
            # ADR-0175: a missing candle must not starve verified OI/funding inputs.
            if not (record.get("oi_source_ms") and isinstance(record.get("instrument"),dict)):
                continue
        for kind,field,period in (("oi","last_hot_ms",60000),
                ("instrument","instrument_ms",60000),  # ADR-0170: reserve time inside unchanged120s validity.
                ("global","global_tip_refreshed_ms",300000),
                ("detail","detail_refreshed_ms",300000),
                ("legacy","legacy_source_ms",720000)):
            add(kind,record.get(field,0),period)
        nft=record.get("nft",0)
        changed=next_funding is not None and symbol in next_funding and nft!=next_funding[symbol]
        if record.get("funding_report") and record.get("funding_history") and not changed:
            add("funding",nft,0)
        else:add("funding",0,0)
        add("references",reference_due(symbol,record),0)
        for window,step in PERIODS.items():
            point=record.get("ls_periods",{}).get(window)
            add("period_"+window,point["source_ms"] if point else 0,step)
    return heap


def next_wave(heap,*,limit=8,bootstrap_first=False,homogeneous=False,references_first=False):
    if type(limit) is not int or not 1<=limit<=16:raise ValueError("schedule_wave_bound")
    if type(bootstrap_first) is not bool:raise ValueError("schedule_bootstrap_policy")
    if type(homogeneous) is not bool:raise ValueError("schedule_wave_policy")
    if type(references_first) is not bool:raise ValueError("schedule_reference_policy")
    if bootstrap_first:
        pending=[item for item in heap if item[2]=="bootstrap"]
        if pending:
            # ADR-0138: only an unpublished universe may postpone ordinary refresh.
            heapq.heapify(pending)
            selected=next_wave(pending,limit=limit)
            removed={(j.due_ms,j.symbol,j.kind) for j in selected}
            heap[:]=[item for item in heap if item not in removed]
            heapq.heapify(heap)
            return selected
    if references_first:
        pending=[item for item in heap if item[2]=="references"]
        if pending:
            heapq.heapify(pending)
            selected=next_wave(pending,limit=limit)
            removed={(j.due_ms,j.symbol,j.kind) for j in selected}
            heap[:]=[item for item in heap if item not in removed]
            heapq.heapify(heap)
            return selected
    if homogeneous and heap:
        # ADR-0154: a slow candle job must not idle completed OI worker slots.
        kind=heap[0][2]
        eligible=[item for item in heap if item[2]==kind]
        heapq.heapify(eligible)
        selected=next_wave(eligible,limit=limit)
        removed={(j.due_ms,j.symbol,j.kind) for j in selected}
        heap[:]=[item for item in heap if item not in removed]
        heapq.heapify(heap)
        return selected
    selected=[];seen=set();deferred=[]
    while heap and len(selected)<limit:
        due,symbol,kind=heapq.heappop(heap)
        if symbol in seen:deferred.append((due,symbol,kind));continue
        seen.add(symbol);selected.append(DueJob(symbol,kind,due))
    for item in deferred:heapq.heappush(heap,item)
    return selected
