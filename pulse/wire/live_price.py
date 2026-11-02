"""ADR-0089 source-to-live-price producer; no exchange or store access."""
from copy import deepcopy
from decimal import Decimal
import re
from pulse.metrics.price_precision import prepare_price
from pulse.observation import MAX_SOURCE_TIME_MS
from .identity import digest
from .decimal_text import DECIMAL_PATTERN
from .decimal_text import decimal_text

HASH=re.compile(r"^sha256:[a-f0-9]{64}$")
DECIMAL=re.compile(DECIMAL_PATTERN)
VOLUME_DEFINITION = "volume.rt.v2"
VOLUME_VALIDITY_MS = 300000  # ADR-0194: volume only; price and envelope clocks are unchanged.


def clock(value):
    if type(value) is not int or not 0<=value<=MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_live_clock")
    return value


def compose_live_price(universe,payload,*,mapping_version,batch_version,sequence,now_ms,
                       price_max_age_ms,validate):
    clock(now_ms);clock(sequence)
    if (not callable(validate) or type(price_max_age_ms) is not int or price_max_age_ms<=0
            or now_ms+max(price_max_age_ms+20000,VOLUME_VALIDITY_MS)>MAX_SOURCE_TIME_MS):
        raise ValueError("invalid_live_price_policy")
    if (not isinstance(universe,list) or not 1<=len(universe)<=8192
            or digest(universe)!=mapping_version or not isinstance(batch_version,str) or not HASH.fullmatch(batch_version)):
        raise ValueError("live_dictionary_or_witness")
    if type(payload) is not dict or type(payload.get("prices")) is not dict or len(payload["prices"])>8192:
        raise ValueError("invalid_live_price_batch")
    batch_time=clock(payload.get("ts"))
    if batch_time>now_ms+5000: raise ValueError("future_live_price_batch")
    if batch_time+max(price_max_age_ms+5000,VOLUME_VALIDITY_MS)>MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_live_clock")
    rows=[];seen=set()
    for i,meta in enumerate(universe):
        if type(meta) is not dict: raise ValueError("live_dictionary_identity")
        pair,symbol=meta.get("pair_id"),meta.get("symbol")
        if (type(meta.get("id")) is not int or meta["id"]!=i or not isinstance(pair,str)
                or len(pair)>64 or not re.fullmatch(r"B-[A-Z0-9]+_USDT",pair)
                or symbol!=pair[2:-5]+"USDT" or pair in seen):
            raise ValueError("live_dictionary_identity")
        seen.add(pair)
        result={"id":i,"status":"unavailable","reason":"source_unavailable","price":None,
                "source_ms":None,"expires_ms":None,"input_refs":[]}
        source=payload["prices"].get(pair)
        # ADR-0118: volume uses the batch clock, independently of trade/price validity.
        volume=dict(status="unavailable",reason="source_unavailable",value=None,unit="USDT",source_ms=None,expires_ms=None,definition_version=VOLUME_DEFINITION)
        try:
            if type(source) is not dict or source.get("mkt")!=symbol: raise ValueError("ticker_identity_mismatch")
            raw=source.get("v")
            if type(raw) not in (str,int,Decimal): raise ValueError("volume_invalid")
            number=Decimal(raw)
            if not number.is_finite() or number<0: raise ValueError("volume_invalid")
            if now_ms>=batch_time+VOLUME_VALIDITY_MS: raise ValueError("stale_volume")
            volume.update(status="available",reason=None,value=decimal_text(number),source_ms=batch_time,expires_ms=batch_time+VOLUME_VALIDITY_MS)
        except (ValueError,ArithmeticError) as exc:
            volume["reason"]=str(exc) if isinstance(exc,ValueError) else "volume_invalid"
        result["volume"]=volume
        if source is not None:
            try:
                if type(source) is not dict or source.get("mkt")!=symbol: raise ValueError("ticker_identity_mismatch")
                stamp=clock(source.get("btST"))
                if stamp>min(now_ms,batch_time)+5000: raise ValueError("future_price")
                if stamp+price_max_age_ms>MAX_SOURCE_TIME_MS: raise ValueError("invalid_live_clock")
                if now_ms>=stamp+price_max_age_ms: raise ValueError("stale_price")
                text=source.get("ls")
                # ADR-0114: integer/Decimal JSON tokens are exact; never reprint a binary float.
                if type(text) is int or isinstance(text,Decimal): text=str(text)
                if not isinstance(text,str) or len(text)>64 or not DECIMAL.fullmatch(text):
                    raise ValueError("source_precision_unsupported")
                checked=prepare_price(text)
                if not checked.available: raise ValueError(checked.reason)
                result.update(status="available",reason=None,price=text,source_ms=stamp,
                    expires_ms=stamp+price_max_age_ms,input_refs=["price:batch"])
            except ValueError as exc:
                result["reason"]=str(exc)
        rows.append(result)
    value={"schema":"pulse.live_price.v1","mapping_version":mapping_version,"sequence":sequence,
        "generated_ms":now_ms,"expires_ms":now_ms+15000,"rows":rows,
        "inputs":{"price:batch":{"version":batch_version,"source_ms":batch_time,"verified_at_ms":None,
            "expires_ms":batch_time+max(price_max_age_ms+5000,VOLUME_VALIDITY_MS),"definition_version":"price_change.v1"}}}
    validate(value)
    return deepcopy(value)
