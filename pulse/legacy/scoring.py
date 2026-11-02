import argparse
import json
import math
import re
import time
import warnings
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

from .vol_core import (
    VolatilityConfig,
    fetch_active_instruments,
    fetch_coindcx_candles,
    load_ohlcv,
    audit_data_quality,
    _yang_zhang_vol,
    _finite,
    _safe,
    _serialise,
    _resolve_date_range,
    _resolution_seconds,
    _annualization_from_index,
    _downsample_xy,
    _norm_col,
    _now_utc,
    _instrument_slug,
    _clean_numeric_series,
)

SCORE_BANDS_ANNUAL = [
    (0.0,  0.20, "Quiet"),
    (0.20, 0.40, "Stable"),
    (0.40, 0.75, "Active"),
    (0.75, 1.20, "High Risk"),
    (1.20, float("inf"), "Extreme Turbulence"),
]
BAND_COLORS = {
    "Quiet": "#19c7a0",
    "Stable": "#55a7ff",
    "Active": "#697386",
    "High Risk": "#ff9f43",
    "Extreme Turbulence": "#ff3d61",
}
DEFAULT_SCORE_WEIGHTS = {
    "alpha": 0.65,   
    "beta":  0.35,   
    "gamma": 1.0,    # BN-S jump correction intensity [0,1]
}
TIMEFRAME_OPTIONS = [
    {"value": "60", "label": "1h"},
    {"value": "240", "label": "4h"},
    {"value": "1D", "label": "1D"},
]
HORIZON_OPTIONS = [
    {"value": "4H", "label": "4h"},
    {"value": "12H", "label": "12h"},
    {"value": "24H", "label": "24h"},
    {"value": "30D", "label": "30d"},
    {"value": "1Y", "label": "1y"},
]
DEFAULT_DASHBOARD_INSTRUMENTS = [
    "B-ETH_USDT",
    "B-BTC_USDT",
    "B-XRP_USDT",
    "B-RAVE_USDT",
    "B-ZEC_USDT",
    "B-SOL_USDT",
]
EWMA_LAMBDA = 0.94
TABLE_PAGE_SIZE = 50
SECONDS_PER_YEAR = 31_536_000  # 365 * 24 * 3600

def _parse_horizon_to_seconds(horizon_str: str) -> int:
    s = horizon_str.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([mMhHdDwWyY])$", s)
    if not m:
        raise ValueError(
            f"Invalid horizon format: '{horizon_str}'. "
            "Expected e.g. '24H', '7d', '1M', '1Y', '15m'."
        )
    value = float(m.group(1))
    unit = m.group(2)
    multipliers = {
        "m": 60,                
        "h": 3600, "H": 3600,  
        "d": 86400, "D": 86400, 
        "w": 604800, "W": 604800, 
        "M": 2_592_000,        
        "y": SECONDS_PER_YEAR, "Y": SECONDS_PER_YEAR,  
    }
    if unit not in multipliers:
        raise ValueError(f"Unknown horizon unit: '{unit}'")
    return max(1, int(value * multipliers[unit]))


def _compute_scale_factor(candle_seconds: int, target_seconds: int) -> float:
    if candle_seconds <= 0 or target_seconds <= 0:
        return 1.0
    return math.sqrt(target_seconds / candle_seconds)


def _scale_bands(target_seconds: int) -> List[Tuple[float, float, str]]:
    h = math.sqrt(target_seconds / SECONDS_PER_YEAR)
    return [(lo * h, hi * h, label) for lo, hi, label in SCORE_BANDS_ANNUAL]


def _score_band(score: float, target_seconds: int) -> str:
    if not np.isfinite(score):
        return "Active"
    bands = _scale_bands(target_seconds)
    for lo, hi, label in bands:
        if lo <= score < hi:
            return label
    return "Extreme Turbulence"

@dataclass(frozen=True)
class LWConfig:
    base: VolatilityConfig
    scale_horizon: str = "24H"      
    yz_window: int = 21
    ewma_lambda: float = 0.94
    alpha: float = DEFAULT_SCORE_WEIGHTS["alpha"]   
    beta: float = DEFAULT_SCORE_WEIGHTS["beta"]     
    gamma: float = DEFAULT_SCORE_WEIGHTS["gamma"]   
    bvol_window_hours: float = 24.0
    daily_volume_days: int = 7
    min_volume_usd: float = 0.0  # millions
    output_json: str = "volatility_lw_report.json"
    output_csv: str = "volatility_lw_summary.csv"
    output_html: str = "volatility_lw_dashboard.html"

def _score_weights(cfg: LWConfig) -> Dict[str, float]:
    alpha = _finite(cfg.alpha, 0.65)
    beta = _finite(cfg.beta, 0.35)
    gamma = _finite(cfg.gamma, 1.0)
    if alpha < 0 or beta < 0:
        raise ValueError("alpha and beta must be non-negative")
    if alpha + beta <= 0:
        raise ValueError("alpha + beta must be > 0")
    if not (0.0 <= gamma <= 1.0):
        raise ValueError(f"gamma must be in [0, 1]; received {gamma}")
    return {"alpha": alpha, "beta": beta, "gamma": gamma}

def _window_label(hours: float) -> str:
    if not np.isfinite(hours) or hours <= 0:
        return "24h"
    if abs(hours - round(hours)) < 1e-9:
        hours_i = int(round(hours))
        if hours_i % 24 == 0:
            days = hours_i // 24
            return f"{days}d" if days != 1 else "24h"
        return f"{hours_i}h"
    return f"{hours:.2f}h".rstrip("0").rstrip(".")


