"""ADR-0098 pure bridge to unchanged v1 scorer/signal functions."""
from copy import deepcopy
import math

from .rows import available,unavailable
from .semantics import CATEGORIES

RESOLUTIONS={"1H":"60","4H":"240","24H":"1D"}
SECONDS={"1H":3600,"4H":14400,"24H":86400}
LEVELS={"Calm":1,"Low":1,"Active":2,"Hot":3,"Extreme":4}


def evaluate(pair_id,candles,bulk_row,legacy_oi_history):
    """Caller validates source identity, history, freshness and full lookbacks first."""
    from pulse.metrics.volatility import evaluate_v1
    from pulse.legacy.scoring import LWConfig
    from pulse.legacy.vol_core import VolatilityConfig
    from pulse.legacy.signal_engine import enrich_token
    from pulse.legacy.signal_oi import compute_oi_metrics
    if bulk_row.get("mkt")!=pair_id[2:-5]+"USDT" or bulk_row.get("efr") is None:
        raise ValueError("legacy_live_inputs_unavailable")
    outputs={}
    for horizon,resolution in RESOLUTIONS.items():
        cfg=LWConfig(base=VolatilityConfig(resolution=resolution),scale_horizon=horizon)
        results,_,_=evaluate_v1({pair_id:candles[resolution]},cfg)
        token=deepcopy(results[pair_id])
        if "error" in token: raise ValueError("legacy_candle_evaluation_unavailable")
        token.update(compute_oi_metrics(legacy_oi_history,provider="binance",source_symbol=bulk_row["mkt"]))
        for source,target in (("ls","price_last"),("v","dollar_vol_24h"),("h","high_24h"),("l","low_24h"),("efr","funding_rate")):
            value=float(bulk_row[source])
            if not math.isfinite(value): raise ValueError("legacy_nonfinite_source")
            token[target]=value
        token["price_change_24h"]=float(bulk_row["pc"])/100
        if not math.isfinite(token["price_change_24h"]): raise ValueError("legacy_nonfinite_source")
        outputs[horizon]=enrich_token(token,SECONDS[horizon])
    return outputs


def attach(rows,outputs,*,input_ref,source_ms,expires_ms,now_ms):
    """Rows are one per selected horizon; no state store or implicit network calls."""
    for horizon,row in rows.items():
        token=outputs[horizon]
        def field(unit,definition,value,**extra):
            if hasattr(value,"item"): value=value.item()
            if isinstance(value,float) and not math.isfinite(value):
                return unavailable(unit,definition,"legacy_nonfinite_result")
            return available(unit,definition,value,source_ms=source_ms,expires_ms=expires_ms,
                             input_refs=[input_ref],now_ms=now_ms,assurance="legacy_v1",**extra)
        for window in RESOLUTIONS:
            state=outputs[window]["regime"]
            row["volatility"]["windows"][window]=field("fraction","volatility.v1",outputs[window]["volatility_score"],
                classification_status="classified",level=LEVELS[state],classification_version="volatility.v1.regime")
        row["volatility"]["direction"]=field("state","volatility.v1",token["vol_direction"])
        row["context"]["rsi_14"]=field("score","legacy_context.v1",token["rsi_14"])
        row["context"]["volatility_momentum"]=field("log_ratio","legacy_context.v1",token["vol_momentum"])
        velocity={"accelerating":"Accelerating","decelerating":"Decelerating","steady":"Stable"}[token["vol_velocity_label"]]
        row["context"]["volatility_velocity"]=field("state","legacy_context.v1",velocity)
        code=token["trade_signal"]
        row["signal"]["code"]=field("state","signal.v1",code)
        row["signal"]["category"]=field("state","signal.v1",CATEGORIES[code])
        if token["vol_outlook"] in ("Persist","Fade","Neutral"):
            row["outlook"]["label"]=field("state","legacy_outlook.v1",token["vol_outlook"])
            row["outlook"]["confidence"]=field("fraction","legacy_outlook.v1",token["vol_outlook_confidence"]/100)
            row["outlook"]["description"]=field("text","legacy_outlook.v1","; ".join(token["vol_outlook_reasons"]))
        else:
            for key,unit in (("label","state"),("confidence","fraction"),("description","text")):
                row["outlook"][key]=unavailable(unit,"legacy_outlook.v1","legacy_state_not_in_contract")
