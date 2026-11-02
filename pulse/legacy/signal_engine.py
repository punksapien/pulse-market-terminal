import math
import numpy as np
from typing import Any, Dict, List, Optional, Tuple

from .signal_config import VolSignalConfig

SECONDS_PER_YEAR = 31_536_000

REGIME_ICONS = {"Calm": "", "Low": "", "Active": "", "Hot": "", "Extreme": ""}
REGIME_COLORS = {
    "Calm": "#4b5563", "Low": "#22c55e", "Active": "#f59e0b",
    "Hot": "#f97316", "Extreme": "#ef4444",
}

DIRECTION_ICONS = {"Surging": "⇡", "Rising": "↗", "Stable": "→", "Falling": "↘", "Contracting": "↧"}
DIRECTION_COLORS = {
    "Surging": "#f97316", "Rising": "#f59e0b", "Stable": "#6b7280",
    "Falling": "#3b82f6", "Contracting": "#a855f7",
}

SIGNAL_SPEC = {
    "MomentumLong":  {"short": "M↑", "full": "MOMENTUM LONG",  "color": "#22c55e"},
    "MomentumShort": {"short": "M↓", "full": "MOMENTUM SHORT", "color": "#ef4444"},
    "ReversalLong":  {"short": "R↑", "full": "REVERSAL LONG",  "color": "#06b6d4"},
    "ReversalShort": {"short": "R↓", "full": "REVERSAL SHORT", "color": "#f43f5e"},
    "BreakoutWatch": {"short": "BW", "full": "BREAKOUT WATCH", "color": "#a855f7"},
    "Range":         {"short": "RG", "full": "RANGE",          "color": "#6b7280"},
    "Avoid":         {"short": "AV", "full": "AVOID",          "color": "#374151"},
}


def _oi_state(token: Dict) -> Tuple[bool, float]:
    explicit = token.get("oi_available")
    if explicit is None:
        history = token.get("oi_history")
        explicit = (
            bool(history)
            or token.get("oi_usd") not in (None, 0, 0.0)
            or token.get("oi_24h_change") not in (None, 0, 0.0)
        )
    try:
        value = float(token.get("oi_24h_change") or 0.0)
        if not math.isfinite(value):
            value = 0.0
    except (TypeError, ValueError):
        value = 0.0
    return bool(explicit), value


def classify_regime(vol_score: float, target_seconds: int = 86400, cfg: VolSignalConfig = None) -> str:
    if cfg is None: cfg = VolSignalConfig()
    scale = math.sqrt(target_seconds / SECONDS_PER_YEAR)
    thresholds = [t * scale for t in cfg.regime.annual_thresholds]
    if vol_score < thresholds[0]: return cfg.regime.names[0]
    if vol_score < thresholds[1]: return cfg.regime.names[1]
    if vol_score < thresholds[2]: return cfg.regime.names[2]
    if vol_score < thresholds[3]: return cfg.regime.names[3]
    return cfg.regime.names[4]


def regime_band_pct(vol_score: float, regime: str, target_seconds: int = 86400, cfg: VolSignalConfig = None) -> float:
    if cfg is None: cfg = VolSignalConfig()
    scale = math.sqrt(target_seconds / SECONDS_PER_YEAR)
    t = cfg.regime.annual_thresholds
    n = cfg.regime.names
    bands = {
        n[0]: (0, t[0]), n[1]: (t[0], t[1]), n[2]: (t[1], t[2]),
        n[3]: (t[2], t[3]), n[4]: (t[3], 2.00)
    }
    lo, hi = bands.get(regime, (0, 1))
    lo_s, hi_s = lo * scale, hi * scale
    rng = hi_s - lo_s
    if rng <= 0: return 1.0
    return max(0, min(1, (vol_score - lo_s) / rng))