def normalize_instrument_lw(value: str, margin_currency: str = "USDT") -> str:
    """Normalize user-facing symbols such as BTC into CoinDCX futures pairs."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""

    margin = re.sub(r"[^A-Z0-9]+", "", str(margin_currency or "USDT").upper()) or "USDT"
    token = raw.replace("/", "_").replace(":", "_")
    if token.startswith("B-"):
        token = token[2:]
    elif token.startswith("B_"):
        token = token[2:]
    token = token.replace("-", "_")
    token = re.sub(r"[^A-Z0-9_]+", "", token)
    token = re.sub(r"_+", "_", token).strip("_")
    if not token:
        return ""

    if "_" in token:
        base, quote = token.split("_", 1)
        quote = quote or margin
    elif token.endswith(margin) and len(token) > len(margin):
        base, quote = token[: -len(margin)], margin
    else:
        base, quote = token, margin
    return f"B-{base}_{quote}"


def normalize_instruments_lw(
    values: Optional[List[str]], margin_currency: str = "USDT",
) -> Optional[List[str]]:
    if not values:
        return None
    normalized = [
        inst for inst in (normalize_instrument_lw(v, margin_currency) for v in values) if inst
    ]
    return sorted(set(normalized)) if normalized else None


def fetch_candles_data(
    pair: str,
    cfg: VolatilityConfig,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """Fetch normalized CoinDCX candle data for dashboard refresh/API calls."""
    instrument = normalize_instrument_lw(pair, cfg.margin_currency)
    if not instrument:
        return pd.DataFrame()
    return fetch_coindcx_candles(instrument, cfg, session=session)


def _daily_volume_config(cfg: LWConfig) -> VolatilityConfig:
    end_day = (
        datetime.strptime(cfg.base.end_date, "%Y-%m-%d").date()
        if cfg.base.end_date
        else _now_utc().date()
    )
    start_day = end_day - timedelta(days=max(2, int(cfg.daily_volume_days)))
    return replace(
        cfg.base,
        resolution="1D",
        start_date=start_day.isoformat(),
        end_date=cfg.base.end_date,
        min_bars=1,
    )


def fetch_daily_volume_data(
    pair: str,
    cfg: LWConfig,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    return fetch_candles_data(pair, _daily_volume_config(cfg), session=session)


def _latest_daily_volume(raw_df: Optional[pd.DataFrame]) -> Tuple[float, Optional[str]]:
    if raw_df is None or raw_df.empty:
        return np.nan, None
    df = raw_df.copy()
    df.columns = [_norm_col(c) for c in df.columns]
    if "volume" not in df.columns:
        return np.nan, None

    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    elif "time" in df.columns:
        ts = pd.to_datetime(df["time"], unit="ms", utc=True, errors="coerce")
    else:
        ts = pd.Series(pd.NaT, index=df.index)

    vol = _clean_numeric_series(df["volume"])
    clean = pd.DataFrame({"timestamp": ts, "volume": vol}).dropna(subset=["volume"])
    if clean.empty:
        return np.nan, None
    clean = clean.sort_values("timestamp", na_position="first")
    row = clean.iloc[-1]
    stamp = None if pd.isna(row["timestamp"]) else str(pd.Timestamp(row["timestamp"]))
    return float(row["volume"]), stamp

def compute_ewma_vol(
    returns: pd.Series, lam: float = 0.94, scale_factor: float = 1.0,
) -> pd.Series:
    r = returns.replace([np.inf, -np.inf], np.nan).fillna(0.0).values
    n = len(r)
    if n < 2:
        return pd.Series(np.nan, index=returns.index)
    var = np.zeros(n)
    var[0] = r[0] ** 2
    for t in range(1, n):
        var[t] = lam * var[t - 1] + (1 - lam) * r[t - 1] ** 2
    sigma = np.sqrt(np.maximum(var, 0.0))
    return pd.Series(sigma * scale_factor, index=returns.index)

# def compute_bvol_window(
#     returns: pd.Series,
#     candle_seconds: int,
#     window_hours: float = 24.0,
#     scale_factor: float = 1.0,
# ) -> pd.Series:
#     bars_per_window = max(2, int(round(window_hours * 3600 / max(candle_seconds, 1))))
#     bvol = (
#         returns.rolling(bars_per_window, min_periods=max(2, bars_per_window // 2)).std()
#         * scale_factor
#     )
#     return bvol

def compute_bvol_window(
    returns: pd.Series,
    candle_seconds: int,
    window_hours: float = 24.0,
    scale_factor: float = 1.0,
) -> pd.Series:
    bars_per_window = max(2, int(round(window_hours * 3600 / max(candle_seconds, 1))))
    bvol = (
        returns.rolling(bars_per_window, min_periods=max(2, bars_per_window // 2)).std(ddof=1)
        * scale_factor
    )
    return bvol


def compute_jump_variation(returns: pd.Series) -> Dict[str, float]:
    r = returns.replace([np.inf, -np.inf], np.nan).dropna().values
    n = len(r)
    if n < 10:
        return {"rv": np.nan, "bv": np.nan, "jump_var": np.nan, "jump_fraction": np.nan}
    abs_r = np.abs(r)
    rv = float(np.sum(r ** 2))
    bv = float((math.pi / 2.0) * np.sum(abs_r[1:] * abs_r[:-1]))
    jump_var = max(rv - bv, 0.0)
    jump_frac = float(jump_var / rv) if rv > 0 else 0.0
    return {"rv": rv, "bv": bv, "jump_var": jump_var, "jump_fraction": jump_frac}


def _clamp_float(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _actionable_guidance(
    row: pd.Series,
    median_score: float,
    median_dollar_vol: float,
) -> Dict[str, Any]:
    """Translate the risk score into compact trading risk controls."""
    score = _finite(row.get("volatility_score"), 0.0)
    jump = _finite(row.get("jump_fraction"), 0.0)
    quality = _finite(row.get("quality_score"), 1.0)
    dollar_vol = _finite(row.get("dollar_vol_24h"), np.nan)
    band = str(row.get("score_band") or "Active")

    ref_score = median_score if np.isfinite(median_score) and median_score > 0 else 0.04
    size_mult = _clamp_float(ref_score / max(score, 1e-9), 0.08, 1.35)
    leverage_mult = _clamp_float(0.04 / max(score, 1e-9), 0.05, 2.50)

    if jump >= 0.30:
        size_mult *= 0.55
        leverage_mult *= 0.60
        jump_note = "high jump risk"
    elif jump >= 0.15:
        size_mult *= 0.75
        leverage_mult *= 0.80
        jump_note = "elevated jump risk"
    else:
        jump_note = "smooth-vol dominated"

    if quality < 0.95:
        size_mult *= 0.70
        leverage_mult *= 0.75
        trust = "haircut signal; data quality is weak"
    elif quality < 0.99:
        size_mult *= 0.88
        leverage_mult *= 0.90
        trust = "use a small quality haircut"
    else:
        trust = "quality clean"

    if np.isfinite(dollar_vol) and np.isfinite(median_dollar_vol) and median_dollar_vol > 0:
        if dollar_vol < 0.45 * median_dollar_vol:
            size_mult *= 0.70
            leverage_mult *= 0.80
            liquidity = "thin versus this universe"
        elif dollar_vol > 1.6 * median_dollar_vol:
            liquidity = "strong versus this universe"
        else:
            liquidity = "usable versus this universe"
    else:
        liquidity = "liquidity unknown"

    size_mult = _clamp_float(size_mult, 0.03, 1.50)
    leverage_mult = _clamp_float(leverage_mult, 0.03, 3.00)

    if band == "Extreme Turbulence" or jump >= 0.30:
        stop_policy = "Use wide volatility-aware stops or stand aside."
        strategy_bias = "Only shock-regime momentum or breakout systems should participate."
        posture = "defensive"
        participation_rule = "Trade only confirmed shock-regime setups."
        leverage_rule = "Keep leverage well below normal and do not average into spikes."
    elif band == "High Risk" or jump >= 0.20:
        stop_policy = "Widen stops and require a clear invalidation level."
        strategy_bias = "Prefer confirmed momentum; avoid tight mean reversion."
        posture = "reduced participation"
        participation_rule = "Reduce participation and demand stronger signal quality."
        leverage_rule = "Cap leverage below the normal playbook."
    elif band == "Active":
        stop_policy = "Use moderately wider volatility stops."
        strategy_bias = "Momentum or selective mean reversion can work with confirmation."
        posture = "selective"
        participation_rule = "Trade selectively and watch execution costs."
        leverage_rule = "Keep leverage near normal only when liquidity is strong."
    elif band == "Stable":
        stop_policy = "Standard volatility stops are acceptable."
        strategy_bias = "Normal playbook if spread, depth, and signal agree."
        posture = "normal"
        participation_rule = "Normal participation is acceptable."
        leverage_rule = "Use normal leverage caps, still anchored to observed volatility."
    else:
        stop_policy = "Tighter stops can work, but quiet is not safety."
        strategy_bias = "Range or mean reversion can work if liquidity holds."
        posture = "normal to opportunistic"
        participation_rule = "Normal participation is acceptable; avoid complacency."
        leverage_rule = "Do not increase leverage just because the regime is quiet."

    if "thin" in liquidity:
        strategy_bias = "Avoid execution-sensitive setups and use smaller orders."
        participation_rule = "Participation should be small or skipped until liquidity improves."

    execution_rule = (
        "Use smaller orders and expect slippage."
        if "thin" in liquidity or jump >= 0.25
        else "Standard execution is acceptable if spreads stay controlled."
    )
    action_summary = (
        f"{posture.title()} regime. {participation_rule} {stop_policy}"
    )
    return {
        "position_size_mult": float(size_mult),
        "leverage_cap_mult": float(leverage_mult),
        "risk_posture": posture,
        "participation_rule": participation_rule,
        "leverage_rule": leverage_rule,
        "stop_policy": stop_policy,
        "strategy_bias": strategy_bias,
        "execution_rule": execution_rule,
        "liquidity_label": liquidity,
        "data_trust_label": trust,
        "jump_label": jump_note,
        "action_summary": action_summary,
    }

def analyse_instrument_lw(
    instrument: str,
    raw_df: pd.DataFrame,
    cfg: LWConfig,
    daily_volume_df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"instrument": instrument}
    if raw_df is None or raw_df.empty:
        result["error"] = "no data"
        return result
    try:
        df, freq_label, ppy = load_ohlcv(raw_df)
    except Exception as exc:
        result["error"] = f"load failed: {exc}"
        return result

    quality = audit_data_quality(df)
    n = len(df)
    r = df["ret_cc"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < max(20, min(cfg.base.min_bars, 40)):
        result["error"] = f"insufficient bars: {len(r)}"
        result["quality"] = quality
        return result

    candle_secs = _resolution_seconds(cfg.base.resolution)
    target_secs = _parse_horizon_to_seconds(cfg.scale_horizon)

    S = _compute_scale_factor(candle_secs, target_secs)
    # S = math.sqrt(1440) #1m-24h scaling bars

    o, h, lo, c = df["open"], df["high"], df["low"], df["close"]

    yz_series = (_yang_zhang_vol(o, h, lo, c, cfg.yz_window) * S).replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    sigma_yz = float(yz_series.iloc[-1]) if len(yz_series) else np.nan

    ewma_series = compute_ewma_vol(r, lam=cfg.ewma_lambda, scale_factor=S)
    sigma_ewma = float(ewma_series.iloc[-1]) if len(ewma_series) else np.nan

    jump = compute_jump_variation(r)
    jump_fraction = jump["jump_fraction"]

    bvol_series = compute_bvol_window(
        r, candle_secs, cfg.bvol_window_hours, scale_factor=S,
    )
    bvol_window = float(bvol_series.iloc[-1]) if len(bvol_series.dropna()) else np.nan

    bars_per_day = 86400.0 / max(candle_secs, 1)
    volume_24h, volume_timestamp = _latest_daily_volume(daily_volume_df)
    volume_source = "daily_1d_candle"
    if not np.isfinite(volume_24h):
        volume_24h = float(df["volume"].tail(int(max(1, bars_per_day))).sum())
        volume_timestamp = str(df.index.max())
        volume_source = "rolling_candle_volume"

    price_last = float(c.iloc[-1])
    dollar_vol_24h = (
        float(volume_24h * price_last)
        if np.isfinite(volume_24h) and np.isfinite(price_last)
        else np.nan
    )

    # ── Multi-timeframe vol (1h, 4h, 24h, 7d) ──
    def _vol_at_window(bars_count):
        if len(r) < bars_count or bars_count < 1:
            return np.nan
        sub = r.iloc[-bars_count:]
        if len(sub) >= 2:
            return float(sub.std(ddof=1) * S)
        elif len(sub) == 1:
            # Unbiased estimator of volatility from a single return under normality blended with Parkinson:
            # Parkinson range estimator: sqrt((ln(H/L))^2 / (4 * ln(2)))
            # Close-to-close estimator: |r_t| * sqrt(pi/2)
            try:
                high_val = float(h.iloc[-1])
                low_val = float(lo.iloc[-1])
                if low_val > 0 and high_val >= low_val:
                    parkinson_val = float(math.sqrt((math.log(high_val / low_val))**2 / (4 * math.log(2))) * S)
                else:
                    parkinson_val = 0.0
            except Exception:
                parkinson_val = 0.0
            
            close_to_close_val = float(abs(sub.iloc[0]) * 1.2533141373 * S)
            return 0.5 * parkinson_val + 0.5 * close_to_close_val
        return np.nan

    bars_1h = max(1, int(3600 / max(candle_secs, 1)))
    bars_4h = max(bars_1h + 1, int(4 * 3600 / max(candle_secs, 1)))
    bars_24h = max(bars_4h + 1, int(24 * 3600 / max(candle_secs, 1)))
    bars_7d = max(bars_24h + 1, int(7 * 24 * 3600 / max(candle_secs, 1)))
    bars_30d = max(bars_7d + 1, int(30 * 24 * 3600 / max(candle_secs, 1)))

    vol_1h = _vol_at_window(bars_1h)
    vol_4h = _vol_at_window(bars_4h)
    vol_24h_raw = _vol_at_window(bars_24h)
    vol_7d = _vol_at_window(bars_7d)
    vol_30d = _vol_at_window(bars_30d)
    vol_7d_avg = vol_7d if np.isfinite(vol_7d) else (vol_24h_raw if np.isfinite(vol_24h_raw) else 0.01)

    # Vol percentile vs 30d history
    vol_percentile_30d = 0.5
    if len(bvol_series.dropna()) > 20:
        bvol_arr = bvol_series.dropna().values
        current_bvol = bvol_arr[-1]
        vol_percentile_30d = float(np.mean(bvol_arr <= current_bvol))

    # ── RSI-14 ──
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    rsi_14 = float(rsi_series.iloc[-1]) if len(rsi_series.dropna()) > 0 else 50.0

    # ── Price changes (Continuous Log Returns) ──
    price_first_24h = float(c.iloc[-bars_24h]) if len(c) > bars_24h else float(c.iloc[0])
    price_first_1h = float(c.iloc[-bars_1h]) if len(c) > bars_1h else float(c.iloc[0])
    price_change_24h = float(np.log(price_last / max(price_first_24h, 1e-9)))
    price_change_1h = float(np.log(price_last / max(price_first_1h, 1e-9)))


    # ── Price tightening (ATR narrowing last 6h) ──
    bars_6h = max(1, int(6 * 3600 / max(candle_secs, 1)))
    price_tightening = False
    if "atr_14" in df.columns and len(df) > bars_6h:
        atr_tail = df["atr_14"].iloc[-bars_6h:]
        if len(atr_tail.dropna()) > 5:
            atr_first = float(atr_tail.iloc[:len(atr_tail)//3].mean())
            atr_last = float(atr_tail.iloc[-len(atr_tail)//3:].mean())
            if atr_first > 0 and atr_last < atr_first * 0.85:
                price_tightening = True

    # ── Sparkline (24 hourly vol points) ──
    vol_sparkline_24h = []
    if len(bvol_series.dropna()) > 24:
        step = max(1, len(bvol_series.dropna()) // 24)
        spark = bvol_series.dropna().values[::step][-24:]
        vol_sparkline_24h = [round(float(v), 5) for v in spark if np.isfinite(v)]

    # ── Symbol extraction ──
    symbol = instrument.replace("B-", "").split("_")[0]

    result.update({
        "meta": {
            "n_bars": n, "freq": freq_label, "ppy": ppy,
            "candle_seconds": candle_secs,
            "scale_factor": S,
            "start": str(df.index.min()), "end": str(df.index.max()),
            "volume_source": volume_source,
        },
        "symbol": symbol,
        "quality": quality,
        "sigma_yz": sigma_yz,
        "sigma_ewma": sigma_ewma,
        "jump_fraction": jump_fraction,
        "jump_var": jump["jump_var"],
        "rv": jump["rv"],
        "bv": jump["bv"],
        "bvol_window": bvol_window,
        "bvol_window_label": _window_label(cfg.bvol_window_hours),
        "volume_24h": volume_24h,
        "daily_volume": volume_24h,
        "dollar_vol_24h": dollar_vol_24h,
        "daily_notional_24h": dollar_vol_24h,
        "volume_timestamp": volume_timestamp,
        "volume_source": volume_source,
        "price_last": price_last,
        "quality_score": quality.get("quality_score", np.nan),
        # Multi-TF vol
        "vol_1h": vol_1h if np.isfinite(vol_1h) else sigma_yz,
        "vol_4h": vol_4h if np.isfinite(vol_4h) else sigma_yz,
        "vol_24h": vol_24h_raw if np.isfinite(vol_24h_raw) else sigma_yz,
        "vol_7d": vol_7d if np.isfinite(vol_7d) else sigma_yz,
        "vol_7d_avg": vol_7d_avg,
        "vol_30d_avg": vol_30d if np.isfinite(vol_30d) else vol_7d_avg,
        "vol_percentile_30d": vol_percentile_30d,
        # RSI
        "rsi_14": rsi_14,
        # Price
        "price_change_24h": round(price_change_24h, 4),
        "price_change_1h": round(price_change_1h, 4),
        "price_tightening": price_tightening,
        # Sparkline
        "vol_sparkline_24h": vol_sparkline_24h,
        "timeseries": {
            "yz": _downsample_xy(yz_series.index, yz_series.values, cfg.base.max_chart_points) if len(yz_series) else {},
            "ewma": _downsample_xy(ewma_series.index, ewma_series.values, cfg.base.max_chart_points) if len(ewma_series) else {},
            "bvol": _downsample_xy(bvol_series.dropna().index, bvol_series.dropna().values, cfg.base.max_chart_points) if len(bvol_series.dropna()) else {},
            "close": _downsample_xy(df.index, c.values, cfg.base.max_chart_points),
            "volume": _downsample_xy(df.index, df["volume"].values, cfg.base.max_chart_points),
        },
    })
    return result

def score_cross_section(
    results: Dict[str, Dict[str, Any]], cfg: LWConfig,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Compute absolute volatility scores — no cross-sectional ranking.

    Mathematical basis (Barndorff-Nielsen & Shephard decomposition):
        RV  = BV + JV            (Realized = Bipower + Jump variance)
        J   = JV / RV            (jump fraction, dimensionless)
        sigma_c ~ sigma_base     (YZ/EWMA estimate the continuous component)
        sigma_total = sigma_c / sqrt(1-J)   (total risk including jumps)

    With user-controlled intensity gamma in [0,1]:
        vol_score = sigma_base / sqrt(1 - gamma * J)

    gamma = 0  ->  ignore jumps (smooth vol surface assumption)
    gamma = 1  ->  full BN-S correction (rough vol surface)
    """
    weights = _score_weights(cfg)
    alpha, beta, gamma = weights["alpha"], weights["beta"], weights["gamma"]
    ab_sum = alpha + beta
    target_secs = _parse_horizon_to_seconds(cfg.scale_horizon)

    rows = []
    for inst, info in results.items():
        if "error" in info:
            continue
        bvol_window = _finite(info.get("bvol_window", info.get("bvol_24h")))
        rows.append({
            "instrument": inst,
            "sigma_yz": _finite(info.get("sigma_yz")),
            "sigma_ewma": _finite(info.get("sigma_ewma")),
            "jump_fraction": _finite(info.get("jump_fraction")),
            "bvol_window": bvol_window,
            "volume_24h": _finite(info.get("volume_24h", info.get("daily_volume"))),
            "dollar_vol_24h": _finite(info.get("dollar_vol_24h", info.get("daily_notional_24h"))),
            "price_last": _finite(info.get("price_last")),
            "quality_score": _finite(info.get("quality_score")),
            "n_bars": info.get("meta", {}).get("n_bars", 0),
        })
    if not rows:
        return [], {}

    df = pd.DataFrame(rows)

    yz_fill = df["sigma_yz"].fillna(0.0)
    ew_fill = df["sigma_ewma"].fillna(0.0)
    df["sigma_base"] = (alpha * yz_fill + beta * ew_fill) / ab_sum     #alpha + beta = 1

    jf = df["jump_fraction"].fillna(0.0).clip(0.0, 0.95)
    df["jump_correction"] = 1.0 / np.sqrt(np.maximum(1.0 - gamma * jf, 0.05))
    df["volatility_score"] = df["sigma_base"] * df["jump_correction"]

    df["score_band"] = df["volatility_score"].apply(
        lambda v: _score_band(v, target_secs)
    )
    df["vol_score_rank"] = (
        df["volatility_score"].rank(ascending=False, method="min").astype(int)
    )

    median_score = float(df["volatility_score"].median())
    median_dollar_vol = (
        float(df["dollar_vol_24h"].median())
        if df["dollar_vol_24h"].notna().any()
        else np.nan
    )
    guidance = df.apply(
        lambda row: _actionable_guidance(row, median_score, median_dollar_vol),
        axis=1,
    )
    if len(guidance):
        guidance_df = pd.DataFrame(list(guidance), index=df.index)
        for col in guidance_df.columns:
            df[col] = guidance_df[col]

    summary_rows = df.sort_values("volatility_score", ascending=False).to_dict("records")

    score_map = {r["instrument"]: r for r in summary_rows}
    for inst, info in results.items():
        if inst in score_map:
            sc = score_map[inst]
            info["volatility_score"] = sc["volatility_score"]
            info["sigma_base"] = sc["sigma_base"]
            info["jump_correction"] = sc["jump_correction"]
            info["score_band"] = sc["score_band"]
            info["bvol_window"] = sc["bvol_window"]
            info["bvol_window_label"] = _window_label(cfg.bvol_window_hours)
            info.pop("bvol_24h", None)
            info["volume_24h"] = sc["volume_24h"]
            info["daily_volume"] = sc["volume_24h"]
            info["dollar_vol_24h"] = sc.get("dollar_vol_24h")
            info["daily_notional_24h"] = sc.get("dollar_vol_24h")
            info["vol_score_rank"] = sc["vol_score_rank"]
            for key in (
                "position_size_mult", "leverage_cap_mult", "risk_posture",
                "participation_rule", "leverage_rule", "stop_policy",
                "strategy_bias", "execution_rule", "liquidity_label",
                "data_trust_label", "jump_label", "action_summary",
            ):
                if key in sc:
                    info[key] = sc[key]

    cs = {
        "n_instruments": len(df),
        "band_counts": df["score_band"].value_counts().to_dict(),
        "median_score": median_score,
        "mean_score": float(df["volatility_score"].mean()),
        "median_bvol": float(df["bvol_window"].median()) if df["bvol_window"].notna().any() else np.nan,
        "median_volume_24h": float(df["volume_24h"].median()) if df["volume_24h"].notna().any() else np.nan,
        "median_dollar_vol_24h": median_dollar_vol,
        "bvol_window_label": _window_label(cfg.bvol_window_hours),
        "score_weights": weights,
        "mean_jump_fraction": float(df["jump_fraction"].mean()) if df["jump_fraction"].notna().any() else np.nan,
        "high_risk_pct": float(
            df["score_band"].isin(["High Risk", "Extreme Turbulence"]).mean()
        ),
        "score_bands_scaled": _scale_bands(target_secs),
    }
    return summary_rows, cs

