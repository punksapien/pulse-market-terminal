"""ADR-0078 full named snapshot proposal and fictional fixtures."""
from copy import deepcopy
from decimal import Decimal
from spec import (DRAFT, NOW, HORIZONS, TIME, TOKEN, REASON, DECIMAL, ref, nullable, obj,
                  typed_field, value_field, unavailable_field)

SNAPSHOT="urn:pulse:schema:v1:snapshot"
ID={"type":"integer","minimum":0,"maximum":8191}
STATUS={"enum":["available","unavailable","settlement_pending","unsupported"]}
PAIR={"type":"string","maxLength":64,"pattern":r"^B-[A-Z0-9]+_USDT$"}
SYMBOL={"type":"string","maxLength":64,"pattern":r"^[A-Z0-9]+USDT$"}
HOURS={"enum":[1,4,8]}
CODES=["MomentumLong","MomentumShort","ReversalLong","ReversalShort","BreakoutWatch","Range","Avoid"]
CATEGORIES={"MomentumLong":"Uptrend","MomentumShort":"Downtrend","ReversalLong":"Reversal",
            "ReversalShort":"Reversal","BreakoutWatch":"Breakout","Range":"Range","Avoid":"Avoid"}


def window_map(schema):
    return obj({h:deepcopy(schema) for h in HORIZONS})


def string_field(unit,definition,allowed=None):
    properties={"unit":{"const":unit},"definition_version":{"const":definition}}
    if allowed is not None: properties["value"]={"enum":[None]+list(allowed)}
    return {"allOf":[ref("textField"),{"properties":properties}]}


def availability(properties,available_properties):
    schema=obj({"status":STATUS,"reason":nullable(REASON),**properties})
    schema["allOf"]=[{"if":{"properties":{"status":{"const":"available"}}},
        "then":{"properties":{"reason":{"type":"null"},**available_properties}},
        "else":{"properties":{"reason":REASON}}}]
    return schema


def point_schema():
    schema=availability({"reference_minute_ms":TIME,"open_price":nullable(DECIMAL)},
                        {"open_price":DECIMAL})
    schema["allOf"][0]["else"]["properties"]["open_price"]={"type":"null"}
    return schema