def classify_direction(vol_1h: float, vol_4h: float, vol_24h: float,
                       vol_7d_avg: float, vol_pct_30d: float, cfg: VolSignalConfig = None) -> str:
    if cfg is None: cfg = VolSignalConfig()
    dcfg = cfg.direction
    
    lr_1_4 = math.log(vol_1h / vol_4h) if vol_1h > 0 and vol_4h > 0 else 0
    lr_4_24 = math.log(vol_4h / vol_24h) if vol_4h > 0 and vol_24h > 0 else 0
    lr_24_7d = math.log(vol_24h / vol_7d_avg) if vol_24h > 0 and vol_7d_avg > 0 else 0
    lr_1_24 = math.log(vol_1h / vol_24h) if vol_1h > 0 and vol_24h > 0 else 0

    # Robust checks: Immediate surge is extremely important for day traders
    if lr_1_4 > dcfg.surge_short_long_log_ratio or lr_1_24 > dcfg.surge_short_extreme_log_ratio:
        return "Surging"
    if lr_1_4 > dcfg.rise_short_vol_vel_min and lr_4_24 > dcfg.rise_mid_vol_vel_min:
        return "Rising"
    if lr_24_7d > dcfg.rise_long_macro_log_ratio and lr_4_24 > dcfg.rise_mid_long_log_ratio:
        return "Rising"
    if vol_pct_30d < dcfg.contracting_macro_percentile_max and lr_4_24 < dcfg.contracting_mid_long_log_ratio:
        return "Contracting"
    if lr_24_7d > dcfg.fall_long_macro_log_ratio and lr_4_24 < dcfg.fall_mid_long_log_ratio:
        return "Falling"
    return "Stable"

def compute_vol_dynamics(vol_1h: float, vol_4h: float, vol_24h: float,
                         vol_7d: float, cfg: VolSignalConfig = None) -> Dict[str, Any]:
    if cfg is None: cfg = VolSignalConfig()
    dy = cfg.dynamics
    
    ln_v1 = math.log(max(vol_1h, 1e-9))
    ln_v4 = math.log(max(vol_4h, 1e-9))
    ln_v24 = math.log(max(vol_24h, 1e-9))
    ln_v7 = math.log(max(vol_7d, 1e-9))

    vel_short = ln_v1 - ln_v4
    vel_long = ln_v4 - ln_v24
    accel = vel_short - vel_long
    momentum = ln_v24 - ln_v7

    return {
        "vol_velocity": round(vel_short, 4),
        "vol_acceleration": round(accel, 4),
        "vol_momentum": round(momentum, 4),
        "vol_velocity_label": "accelerating" if accel > dy.acceleration_min else "decelerating" if accel < dy.deceleration_max else "steady",
    }

def classify_trade_signal(token: Dict, cfg: VolSignalConfig = None) -> str:
    if cfg is None: cfg = VolSignalConfig()
    scfg = cfg.signal

    qs = token.get("quality_score", 1.0)
    dv = token.get("dollar_vol_24h", 1e9)
    if qs < scfg.min_quality_score: return "Avoid"
    if dv < scfg.min_dollar_vol_24h: return "Avoid"

    pc24 = token.get("price_change_24h", 0)
    pc1h = token.get("price_change_1h", 0)
    vdir = token.get("vol_direction", "Stable")
    oi_available, oi_chg = _oi_state(token)
    jf = token.get("jump_fraction", 0)
    fr = token.get("funding_rate", 0)
    regime = token.get("regime", "Active")
    vol_pct = token.get("vol_percentile_30d", 0.5)
    pt = token.get("price_tightening", False)

    # 1. Momentum Long (Aggressive Trend Following)
    if pc24 > scfg.mom_min_log_price_chg or pc1h > scfg.mom_min_1h_price_chg:
        if vdir in ("Surging", "Rising", "Stable") and regime in ("Active", "Hot", "Extreme"):
            if fr < scfg.mom_max_funding_long or (
                oi_available and oi_chg > scfg.mom_min_oi_chg
            ):
                return "MomentumLong"
        
    # 2. Momentum Short (Trend Following Short)
    if pc24 < -scfg.mom_min_log_price_chg or pc1h < -scfg.mom_min_1h_price_chg:
        if vdir in ("Surging", "Rising", "Stable") and regime in ("Active", "Hot", "Extreme"):
            if fr > scfg.mom_min_funding_short or (
                oi_available and oi_chg > scfg.mom_min_oi_chg
            ):
                return "MomentumShort"

    # 3. Reversal Long (Capitulation Buy)
    if pc24 < scfg.rev_min_log_price_drop or pc1h < scfg.rev_min_1h_price_drop:
        if jf > scfg.rev_min_jump_fraction and (
            (oi_available and oi_chg < scfg.rev_max_log_oi_chg)
            or fr < scfg.rev_max_funding_long
        ):
            return "ReversalLong"
        
    # 4. Reversal Short (Overextended Blow-off Top)
    if pc24 > scfg.rev_min_log_price_pump or pc1h > scfg.rev_min_1h_price_pump:
        if jf > scfg.rev_min_jump_fraction and (
            (oi_available and oi_chg < scfg.rev_min_oi_chg_short)
            or fr > scfg.rev_min_funding_short
        ):
            return "ReversalShort"

    # 5. Breakout Watch (Coiling Spring)
    if vdir == "Contracting" or (vol_pct < scfg.bw_max_macro_percentile and pt):
        if oi_available and oi_chg > scfg.bw_min_log_oi_chg:
            return "BreakoutWatch"

    return "Range"

