"""ADR-0095 production row construction; no sample values or source calls."""
from copy import deepcopy
from decimal import Decimal

from .decimal_text import decimal_text
from .identity import digest, generation
from .validation import snapshot as validate

WINDOWS=("1H","4H","24H")
NUMERIC_UNITS=frozenset(("fraction","ratio","pp","price","quantity","USDT","bps","score","log_ratio"))
# ADR-0117: the container survives publication jitter; field clocks never change.
SNAPSHOT_ENVELOPE_MS = 180000


def unavailable(unit, definition, reason="source_unavailable"):
    return dict(status="unavailable",reason=reason,value=None,unit=unit,asof_ms=None,expires_ms=None,
        assurance=None,definition_version=definition,input_refs=[],classification_status="uncalibrated",
        level=None,level_basis=None,level_basis_unit=None)


def available(unit, definition, value, *, source_ms, expires_ms, input_refs, now_ms, **extra):
    if type(source_ms) is not int or type(expires_ms) is not int or source_ms>now_ms+5000 or not source_ms<expires_ms or now_ms>=expires_ms:
        return unavailable(unit,definition,"source_expired_or_future")
    result=unavailable(unit,definition)
    if type(value) is str and unit in NUMERIC_UNITS:
        # a numeric string from a caller (for example repr(float)) must meet the same grammar; plain strings keep their spelling
        try: value=decimal_text(Decimal(value))
        except (ValueError,ArithmeticError):
            return unavailable(unit,definition,"value_unrepresentable")
    elif type(value) in (float,int,Decimal):
        try: value=decimal_text(value)
        except ValueError:
            # one unrepresentable value (NaN, beyond the grammar's range) becomes an explicit unavailable field
            # instead of aborting the whole composition
            return unavailable(unit,definition,"value_unrepresentable")
    result.update(status="available",reason=None,value=value,asof_ms=source_ms,expires_ms=expires_ms,
                  input_refs=list(input_refs),**extra)
    return result


def empty_row(meta, mapping):
    def f(unit,definition): return unavailable(unit,definition)
    def numeric(unit,definition): return {h:f(unit,definition) for h in WINDOWS}
    oi={"quantity":f("quantity","open_interest.v1")}
    for h in WINDOWS:
        oi["change_"+h.lower()]={**f("fraction","open_interest.v1"),"baseline_asof_ms":None,
            "actual_duration_ms":None,"baseline_error_ms":None,"mode":None}
    return dict(id=meta["id"],price=f("price","price_change.v1"),changes=numeric("fraction","price_change.v1"),
        volume={"quote_24h":f("USDT","volume.rt.v1"),"venue":"Binance_USDT_perpetual"},oi=oi,
        ls={"global_ratio":f("ratio","long_short.v1"),"top_ratio":f("ratio","long_short.v1"),
            "period_ratios":numeric("ratio","long_short.v1"),
            "global_long_share":f("fraction","long_short.v1"),"top_long_share":f("fraction","long_short.v1"),
            "global_share_change":numeric("pp","long_short.v1"),"top_share_change":numeric("pp","long_short.v1"),
            "divergence":f("pp","long_short.v1")},
        funding={"h":meta["funding_hours"],"nft":None,"native_estimate":f("fraction","funding.v1"),
            "cumulative_1d":f("fraction","funding.v1"),"cumulative_7d":f("fraction","funding.v1"),
            "cap":f("fraction","funding.v1"),"floor":f("fraction","funding.v1"),
            "at_cap":f("boolean","funding.v1"),"at_floor":f("boolean","funding.v1")},
        funding_change={"id":meta["id"],"mapping_version":mapping,"status":"unavailable","reason":"settlement_unverified",
            "h":meta["funding_hours"],"nft":None,"settled_rate":None,"settlement_ms":None,"verified_at_ms":None,
            "expires_ms":None,"settled_last":f("fraction","funding_change.v1"),
            "settled_1d":f("fraction","funding_change.v1"),"settled_7d":f("fraction","funding_change.v1"),
            "basis_bps":f("bps","funding_change.v1")},
        volatility={"score":f("fraction","volatility.v1"),"windows":numeric("fraction","volatility.v1"),
            "direction":f("state","volatility.v1")},
        signal={"code":f("state","signal.v1"),"category":f("state","signal.v1")},
        signal_since={"status":"unavailable","reason":"state_unverified","state":None,"price":None,
            "at_ms":None,"asof_ms":None,"expires_ms":None,"definition_version":"signal_state.v1","input_refs":[]},
        context={"rsi_14":f("score","legacy_context.v1"),"volatility_momentum":f("log_ratio","legacy_context.v1"),
            "volatility_velocity":f("state","legacy_context.v1")},
        outlook={"label":f("state","legacy_outlook.v1"),"confidence":f("fraction","legacy_outlook.v1"),
            "description":f("text","legacy_outlook.v1")},
        history={name:{"status":"unsupported","reason":"not_retained","unit":"ratio" if name=="ls" else "fraction",
            "definition_version":"history.v1","input_refs":[],"points":[]} for name in ("volatility","ls","funding","funding_change","oi")},
        references={"status":"unavailable","reason":"reference_missing","mapping_version":mapping,
            "definition_version":"price_change.v1","generation_id":"pending","ladder":None,"fallback":None})


def build(universe, rows, inputs, *, horizon, now_ms,validate_now=True):
    if type(validate_now) is not bool:raise ValueError("snapshot_validation_policy")
    if horizon not in WINDOWS: raise ValueError("unsupported_horizon")
    mapping=digest(universe)
    result=dict(schema="pulse.snapshot.v1",mapping_version=mapping,generation="pending",horizon=horizon,
        generated_ms=now_ms,expires_ms=now_ms+SNAPSHOT_ENVELOPE_MS,universe=deepcopy(universe),rows=deepcopy(rows),inputs=deepcopy(inputs))
    for row in result["rows"]:
        row["volatility"]["score"]=deepcopy(row["volatility"]["windows"][horizon])
    result["generation"]=generation(result)
    for row in result["rows"]: row["references"]["generation_id"]=result["generation"]
    if validate_now:validate(result)
    return result