def snapshot_schema():
    oi_change={"allOf":[typed_field("fraction","open_interest.v1"),
        {"required":["baseline_asof_ms","actual_duration_ms","baseline_error_ms","mode"]}]}
    funding_cycle=availability({"id":ID,"mapping_version":TOKEN,"h":nullable(HOURS),"nft":nullable(TIME),
        "settled_rate":nullable(DECIMAL),"settlement_ms":nullable(TIME),"verified_at_ms":nullable(TIME),
        "expires_ms":nullable(TIME),"settled_last":typed_field("fraction","funding_change.v1"),
        "settled_1d":typed_field("fraction","funding_change.v1"),
        "settled_7d":typed_field("fraction","funding_change.v1"),
        "basis_bps":typed_field("bps","funding_change.v1")},
        {"h":HOURS,"nft":TIME,"settled_rate":DECIMAL,"settlement_ms":TIME,"verified_at_ms":TIME,"expires_ms":TIME})
    funding_cycle["allOf"][0]["else"]["properties"].update({key:{"type":"null"} for key in
        ("settled_rate","settlement_ms","verified_at_ms","expires_ms")})
    level={"allOf":[typed_field("fraction","volatility.v1")]}
    references=obj({"status":STATUS,"reason":nullable(REASON),"mapping_version":TOKEN,
        "definition_version":{"const":"price_change.v1"},"generation_id":TOKEN,
        "ladder":nullable(obj({"built_at_ms":TIME,"start_ms":TIME,"valid_until_ms":TIME,
            "windows":{"const":list(HORIZONS)},"slots":{"type":"array","minItems":5,"maxItems":5,
                "items":{"type":"array","minItems":3,"maxItems":3,"items":point_schema()}}})),
        "fallback":nullable(obj({"source_ms":TIME,"price":DECIMAL,"expires_ms":TIME,
                                  "references":window_map(point_schema())}))})
    references["allOf"]=[{"if":{"properties":{"status":{"const":"available"}}},
        "then":{"properties":{"reason":{"type":"null"},"ladder":{"type":"object"}}},
        "else":{"properties":{"reason":REASON,"ladder":{"type":"null"},"fallback":{"type":"null"}}}}]
    history=availability({"unit":{"enum":["fraction","ratio"]},"definition_version":TOKEN,
        "input_refs":{"type":"array","maxItems":8,"items":TOKEN,"uniqueItems":True},
        "points":{"type":"array","maxItems":96,"items":obj({"source_ms":TIME,"value":DECIMAL})}},
        {"points":{"minItems":1},"input_refs":{"minItems":1}})
    history["allOf"][0]["else"]["properties"]["points"]={"maxItems":0}
    since=availability({"state":nullable({"enum":CODES}),"price":nullable(DECIMAL),
        "at_ms":nullable(TIME),"asof_ms":nullable(TIME),"expires_ms":nullable(TIME),
        "definition_version":{"const":"signal_state.v1"},"input_refs":{"type":"array","maxItems":8,"items":TOKEN}},
        {"state":{"enum":CODES},"price":DECIMAL,"at_ms":TIME,"asof_ms":TIME,"expires_ms":TIME,"input_refs":{"minItems":1}})
    since["allOf"][0]["else"]["properties"].update({k:{"type":"null"} for k in ("state","price","at_ms")})
    row=obj({"id":ID,"price":typed_field("price","price_change.v1"),
        "changes":window_map(typed_field("fraction","price_change.v1")),
        "volume":obj({"quote_24h":typed_field("USDT","volume.rt.v1"),"venue":{"const":"Binance_USDT_perpetual"}}),
        "oi":obj({"quantity":typed_field("quantity","open_interest.v1"),
                  **{"change_"+h.lower():deepcopy(oi_change) for h in HORIZONS}}),
        "ls":obj({"global_ratio":typed_field("ratio","long_short.v1"),"top_ratio":typed_field("ratio","long_short.v1"),
            "global_long_share":typed_field("fraction","long_short.v1"),"top_long_share":typed_field("fraction","long_short.v1"),
            "global_share_change":window_map(typed_field("pp","long_short.v1")),
            "top_share_change":window_map(typed_field("pp","long_short.v1")),
            "divergence":typed_field("pp","long_short.v1")}),
        "funding":obj({"h":nullable(HOURS),"nft":nullable(TIME),
            "native_estimate":typed_field("fraction","funding.v1"),
            "cumulative_1d":typed_field("fraction","funding.v1"),"cumulative_7d":typed_field("fraction","funding.v1"),
            "cap":typed_field("fraction","funding.v1"),"floor":typed_field("fraction","funding.v1"),
            "at_cap":ref("flagField"),"at_floor":ref("flagField")}),
        "funding_change":funding_cycle,
        "volatility":obj({"score":deepcopy(level),"windows":window_map(level),
            "direction":string_field("state","volatility.v1",["Surging","Rising","Stable","Falling","Contracting"])}),
        "signal":obj({"code":string_field("state","signal.v1",CODES),
                      "category":string_field("state","signal.v1",sorted(set(CATEGORIES.values())))}),
        "signal_since":since,
        "context":obj({"rsi_14":typed_field("score","legacy_context.v1"),
            "volatility_momentum":typed_field("log_ratio","legacy_context.v1"),
            "volatility_velocity":string_field("state","legacy_context.v1",["Accelerating","Decelerating","Stable"])}),
        "outlook":obj({"label":string_field("state","legacy_outlook.v1",["Persist","Fade","Neutral"]),
            "confidence":typed_field("fraction","legacy_outlook.v1"),"description":string_field("text","legacy_outlook.v1")}),
        "history":obj({name:deepcopy(history) for name in ("volatility","ls","funding","funding_change","oi")}),
        "references":references})
    schema=obj({"schema":{"const":"pulse.snapshot.v1"},"mapping_version":TOKEN,"generation":TOKEN,
        "horizon":{"enum":list(HORIZONS)},"generated_ms":TIME,"expires_ms":TIME,
        "universe":{"type":"array","minItems":1,"maxItems":8192,"items":obj({"id":ID,
            "pair_id":PAIR,"symbol":SYMBOL,"funding_hours":nullable(HOURS)})},
        "rows":{"type":"array","minItems":1,"maxItems":8192,"items":row},
        "inputs":{"type":"object","minProperties":1,"maxProperties":65536,"propertyNames":TOKEN,
            "additionalProperties":obj({"version":TOKEN,"source_ms":TIME,"verified_at_ms":nullable(TIME),
                                        "expires_ms":TIME,"definition_version":TOKEN})}})
    # ADR-0120: optional for old logical fixtures; new live producers supply all windows.
    row["properties"]["ls"]["properties"]["period_ratios"]=window_map(typed_field("ratio","long_short.v1"))
    return {"$schema":DRAFT,"$id":SNAPSHOT,"title":"Self-contained Pulse snapshot v1 proposal",**schema}