def compute_vol_outlook(token: Dict, cfg: VolSignalConfig = None) -> Tuple[str, int, List[str]]:
    if cfg is None: cfg = VolSignalConfig()
    ocfg = cfg.outlook
    
    score = 0
    reasons = []
    oi_available, oi = _oi_state(token)
    fr = token.get("funding_rate", 0)
    v1, v4, v24 = token.get("vol_1h", 0), token.get("vol_4h", 0), token.get("vol_24h", 0)
    jf = token.get("jump_fraction", 0)
    qs = token.get("quality_score", 1)

    if oi_available:
        if oi > ocfg.oi_strong_rise_log:
            score += ocfg.oi_strong_rise_weight; reasons.append("OI rising — participation is real")
        elif oi > ocfg.oi_moderate_rise_log:
            score += ocfg.oi_moderate_rise_weight
        elif oi < ocfg.oi_strong_fall_log:
            score -= ocfg.oi_strong_fall_penalty; reasons.append("OI falling — vol may be exhausting")

    if abs(fr) < ocfg.funding_neutral_max_abs:
        score += ocfg.funding_neutral_weight; reasons.append("Funding neutral — no crowding to unwind")
    elif abs(fr) > ocfg.funding_extreme_min_abs:
        score -= ocfg.funding_extreme_penalty; reasons.append("Funding extreme — crowded side at risk")

    if v1 > v4 > v24:
        score += ocfg.vol_expansion_weight; reasons.append("Vol expanding across all timeframes")
    elif v1 < v4 and v4 > v24:
        score -= ocfg.vol_deceleration_penalty; reasons.append("Short-term vol decelerating")

    if jf < ocfg.jump_fraction_smooth_max:
        score += ocfg.smooth_trend_weight; reasons.append("Smooth trend — diffusion-driven")
    elif jf > ocfg.jump_fraction_heavy_min:
        score -= ocfg.heavy_jump_penalty; reasons.append("Jump-heavy — cascade vol tends to revert")

    if qs > ocfg.quality_score_high_min: score += ocfg.high_quality_weight

    vdir = token.get("vol_direction", "Stable")
    if vdir == "Contracting" and oi_available and oi > 0:
        return "Compression", min(80, score + ocfg.compression_bonus), reasons[:3]
    if score >= ocfg.persist_threshold:
        return "Persist", min(95, score), reasons[:3]
    if score <= ocfg.fade_threshold:
        return "Fade", min(90, 100 - score), reasons[:3]
    return "Fade", ocfg.fade_default_confidence, ["Mixed signals — low conviction outlook"]

def compute_readiness(token: Dict, signal: str, cfg: VolSignalConfig = None) -> int:
    if cfg is None: cfg = VolSignalConfig()
    rc = cfg.readiness

    s = 0
    vdir = token.get("vol_direction", "Stable")
    jf = token.get("jump_fraction", 0)
    oi_available, oi = _oi_state(token)
    qs = token.get("quality_score", 1)
    dv = token.get("dollar_vol_24h", 0)
    rsi = token.get("rsi_14", 50)
    vpct = token.get("vol_percentile_30d", 0.5)

    if signal in ("MomentumLong", "MomentumShort"):
        if vdir in ("Surging", "Rising"): s += rc.mom_surging_weight
        if jf < rc.mom_jump_max: s += rc.mom_jump_weight
        if oi_available and oi > rc.mom_oi_min: s += rc.mom_oi_weight
        if qs > rc.mom_qs_min: s += rc.mom_qs_weight
        if dv > rc.mom_dv_min: s += rc.mom_dv_weight
        va = token.get("vol_acceleration", 0)
        if va > rc.mom_accel_min: s += rc.mom_accel_weight
        vv = token.get("vol_velocity", 0)
        if vv > rc.mom_vel_min: s += rc.mom_vel_weight
    elif signal in ("ReversalLong", "ReversalShort"):
        if jf > rc.rev_jump_min: s += rc.rev_jump_weight
        if vdir in ("Falling", "Stable"): s += rc.rev_fade_weight
        if oi_available and oi < rc.rev_oi_max: s += rc.rev_oi_weight
        if abs(token.get("funding_rate", 0)) > rc.rev_fund_abs_min: s += rc.rev_fund_weight
        if qs > rc.rev_qs_min: s += rc.rev_qs_weight
        if rsi < rc.rev_rsi_oversold or rsi > rc.rev_rsi_overbought: s += rc.rev_rsi_weight
    elif signal == "BreakoutWatch":
        if vpct < rc.bw_vpct_max: s += rc.bw_vpct_weight
        if vdir == "Contracting": s += rc.bw_contracting_weight
        if oi_available and oi > rc.bw_oi_min: s += rc.bw_oi_weight
        if token.get("price_tightening"): s += rc.bw_tightening_weight
        if qs > rc.bw_qs_min: s += rc.bw_qs_weight
    return min(100, s)

