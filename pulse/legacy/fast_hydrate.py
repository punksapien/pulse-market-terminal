"""ADR-0033: legacy price hydration with explicit HTTP/read/storage dependencies."""
import time

from pulse.config import TRACKED_TRADE_SIGNALS
from pulse.legacy.vol_core import _finite
from pulse.legacy.signal_state import _positive_finite_number, _matching_signal_state_fields, _signal_state_key


def _fast_hydrate(query, cache_key, resolution, horizon, *, resolve_report, http, storage):
    """HOT refresh: V3 prices only -> signal reclassification -> tiny delta.
    Takes ~0.5-1.5s. No OI fetching. No candle fetching.
    """
    _resolve_target_report = resolve_report
    requests = http
    VercelStorage = storage
    # The watermark must precede generation resolution. If a new generation is
    # published while this request is in flight, its later publication
    # watermark wins the Redis transaction.
    observation_started_at = time.time()
    now = observation_started_at
    base, provenance = _resolve_target_report(
        cache_key,
        resolution,
        horizon,
        allow_stale=True,
    )
    if not base:
        return {
            "ts": now,
            "type": "delta",
            "delta": {},
            "market_context": None,
            "status": "unavailable",
            "reason": provenance.get("reason"),
        }

    v3_data = {}
    try:
        resp = requests.get(
            "https://public.coindcx.com/market_data/v3/current_prices/futures/rt",
            timeout=5
        )
        if resp.status_code == 200:
            v3_data = resp.json().get("prices", {})
    except Exception as e:
        print(f"[FAST HYDRATE] CoinDCX V3 call failed: {e}")
        return {
            "ts": now,
            "type": "delta",
            "delta": {},
            "market_context": base.get("market_context"),
        }

    from pulse.legacy.signal_config import VolSignalConfig
    from pulse.legacy.signal_engine import enrich_token
    cfg = VolSignalConfig()
    delta = {}
    observations = []

    target_secs = base.get("meta", {}).get("target_seconds", 86400)

    for row in base.get("summary", []):
        inst = row.get("instrument", "")
        v3 = v3_data.get(inst)
        if not v3:
            continue

        updates = {}
        price_last_val = v3.get("ls")
        new_price = _positive_finite_number(price_last_val)
        if new_price and new_price != row.get("price_last"):
            updates["price_last"] = new_price

        pc = v3.get("pc")
        if pc is not None:
            pc_number = _finite(pc, None)
            if pc_number is not None:
                updates["price_change_24h"] = pc_number / 100.0

        efr = v3.get("efr")
        if efr is None:
            efr = v3.get("fr")
        if efr is not None:
            funding_number = _finite(efr, None)
            if funding_number is not None:
                updates["funding_rate"] = funding_number

        vol = v3.get("v")
        if vol is not None:
            volume_number = _finite(vol, None)
            if volume_number is not None and volume_number >= 0:
                updates["dollar_vol_24h"] = volume_number
                latest_p = (
                    new_price
                    or _positive_finite_number(row.get("price_last"))
                )
                if latest_p is not None:
                    updates["volume_24h"] = volume_number / latest_p
                else:
                    updates["volume_24h"] = volume_number
                updates["daily_volume"] = updates["volume_24h"]

        high_val = v3.get("h")
        if high_val is not None:
            high_number = _positive_finite_number(high_val)
            if high_number is not None:
                updates["high_24h"] = high_number

        low_val = v3.get("l")
        if low_val is not None:
            low_number = _positive_finite_number(low_val)
            if low_number is not None:
                updates["low_24h"] = low_number

        if updates:
            merged = {**row, **updates}
            enrich_token(merged, target_secs, cfg)
            
            updates["trade_signal"] = merged.get("trade_signal")
            updates["signal_spec"] = merged.get("signal_spec")
            updates["signal_readiness"] = merged.get("signal_readiness")
            updates["regime"] = merged.get("regime")
            updates["regime_icon"] = merged.get("regime_icon")
            updates["regime_color"] = merged.get("regime_color")
            updates["regime_band_pct"] = merged.get("regime_band_pct")
            updates["vol_direction"] = merged.get("vol_direction")
            updates["direction_icon"] = merged.get("direction_icon")
            updates["direction_color"] = merged.get("direction_color")
            updates["vol_velocity"] = merged.get("vol_velocity")
            updates["vol_acceleration"] = merged.get("vol_acceleration")
            updates["vol_momentum"] = merged.get("vol_momentum")
            updates["vol_outlook"] = merged.get("vol_outlook")
            updates["vol_outlook_confidence"] = merged.get("vol_outlook_confidence")
            updates["sizing"] = merged.get("sizing")
            updates["card_rationale"] = merged.get("card_rationale")
            updates["playbook_action"] = merged.get("playbook_action")
            updates["playbook_strategy"] = merged.get("playbook_strategy")
            updates["playbook_tactics"] = merged.get("playbook_tactics")

            delta[inst] = updates
            if (
                new_price is not None
                and updates.get("trade_signal") in TRACKED_TRADE_SIGNALS
            ):
                observations.append({
                    "instrument": inst,
                    "signal": updates["trade_signal"],
                    "price": new_price,
                })

    reconciliation = VercelStorage.reconcile_signal_states(
        _signal_state_key(resolution, horizon),
        observations,
        observation_started_at,
        generation_id=base.get("meta", {}).get("generation_id"),
    )
    if reconciliation.get("reason") == "superseded":
        # A newer live-price snapshot already won the Redis transaction. Do not
        # let this delayed HTTP response roll browser state back either.
        delta = {}
    else:
        signal_states = reconciliation.get("states", {})
        for instrument, updates in delta.items():
            fields = _matching_signal_state_fields(
                signal_states.get(instrument),
                updates.get("trade_signal"),
            )
            if fields:
                updates.update(fields)
            else:
                # Never retain a transition price across a state mismatch or a
                # Redis failure: hiding the line is safer than showing a lie.
                updates["signal_since_price"] = None
                updates["signal_since_at"] = None

    return {
        "ts": now,
        "type": "delta",
        "delta": delta,
        "market_context": base.get("market_context"),
        "generation_id": base.get("meta", {}).get("generation_id"),
        "freshness_status": base.get("meta", {}).get("freshness_status"),
    }