def snapshot_fixture(horizon="1H",hours=8,identifier=0):
    def field(unit,value,definition): return value_field(unit,value,definition)
    def text(value,definition,unit="state"):
        return field(unit,value,definition)
    quantity=field("quantity","112000","open_interest.v1")
    oi={"quantity":quantity}
    for h,window in zip(HORIZONS,(3600000,14400000,86400000)):
        oi["change_"+h.lower()]={**field("fraction","0.12","open_interest.v1"),
            "baseline_asof_ms":NOW-1000-window,"actual_duration_ms":window,"baseline_error_ms":0,"mode":"own"}
    last=NOW-1000; due=last+hours*3600000
    mapping="fixture.mapping.v1"
    def funding_field(value,definition="funding.v1"):
        f=field("fraction",value,definition); f["expires_ms"]=due; return f
    dlast=funding_field("0.00005","funding_change.v1")
    basis=str(Decimal("0.00005")*10000*8/hours)
    dlast.update(level_basis=basis,level_basis_unit="bps",assurance="consecutive_verified")
    cumulative={}
    for period,n in (("1d",24//hours),("7d",168//hours)):
        f=funding_field(str(Decimal(n-1)*Decimal(".0001")+Decimal(".00015")))
        f["assurance"]="window_consistent_provisional"; cumulative["cumulative_"+period]=f
    windows={h:field("fraction",str(Decimal(".01")*(i+1)),"volatility.v1") for i,h in enumerate(HORIZONS)}
    def point(stamp): return {"status":"available","reason":None,"reference_minute_ms":stamp,"open_price":"100"}
    start=NOW//60000*60000
    ladder={"built_at_ms":NOW,"start_ms":start,"valid_until_ms":start+300000,"windows":list(HORIZONS),
            "slots":[[point(start+i*60000-w) for w in (3600000,14400000,86400000)] for i in range(5)]}
    fallback={"source_ms":NOW-1000,"price":"102","expires_ms":NOW+60000,
              "references":{h:point((NOW-1000-w)//60000*60000) for h,w in zip(HORIZONS,(3600000,14400000,86400000))}}
    row={"id":identifier,"price":field("price","102","price_change.v1"),
        "changes":{h:field("fraction","0.02","price_change.v1") for h in HORIZONS},
        "volume":{"quote_24h":field("USDT","1000000","volume.rt.v1"),"venue":"Binance_USDT_perpetual"},
        "oi":oi,"ls":{"global_ratio":field("ratio","1.5","long_short.v1"),
            "top_ratio":field("ratio","1.5","long_short.v1"),"global_long_share":field("fraction","0.6","long_short.v1"),
            "top_long_share":field("fraction","0.6","long_short.v1"),
            "global_share_change":{h:field("pp","1.0","long_short.v1") for h in HORIZONS},
            "top_share_change":{h:field("pp","1.0","long_short.v1") for h in HORIZONS},
            "divergence":field("pp","0","long_short.v1")},
        "funding":{"h":hours,"nft":due,"native_estimate":funding_field("0.0002"),**cumulative,
            "cap":funding_field("0.003"),"floor":funding_field("-0.003"),
            "at_cap":field("boolean",False,"funding.v1"),"at_floor":field("boolean",False,"funding.v1")},
        "funding_change":{"id":identifier,"mapping_version":mapping,"status":"available","reason":None,
            "h":hours,"nft":due,"settled_rate":"0.00015","settlement_ms":last,"verified_at_ms":NOW,
            "expires_ms":due+900000,"settled_last":dlast,
            "settled_1d":funding_field("0.00005","funding_change.v1"),
            "settled_7d":funding_field("0.00005","funding_change.v1"),
            "basis_bps":field("bps",basis,"funding_change.v1")},
        "volatility":{"score":deepcopy(windows[horizon]),"windows":windows,"direction":text("Rising","volatility.v1")},
        "signal":{"code":text("MomentumLong","signal.v1"),"category":text("Uptrend","signal.v1")},
        "signal_since":{"status":"available","reason":None,"state":"MomentumLong","price":"100",
            "at_ms":NOW-10000,"asof_ms":NOW-1000,"expires_ms":NOW+60000,"definition_version":"signal_state.v1",
            "input_refs":["fixture:source"]},
        "context":{"rsi_14":field("score","55","legacy_context.v1"),
            "volatility_momentum":field("log_ratio","0.189","legacy_context.v1"),
            "volatility_velocity":text("Accelerating","legacy_context.v1")},
        "outlook":{"label":text("Fade","legacy_outlook.v1"),"confidence":field("fraction","0.45","legacy_outlook.v1"),
            "description":text("Fictional protocol fixture; not a market outlook.","legacy_outlook.v1","text")},
        "history":{name:{"status":"unsupported","reason":"not_retained","unit":"ratio" if name=="ls" else "fraction",
            "definition_version":"history.fixture.v1","input_refs":[],"points":[]} for name in ("volatility","ls","funding","funding_change","oi")},
        "references":{"status":"available","reason":None,"mapping_version":mapping,"definition_version":"price_change.v1",
            "generation_id":"fixture.generation.v1","ladder":ladder,"fallback":fallback}}
    row["funding"]["native_estimate"]["expires_ms"]=min(NOW-1000+120000,due)
    for name in ("cap","floor"):
        row["funding"][name]["expires_ms"]=min(NOW-1000+120000,due)
    return {"schema":"pulse.snapshot.v1","mapping_version":mapping,"generation":"fixture.generation.v1","horizon":horizon,
        "generated_ms":NOW,"expires_ms":NOW+60000,
        "universe":[{"id":identifier,"pair_id":"B-BTC_USDT","symbol":"BTCUSDT","funding_hours":hours}],"rows":[row],
        "inputs":{"fixture:source":{"version":"fixture.input.v1","source_ms":NOW-1000,"verified_at_ms":NOW,
            "expires_ms":due+900000,"definition_version":"fixture.source.v1"}}}


def make_unavailable(snapshot):
    def visit(node):
        if isinstance(node,dict):
            if "unit" in node and "value" in node:
                node.update(status="unavailable",reason="source_unavailable",value=None,asof_ms=None,expires_ms=None,
                            input_refs=[],classification_status="uncalibrated",level=None,level_basis=None,level_basis_unit=None)
                if "mode" in node:
                    for key in ("mode","baseline_asof_ms","baseline_error_ms","actual_duration_ms"): node[key]=None
            else:
                for value in node.values(): visit(value)
        elif isinstance(node,list):
            for value in node: visit(value)
    result=deepcopy(snapshot); visit(result["rows"])
    for row in result["rows"]:
        cycle=row["funding_change"]
        cycle.update(status="unavailable",reason="settlement_unverified",settled_rate=None,
                     settlement_ms=None,verified_at_ms=None,expires_ms=None)
        row["signal_since"].update(status="unavailable",reason="state_unverified",state=None,price=None,
                                   at_ms=None,asof_ms=None,expires_ms=None,input_refs=[])
        row["references"].update(status="unavailable",reason="reference_missing",ladder=None,fallback=None)
    return result