def compute_sizing(token: Dict, cfg: VolSignalConfig = None) -> Dict[str, Any]:
    if cfg is None: cfg = VolSignalConfig()
    z = cfg.sizing

    score = token.get("volatility_score", 0.05)
    score = max(score, 0.001)  
    regime = token.get("regime", "Active")
    jf = token.get("jump_fraction", 0)
    qs = token.get("quality_score", 1)
    dv = token.get("dollar_vol_24h", 1e6)

    base_multiplier = z.target_daily_volatility / score

    if qs >= z.quality_excellent_min:
        qp = z.quality_excellent_penalty
    elif qs >= z.quality_fair_min:
        qp = z.quality_fair_penalty
    else:
        qp = z.quality_poor_penalty

    if dv >= z.liquidity_excellent_min_dv:
        lp = z.liquidity_excellent_penalty
    elif dv >= z.liquidity_fair_min_dv:
        lp = z.liquidity_fair_penalty
    else:
        lp = z.liquidity_poor_penalty

    jp = max(0.1, 1.0 - jf)

    combined = base_multiplier * qp * lp * jp
    combined = max(z.min_combined_multiplier, min(z.max_leverage_ceiling, combined))

    bvol = token.get("bvol_window", score)
    stop_dist = round(bvol * 1.5, 4) if bvol > 0 else 0

    leverage_ceiling = round(min(z.max_leverage_ceiling, 1.0 / score), 1)

    return {
        "base_vol_target": round(base_multiplier, 3),
        "regime_penalty": round(base_multiplier, 3),  
        "jump_penalty": round(jp, 3), 
        "quality_penalty": round(qp, 3),
        "liquidity_penalty": round(lp, 3), 
        "combined_modifier": round(combined, 3),
        "suggested_stop": stop_dist, 
        "leverage_ceiling": leverage_ceiling,
        "position_note": f"Targeting {z.target_daily_volatility*100:.1f}% daily vol → {combined:.2f}× base size",
    }


def generate_card_rationale(token: Dict, cfg: VolSignalConfig = None) -> str:
    if cfg is None: cfg = VolSignalConfig()
    rc = cfg.rationale

    jf = token.get("jump_fraction", 0)
    vdir = token.get("vol_direction", "Stable")
    oi_available, oi = _oi_state(token)
    fr = token.get("funding_rate", 0)
    v24 = token.get("vol_24h", 0)
    v7d = token.get("vol_7d_avg", 0.01)
    rsi = token.get("rsi_14", 50)
    regime = token.get("regime", "Active")
    
    # Safe log ratio
    ratio_log = math.log(max(v24, 1e-9) / max(v7d, 1e-9)) if v24 > 0 and v7d > 0 else 0
    ratio_display = v24 / max(v7d, 1e-9)

    if jf > rc.cascade_jump_min:
        return f"Cascade behavior — watch for reversal"
    if oi_available and vdir == "Contracting" and oi > rc.coil_oi_min:
        return "Vol coiling + OI building"
    if oi_available and oi > rc.surge_oi_min and vdir == "Surging":
        return "OI surging + vol expanding"
    if fr > rc.risk_funding_min and vdir in ("Rising", "Surging"):
        return "Funding crowded + vol rising — risk"
    if ratio_log > rc.ratio_extreme_log_min:
        return f"Vol {ratio_display:.1f}× normal — {regime} regime"
    if rsi < rc.rsi_oversold_max and vdir == "Falling":
        return "Oversold + vol fading — reversal watch"

    if not oi_available:
        return f"{vdir} vol; OI unavailable"
    oi_dir = "rising" if oi > rc.oi_rising_min else "falling" if oi < rc.oi_falling_max else "flat"
    return f"{vdir} vol, {oi_dir} OI"