def _make_demo_data(instruments: List[str], cfg: VolatilityConfig) -> Dict[str, pd.DataFrame]:
    rng = np.random.default_rng(cfg.random_seed)
    _, _, start_ts, end_ts = _resolve_date_range(cfg)
    res_sec = _resolution_seconds(cfg.resolution)
    periods = max(220, min(5000, int((end_ts - start_ts) / res_sec)))
    idx = pd.date_range(
        start=pd.Timestamp(start_ts, unit="s", tz="UTC"), periods=periods, freq=f"{res_sec}s"
    )
    data: Dict[str, pd.DataFrame] = {}
    for i, inst in enumerate(instruments):
        vol_level = 0.0015 + (i / max(1, len(instruments) - 1)) * 0.006
        shock = np.zeros(periods)
        sigma = np.full(periods, vol_level)
        for t in range(1, periods):
            sigma[t] = 0.88 * sigma[t - 1] + 0.12 * vol_level + 0.06 * abs(shock[t - 1])
            sigma[t] = min(sigma[t], vol_level * 4.0)
            shock[t] = rng.normal(0, sigma[t])
            if rng.random() < 0.002 + i * 0.00005:
                shock[t] += rng.normal(0, vol_level * 6.0)
        price = 1.0 + i * 0.35
        close = price * np.exp(np.cumsum(shock))
        open_ = np.r_[close[0], close[:-1]]
        intra = np.maximum(np.abs(shock) * rng.uniform(1.5, 3.5, periods), sigma * 0.8)
        high = np.maximum(open_, close) * (1 + intra)
        low = np.minimum(open_, close) * np.maximum(0.01, 1 - intra)
        volume = rng.lognormal(mean=10.5 - i * 0.015, sigma=0.7, size=periods)
        data[inst] = pd.DataFrame({
            "timestamp": idx, "open": open_, "high": high,
            "low": low, "close": close, "volume": volume,
        })
    return data

