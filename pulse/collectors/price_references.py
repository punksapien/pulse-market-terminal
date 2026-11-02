"""ADR-0075 sparse CoinDCX minute opens. Caller supplies admitted transport."""
from decimal import Decimal

from pulse.metrics.price_change import (PriceTick, CandleOpen, WINDOW_MS, build_ladder,
                                        build_fallback, MINUTE_MS)
from pulse.metrics.price_precision import prepare_price
from pulse.observation import MAX_SOURCE_TIME_MS


def requests_for(tick,*,now_ms,prefetch_minutes=5):
    if type(prefetch_minutes) is not int or prefetch_minutes not in (5,26):raise ValueError("reference_prefetch_bound")
    if (not isinstance(tick,PriceTick) or type(now_ms) is not int
            or not 86400000<=now_ms<=MAX_SOURCE_TIME_MS-300000
            or type(tick.source_time_ms) is not int or not 86400000<=tick.source_time_ms<=now_ms+5000
            or not isinstance(tick.pair_id,str) or not tick.pair_id.startswith("B-")
            or not tick.pair_id.endswith("_USDT") or len(tick.pair_id)>64):
        raise ValueError("reference_request_identity_or_time")
    start=now_ms//MINUTE_MS*MINUTE_MS
    requests=[]
    for window in WINDOW_MS.values():
        needed={start+i*MINUTE_MS-window for i in range(prefetch_minutes)}
        fallback=(tick.source_time_ms-window)//MINUTE_MS*MINUTE_MS
        needed.add(fallback)
        # Small contiguous windows, not an illiquid tick's entire age of history.
        groups=[needed] if max(needed)-min(needed)<=(prefetch_minutes+2)*MINUTE_MS else [needed-{fallback},{fallback}]
        for group in groups:
            requests.append({"pair":tick.pair_id,"from":min(group)//1000,
                "to":(max(group)+MINUTE_MS)//1000,"resolution":"1","pcode":"f",
                "needed":tuple(sorted(group))})
    return tuple(requests)


def parse_opens(payload,request):
    if type(payload) is not dict or payload.get("s") not in (None,"ok"):
        raise ValueError("reference_candle_response")
    rows=payload.get("data")
    if type(rows) is not list or len(rows)>32:
        raise ValueError("reference_candle_bound")
    found={}
    for row in rows:
        if type(row) is not dict:
            raise ValueError("reference_candle_shape")
        stamp=row.get("time")
        if type(stamp) is not int or stamp%MINUTE_MS or not request["from"]*1000<=stamp<=request["to"]*1000:
            raise ValueError("reference_candle_time")
        raw=row.get("open")
        if isinstance(raw,bool) or not isinstance(raw,(str,int,Decimal)):
            raise ValueError("reference_source_precision")
        price=prepare_price(str(raw))
        if not price.available:
            raise ValueError("reference_source_precision:"+price.reason)
        candle=CandleOpen(request["pair"],stamp,price.value)
        if stamp in found and found[stamp]!=candle:
            raise ValueError("reference_conflicting_duplicate")
        found[stamp]=candle
    return {stamp:value for stamp,value in found.items() if stamp in request["needed"]}


def collect(tick,request,*,now_ms,mapping_version,generation_id):
    """At most six bounded requests; no hidden retry or unadmitted source client."""
    opens={}
    plans=requests_for(tick,now_ms=now_ms)
    for plan in plans:
        params={k:v for k,v in plan.items() if k!="needed"}
        raw=request("/market_data/candlesticks",params,host="https://public.coindcx.com")
        for stamp,candle in parse_opens(raw,plan).items():
            if stamp in opens and opens[stamp]!=candle:
                raise ValueError("reference_conflicting_duplicate")
            opens[stamp]=candle
    identity=dict(mapping_version=mapping_version,definition_version="price_change.v1",
                  generation_id=generation_id,now_ms=now_ms)
    return {"ladder":build_ladder(opens,pair_id=tick.pair_id,**identity),
            "fallback":build_fallback(opens,tick,**identity),"requests":len(plans),
            "selected_open_count":len(opens)}