def generate_quant_reasons(token: Dict, cfg: VolSignalConfig = None) -> List[Dict[str, str]]:
    if cfg is None: cfg = VolSignalConfig()
    qc = cfg.quant_reasons

    reasons = []
    oi_available, oi = _oi_state(token)
    fr = token.get("funding_rate", 0)
    jf = token.get("jump_fraction", 0)
    v24 = token.get("vol_24h", 0)
    v7d = token.get("vol_7d_avg", 0.01)
    v1h = token.get("vol_1h", 0)
    v4h = token.get("vol_4h", 0)
    rsi = token.get("rsi_14", 50)
    vpct = token.get("vol_percentile_30d", 0.5)
    pc24 = token.get("price_change_24h", 0)

    lr_1_4 = math.log(v1h / v4h) if v1h > 0 and v4h > 0 else 0
    lr_24_7 = math.log(v24 / v7d) if v24 > 0 and v7d > 0 else 0
    ratio_display = v24 / max(v7d, 1e-9)

    if oi_available:
        if oi > qc.oi_surging_min:
            reasons.append({"icon": "Open Interest", "text": f"Open Interest surging +{oi*100:.1f}% — strong directional conviction", "confidence": "strong"})
        elif oi > qc.oi_building_min:
            reasons.append({"icon": "Open Interest", "text": "Open Interest building steadily — participation real", "confidence": "strong"})
        elif oi < qc.oi_falling_max:
            reasons.append({"icon": "Open Interest", "text": "Open Interest falling — positions being closed", "confidence": "strong"})
        if oi < qc.oi_div_oi_max and pc24 > qc.oi_div_price_min:
            reasons.append({"icon": "Open Interest", "text": "Open Interest falling while price rises — divergence warning", "confidence": "strong"})

    if fr > qc.funding_extreme_long:
        reasons.append({"icon": "Funding Rate", "text": f"Funding Rate at {fr*100:.2f}% — longs crowded, squeeze risk", "confidence": "strong"})
    elif fr > qc.funding_elevated_long:
        reasons.append({"icon": "Funding Rate", "text": "Funding Rate elevated — some crowding in longs", "confidence": "moderate"})
    elif -qc.funding_neutral_max_abs < fr < qc.funding_neutral_max_abs:
        reasons.append({"icon": "Funding Rate", "text": "Funding Rate neutral — no crowding risk", "confidence": "strong"})
    elif fr < qc.funding_extreme_short:
        reasons.append({"icon": "Funding Rate", "text": f"Funding Rate negative {fr*100:.2f}% — shorts crowded", "confidence": "strong"})

    if lr_1_4 > qc.vol_short_accel_log_ratio:
        reasons.append({"icon": "Volatility", "text": "Short-term Volatility 1H accelerating hard", "confidence": "strong"})
    if lr_24_7 > qc.vol_macro_log_ratio:
        reasons.append({"icon": "Volatility", "text": f"Volatility 24H is {ratio_display:.1f}× its 7d average", "confidence": "strong"})
    if vpct < qc.vpct_coiling_max:
        reasons.append({"icon": "Volatility", "text": f"Volatility Score at {vpct*100:.0f}th percentile — coiling", "confidence": "strong"})

    if jf < qc.jump_smooth_max:
        reasons.append({"icon": "Price Jump Risk", "text": "Smooth diffusion-driven price action — stops likely safe", "confidence": "strong"})
    elif jf > qc.jump_cascade_min:
        reasons.append({"icon": "Price Jump Risk", "text": "Cascade behavior detected — stop slippage risk high", "confidence": "strong"})
    elif jf > qc.jump_moderate_min:
        reasons.append({"icon": "Price Jump Risk", "text": "Some discontinuous price moves — widen stops", "confidence": "moderate"})

    if rsi < qc.rsi_oversold_max:
        reasons.append({"icon": "RSI Momentum", "text": f"RSI Momentum at {rsi:.0f} — oversold, watch for bounce", "confidence": "moderate"})
    elif rsi > qc.rsi_overbought_min:
        reasons.append({"icon": "RSI Momentum", "text": f"RSI Momentum at {rsi:.0f} — overbought, watch for pullback", "confidence": "moderate"})

    return reasons[:5]