def run_lightweight_analysis(
    cfg: LWConfig,
    instruments: Optional[List[str]] = None,
    include_regex: Optional[str] = None,
    exclude_regex: Optional[str] = None,
    max_instruments: Optional[int] = None,
    demo: bool = False,
) -> Dict[str, Any]:
    start_label, end_label, _, _ = _resolve_date_range(cfg.base)
    score_weights = _score_weights(cfg)
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    if demo:
        selected = normalize_instruments_lw(instruments, cfg.base.margin_currency) or [
            f"B-DEMO{i:02d}_USDT" for i in range(1, 25)
        ]
        if max_instruments:
            selected = selected[:max_instruments]
        raw_data = _make_demo_data(selected, cfg.base)
    else:
        if instruments:
            selected = normalize_instruments_lw(instruments, cfg.base.margin_currency) or []
        else:
            selected = fetch_active_instruments(cfg.base, session=session)
        if include_regex:
            rx = re.compile(include_regex)
            selected = [x for x in selected if rx.search(x)]
        if exclude_regex:
            rx = re.compile(exclude_regex)
            selected = [x for x in selected if not rx.search(x)]
        if max_instruments:
            selected = selected[:max_instruments]
        raw_data = {}

    print(f"[LW] Analysing {len(selected)} instruments")
    instrument_results: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def process_instrument(inst):
        try:
            if demo:
                df = raw_data[inst]
                daily_volume_df = None
            else:
                df = fetch_candles_data(inst, cfg.base, session=session)
                daily_volume_df = None  # Optimized: bypass daily volume API fetch to halve request counts (rolling computed from df)
            info = analyse_instrument_lw(inst, df, cfg, daily_volume_df=daily_volume_df)
            return inst, info, None
        except Exception as exc:
            return inst, {"instrument": inst, "error": str(exc)}, str(exc)

    max_workers = min(16, len(selected)) if selected else 0
    if max_workers > 0:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_instrument, inst): inst for inst in selected}
            for future in as_completed(futures):
                inst = futures[future]
                try:
                    inst, info, err = future.result()
                    instrument_results[inst] = info
                    if err:
                        errors[inst] = err
                        print(f"[WARN] {inst}: {err}")
                    elif "error" in info:
                        errors[inst] = str(info["error"])
                except Exception as e:
                    errors[inst] = str(e)
                    instrument_results[inst] = {"instrument": inst, "error": str(e)}
                    print(f"[WARN] {inst}: {e}")

    summary_rows, cross_section = score_cross_section(instrument_results, cfg)

    if cfg.min_volume_usd > 0:
        summary_rows = [
            r for r in summary_rows
            if _finite(r.get("volume_24h"), 0) >= cfg.min_volume_usd * 1e6
        ]

    report = {
        "meta": {
            "created_at_utc": str(_now_utc()),
            "start_date": start_label,
            "end_date": end_label or str(_now_utc().date()),
            "resolution": cfg.base.resolution,
            "scale_horizon": cfg.scale_horizon,
            "target_seconds": _parse_horizon_to_seconds(cfg.scale_horizon),
            "margin_currency": cfg.base.margin_currency,
            "framework": "volatility_lightweight",
            "weights": score_weights,
            "score_weights": score_weights,
            "score_formula": "score = ((alpha * sigma_yz + beta * sigma_ewma) / (alpha + beta)) / sqrt(max(1 - gamma * jump_fraction, 0.05))",
            "score_scale": "percentage_fraction",
            "bvol_window_hours": cfg.bvol_window_hours,
            "bvol_window_label": _window_label(cfg.bvol_window_hours),
            "daily_volume_days": cfg.daily_volume_days,
            "volume_basis": "latest_1D_candle_volume",
            "dollar_volume_basis": "volume_24h * last_price",
            "timeframe_options": TIMEFRAME_OPTIONS,
            "horizon_options": HORIZON_OPTIONS,
            "api_path": "/api",
            "selected_instruments": selected,
            "n_requested": len(selected),
            "n_analyzed": len(summary_rows),
            "n_errors": len(errors),
            "source": "demo_synthetic" if demo else "coindcx_public_api",
            "utm_metadata": {
                "utm_source": "volatility_terminal",
                "utm_medium": "report_payload",
                "utm_campaign": "volatility_discovery_v1",
                "utm_content": "dashboard_meta_initialization",
                "version": "1.0.0"
            }
        },
        "cross_section": cross_section,
        "summary": summary_rows,
        "instruments": instrument_results,
        "errors": errors,
    }
    return report

