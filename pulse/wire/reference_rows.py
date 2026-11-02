"""ADR-0095 exact-minute adapter using unchanged reference calculations."""
from pulse.metrics.price_change import build_ladder,build_fallback,fallback_change,WINDOW_MS
from .rows import available,unavailable


def point(candle,minute):
    return {"status":"available" if candle is not None else "unavailable",
            "reason":None if candle is not None else "reference_missing",
            "reference_minute_ms":minute,"open_price":str(candle.price) if candle is not None else None}


def attach_ladder(row,opens,*,pair_id,mapping_version,now_ms):
    # ADR-0122: historical reference validity never depends on the old numerator.
    ladder=build_ladder(opens,pair_id=pair_id,mapping_version=mapping_version,
        definition_version="price_change.v1",generation_id="pending",now_ms=now_ms)
    row["references"].update(status="available",reason=None,
        ladder={"built_at_ms":ladder.built_at_ms,"start_ms":ladder.start_ms,
            "valid_until_ms":ladder.valid_until_ms,"windows":list(WINDOW_MS),
            "slots":[[point(candle,ladder.start_ms+i*60000-window) for candle,window in zip(candles,WINDOW_MS.values())]
                     for i,candles in enumerate(ladder.slots)]},fallback=None)
    for horizon in WINDOW_MS:row["changes"][horizon]=unavailable("fraction","price_change.v1","numerator_unavailable")


def attach(row,tick,opens,*,mapping_version,input_ref,price_expires_ms,now_ms):
    attach_ladder(row,opens,pair_id=tick.pair_id,mapping_version=mapping_version,now_ms=now_ms)
    if row["price"]["status"]!="available" or now_ms>=price_expires_ms:return
    fallback=build_fallback(opens,tick,mapping_version=mapping_version,
        definition_version="price_change.v1",generation_id="pending",now_ms=now_ms)
    row["references"]["fallback"]={"source_ms":tick.source_time_ms,"price":str(tick.price),"expires_ms":price_expires_ms,
            "references":{h:point(candle,(tick.source_time_ms-window)//60000*60000)
                          for (h,window),candle in zip(WINDOW_MS.items(),fallback.references)}}
    for h in WINDOW_MS:
        metric=fallback_change(fallback,pair_id=tick.pair_id,horizon=h,mapping_version=mapping_version,
            definition_version="price_change.v1",generation_id="pending",expires_at_ms=price_expires_ms,now_ms=now_ms)
        row["changes"][h]=(available("fraction","price_change.v1",metric.value_fraction,
            source_ms=tick.source_time_ms,expires_ms=price_expires_ms,input_refs=[input_ref],now_ms=now_ms)
            if metric.status=="available" else unavailable("fraction","price_change.v1",metric.reason))