def compute_market_context(tokens: List[Dict], cfg: VolSignalConfig = None) -> Dict[str, Any]:
    if cfg is None: cfg = VolSignalConfig()
    cc = cfg.context

    n = len(tokens)
    if n == 0:
        return {"market_context_text": "No data available", "hot_extreme_count": 0}
    regimes = [t.get("regime", "Active") for t in tokens]
    dirs = [t.get("vol_direction", "Stable") for t in tokens]
    hot_ext = sum(1 for r in regimes if r in ("Hot", "Extreme"))
    surging_pct = sum(1 for d in dirs if d == "Surging") / max(n, 1)
    scores = [t.get("volatility_score", 0) for t in tokens]
    oi_available_count = sum(1 for token in tokens if _oi_state(token)[0])
    avg_score = sum(scores) / max(len(scores), 1)
    top = max(tokens, key=lambda t: t.get("volatility_score", 0))

    if hot_ext > n * cc.hot_extreme_ratio_threshold:
        status_tag = "[Active]"
        txt = f"{hot_ext} tokens HOT or above · Vol expanding in {surging_pct*100:.0f}% of pairs"
    elif hot_ext > 0:
        status_tag = "[Moderate]"
        txt = f"{hot_ext} tokens elevated · Market selectively active"
    else:
        status_tag = "[Calm]"
        txt = f"Mostly Calm · {sum(1 for r in regimes if r=='Active')} tokens active"

    return {
        "market_context_text": f"{status_tag} {txt}",
        "hot_extreme_count": hot_ext,
        "total_tracked": n,
        "surging_pct": round(surging_pct, 2),
        "market_avg_vol_score": round(avg_score, 4),
        "highest_vol_token": top.get("symbol", ""),
        "highest_vol_value": top.get("volatility_score", 0),
        "oi_available_count": oi_available_count,
        "oi_coverage_ratio": round(oi_available_count / max(n, 1), 4),
        "regime_counts": {r: regimes.count(r) for r in set(regimes)},
        "direction_counts": {d: dirs.count(d) for d in set(dirs)},
    }