def print_console_summary(report: Dict[str, Any]) -> None:
    rows = report.get("summary", [])
    if not rows:
        print("[WARN] No instruments analysed.")
        return
    cs = report.get("cross_section", {})
    print(f"\n{'='*60}")
    print("VOLATILITY LIGHTWEIGHT SCORE SUMMARY")
    print(f"{'='*60}")
    print(f"  Instruments: {cs.get('n_instruments', 0)}")
    print(f"  Median Score: {cs.get('median_score', 0):.4f}")
    print(f"  High Risk %: {cs.get('high_risk_pct', 0):.1%}")
    bands = cs.get("band_counts", {})
    for label in ["Quiet", "Stable", "Active", "High Risk", "Extreme Turbulence"]:
        print(f"    {label:20s}: {bands.get(label, 0):4d}")
    print(f"\nTOP 12 BY VOLATILITY SCORE")
    df = pd.DataFrame(rows[:12])
    cols = ["instrument", "score_band", "volatility_score", "sigma_base", "sigma_yz", "sigma_ewma",
            "jump_fraction", "bvol_window", "volume_24h"]
    available = [c for c in cols if c in df.columns]
    print(df[available].to_string(index=False, float_format="%.4f"))

def build_lw_dashboard(report: Dict[str, Any], output_path: str) -> None:
    html = vol_signal_template(report)
    Path(output_path).write_text(html, encoding="utf-8")
    print(f"[LW] Dashboard saved to {output_path}")


