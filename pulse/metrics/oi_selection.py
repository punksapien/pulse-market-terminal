"""ADR-0064: own first, independent cold fallback; canonical math unchanged."""
from .open_interest import quantity_change


def change_with_baselines(current,own_baseline,cold_baseline,*,horizon,now_ms):
    own=quantity_change(current,own_baseline,horizon=horizon,mode="own",now_ms=now_ms)
    if own.reason not in ("baseline_missing","baseline_after_target","baseline_outside_tolerance"):
        return own
    return quantity_change(current,cold_baseline,horizon=horizon,mode="cold-start",now_ms=now_ms)