def generate_actionable_playbook(token: Dict, cfg: VolSignalConfig = None) -> Dict[str, str]:
    if cfg is None: cfg = VolSignalConfig()
    pcfg = cfg.playbook

    outlook = token.get("vol_outlook", "Neutral")
    regime = token.get("regime", "Active")
    vdir = token.get("vol_direction", "Stable")
    price_change_24h = token.get("price_change_24h", 0)
    signal = token.get("trade_signal", "Range")
    oi_available, oi_change = _oi_state(token)
    
    # Analyze the trend
    if price_change_24h > pcfg.strong_uptrend_min:
        trend = "Strong Uptrend"
    elif price_change_24h > pcfg.moderate_uptrend_min:
        trend = "Moderate Uptrend"
    elif price_change_24h < pcfg.strong_downtrend_max:
        trend = "Strong Downtrend"
    elif price_change_24h < pcfg.moderate_downtrend_max:
        trend = "Moderate Downtrend"
    else:
        trend = "Consolidation / Rangebound"
        
    playbook_action = "Monitor for setup."
    playbook_strategy = "No high-conviction trade active."
    playbook_tactics = "Wait for clear volatility expansion."

    if signal == "MomentumLong":
        playbook_action = "Execute Long Momentum positions."
        playbook_strategy = (
            "Ride the breakout. Volume and Open Interest confirm strong directional conviction."
            if oi_available and oi_change > 0
            else "Ride the breakout. Price and volatility confirm directional momentum."
        )
        if outlook == "Persist":
            playbook_tactics = "Enter on minor pullbacks. Use wider stop below the 1H average true range. Target key resistance."
        else:
            playbook_tactics = "Scalp tight ranges. Scale out quickly as volatility shows signs of short-term exhaustion."
            
    elif signal == "MomentumShort":
        playbook_action = "Execute Short Momentum positions."
        playbook_strategy = (
            "Short aggressive breakdowns. Rising Open Interest confirms active participation."
            if oi_available and oi_change > 0
            else "Short aggressive breakdowns while downside volatility remains confirmed."
        )
        if outlook == "Persist":
            playbook_tactics = "Initiate short positions on minor retracements. Target standard support levels with standard risk."
        else:
            playbook_tactics = "Tight trailing stops are mandatory. Take profits aggressively on quick downward flushes."

    elif signal == "ReversalLong":
        playbook_action = "Accumulate for Reversal / Mean Reversion."
        playbook_strategy = "Long position on capitulation. High jump fraction suggests liquidation flush is nearing completion."
        playbook_tactics = "Limit orders at key historical support levels. Keep size small, as this is a counter-trend play."

    elif signal == "ReversalShort":
        playbook_action = "Distribute for Reversal / Mean Reversion."
        playbook_strategy = (
            "Short overextended pump. Exhaustion in buying momentum is paired with declining Open Interest."
            if oi_available and oi_change < 0
            else "Short overextended pump only after buying momentum confirms exhaustion."
        )
        playbook_tactics = "Scale in shorts near psychological round numbers. Place invalidation stop just above recent high."

    elif signal == "BreakoutWatch":
        playbook_action = "Set Breakout Alerts."
        playbook_strategy = "Vol compression phase. Open Interest building indicates massive accumulation preceding a breakout."
        playbook_tactics = "Place stop-entry orders above/below the tight 24h range boundaries. Position size for high leverage once triggered."

    else: # Range / Rangebound
        if regime in ("Extreme", "Hot"):
            playbook_action = "Scalp Range Boundaries."
            playbook_strategy = "Highly active range trading. Volatility is high but lacks a clear persistent trend."
            playbook_tactics = "Use mean-reversion oscillators (RSI, Bollinger Bands). Sell resistance, buy support with tight stops."
        else:
            playbook_action = "Avoid or Yield Farm."
            playbook_strategy = "Low volatility regime. No tradeable momentum or structural range setup available."
            playbook_tactics = "Deploy stablecoin yield strategy or allocate capital to high-volatility surging candidates."

    return {
        "trend_label": trend,
        "playbook_action": playbook_action,
        "playbook_strategy": playbook_strategy,
        "playbook_tactics": playbook_tactics
    }

def enrich_token(token: Dict, target_seconds: int = 86400, cfg: VolSignalConfig = None) -> Dict:
    if cfg is None: cfg = VolSignalConfig()
    
    vs = token.get("volatility_score", 0)
    regime = classify_regime(vs, target_seconds, cfg)
    token["regime"] = regime
    token["regime_icon"] = REGIME_ICONS.get(regime, "")
    token["regime_color"] = REGIME_COLORS.get(regime, "#6b7280")
    token["regime_band_pct"] = regime_band_pct(vs, regime, target_seconds, cfg)

    v1 = token.get("vol_1h", vs)
    v4 = token.get("vol_4h", vs)
    v24 = token.get("vol_24h", vs)
    v7d = token.get("vol_7d_avg", vs)
    vpct = token.get("vol_percentile_30d", 0.5)

    direction = classify_direction(v1, v4, v24, v7d, vpct, cfg)
    token["vol_direction"] = direction
    token["direction_icon"] = DIRECTION_ICONS.get(direction, "→")
    token["direction_color"] = DIRECTION_COLORS.get(direction, "#6b7280")

    dynamics = compute_vol_dynamics(v1, v4, v24, v7d, cfg)
    token.update(dynamics)

    signal = classify_trade_signal(token, cfg)
    token["trade_signal"] = signal
    token["signal_spec"] = SIGNAL_SPEC.get(signal, SIGNAL_SPEC["Range"])
    token["signal_readiness"] = compute_readiness(token, signal, cfg)

    outlook, confidence, reasons = compute_vol_outlook(token, cfg)
    token["vol_outlook"] = outlook
    token["vol_outlook_confidence"] = confidence
    token["vol_outlook_reasons"] = reasons

    token["sizing"] = compute_sizing(token, cfg)
    token["card_rationale"] = generate_card_rationale(token, cfg)
    token["quant_reasons"] = generate_quant_reasons(token, cfg)

    vol_avg = token.get("volume_vs_30d_avg", 0)
    if vol_avg == 0:
        v_24h = token.get("volume_24h", 0)
        token["volume_vs_30d_avg"] = 1.0  

    playbook = generate_actionable_playbook(token, cfg)
    token.update(playbook)

    return token