def _dashboard_payload(report: Dict[str, Any]) -> Dict[str, Any]:
    payload = _serialise(report)
    instruments = payload.get("instruments", {})
    if isinstance(instruments, dict):
        for info in instruments.values():
            if isinstance(info, dict):
                info.pop("timeseries", None)
    return payload


def vol_signal_template(report: Dict[str, Any]) -> str:
    from frontend.render import render_volatility_document

    data_json = json.dumps(_dashboard_payload(report), separators=(",", ":")).replace("</", "<\\/")
    return render_volatility_document(data_json)


def save_lw_outputs(report: Dict[str, Any], cfg: LWConfig) -> None:
    serialised = _serialise(report)
    Path(cfg.output_json).write_text(json.dumps(serialised, indent=2), encoding="utf-8")
    print(f"[LW] JSON saved to {cfg.output_json}")
    if report.get("summary"):
        pd.DataFrame(report["summary"]).to_csv(cfg.output_csv, index=False)
        print(f"[LW] CSV saved to {cfg.output_csv}")
    build_lw_dashboard(report, cfg.output_html)

def build_lw_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Lightweight Volatility Score Framework")
    p.add_argument("--scale-horizon", default="24H", help="Target horizon for absolute vol scaling (e.g. 24H, 7d, 1M)")
    p.add_argument("--start-date", help="UTC start YYYY-MM-DD (default: 90d ago)")
    p.add_argument("--end-date", help="UTC end YYYY-MM-DD (default: now)")
    p.add_argument("--resolution", "--timeframe", dest="resolution", default="240", help="Candle timeframe/resolution (default: 240 = 4H)")
    p.add_argument("--margin-currency", default="USDT")
    p.add_argument("--instrument", action="append", help="Instrument(s), repeatable")
    p.add_argument("--instruments", action="append", help="Comma/newline/space separated instrument list")
    p.add_argument("--instrument-file", help="Text file, one instrument per line")
    p.add_argument("--include-regex", help="Only matching instruments")
    p.add_argument("--exclude-regex", help="Exclude matching instruments")
    p.add_argument("--max-instruments", type=int)
    p.add_argument("--min-bars", type=int, default=80)
    p.add_argument("--min-volume", type=float, default=0.0, help="Min latest daily candle volume in millions")
    p.add_argument("--alpha", type=float, default=DEFAULT_SCORE_WEIGHTS["alpha"], help="YZ score weight")
    p.add_argument("--beta", type=float, default=DEFAULT_SCORE_WEIGHTS["beta"], help="EWMA score weight")
    p.add_argument("--gamma", type=float, default=DEFAULT_SCORE_WEIGHTS["gamma"], help="Jump score BN-S multiplier")
    p.add_argument("--bvol-window-hours", type=float, default=24.0, help="BVOL horizon in hours")
    p.add_argument("--cache-dir", default="volatility_cache")
    p.add_argument("--refresh-cache", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--sleep", type=float, default=0.25)
    p.add_argument("--output-json", default="volatility_lw_report.json")
    p.add_argument("--output-csv", default="volatility_lw_summary.csv")
    p.add_argument("--output-html", default="volatility_lw_dashboard.html")
    p.add_argument("--demo", action="store_true", help="Run with synthetic demo data")
    return p


def parse_instruments_lw(values, file_path, margin_currency: str = "USDT"):
    instruments = []
    if values:
        for v in values:
            instruments.extend([x.strip() for x in re.split(r"[\s,;]+", v) if x.strip()])
    if file_path:
        for line in Path(file_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                instruments.append(line)
    return normalize_instruments_lw(instruments, margin_currency)


def main() -> Dict[str, Any]:
    parser = build_lw_parser()
    args = parser.parse_args()
    base_cfg = VolatilityConfig(
        margin_currency=args.margin_currency,
        resolution=args.resolution,
        start_date=args.start_date,
        end_date=args.end_date,
        min_bars=args.min_bars,
        cache_dir=args.cache_dir,
        refresh_cache=args.refresh_cache,
        cache_raw=not args.no_cache,
        request_sleep=args.sleep,
    )
    cfg = LWConfig(
        base=base_cfg,
        scale_horizon=args.scale_horizon,
        alpha=args.alpha,
        beta=args.beta,
        gamma=args.gamma,
        bvol_window_hours=args.bvol_window_hours,
        min_volume_usd=args.min_volume,
        output_json=args.output_json,
        output_csv=args.output_csv,
        output_html=args.output_html,
    )
    instruments = parse_instruments_lw(
        (args.instrument or []) + (args.instruments or []),
        args.instrument_file,
        args.margin_currency,
    )
    report = run_lightweight_analysis(
        cfg, instruments=instruments,
        include_regex=args.include_regex, exclude_regex=args.exclude_regex,
        max_instruments=args.max_instruments, demo=args.demo,
    )
    save_lw_outputs(report, cfg)
    print_console_summary(report)
    return report

if __name__ == "__main__":
    main()
