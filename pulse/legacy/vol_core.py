import argparse
import json
import math
import re
import time
import warnings
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

try:
    from scipy import stats

    _SCIPY = True
except Exception:
    stats = None
    _SCIPY = False

try:
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch

    _SM = True
except Exception:
    _SM = False


ACTIVE_INSTRUMENTS_URL = (
    "https://api.coindcx.com/exchange/v1/derivatives/futures/data/"
    "active_instruments"
)
CANDLE_URL = "https://public.coindcx.com/market_data/candlesticks"


VOL_CLASS_ORDER = ["extreme_high", "high", "med", "low", "extreme_low"]
VOL_CLASS_LABELS = {
    "extreme_high": "Extreme High",
    "high": "High",
    "med": "Med",
    "low": "Low",
    "extreme_low": "Extreme Low",
}

TOP_K = 5
SMALL_UNIVERSE_MAX = 25
MEDIUM_UNIVERSE_MAX = 250
CORRELATION_UNIVERSE_CAP = 120
TABLE_PAGE_SIZE = 50


@dataclass(frozen=True)
class VolatilityConfig:
    margin_currency: str = "USDT"
    pcode: str = "f"
    resolution: str = "5"
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    min_bars: int = 80
    vol_windows: Tuple[int, ...] = (5, 10, 21, 63, 126)
    default_vol_window: int = 21
    rolling_z_window: int = 63
    trend_window: int = 63
    regime_window: int = 21

    spike_zscore_thresh: float = 2.0
    jump_zscore_thresh: float = 3.0
    extreme_sigma_levels: Tuple[float, ...] = (2.0, 3.0, 4.0)
    acf_lags: Tuple[int, ...] = (1, 2, 5, 10, 20)
    ljungbox_lags: Tuple[int, ...] = (5, 10, 20)
    vr_lags: Tuple[int, ...] = (2, 5, 10, 20)
    tail_pct: float = 0.05
    hill_k_frac: float = 0.10
    gpd_threshold_q: float = 0.95
    extremal_block_size: int = 50
    hurst_min_window: int = 10
    hurst_max_window: int = 500
    entropy_bins: int = 16
    entropy_embed_dims: Tuple[int, ...] = (3, 4, 5, 6)
    sample_entropy_m: int = 2
    sample_entropy_r_mult: float = 0.2
    sample_entropy_limit: int = 256
    slow_fit_max_bars: int = 2500

    request_timeout: int = 25
    request_sleep: float = 0.25
    retries: int = 3
    retry_backoff: float = 1.6
    api_limit: int = 30000

    cache_dir: str = "volatility_cache"
    refresh_cache: bool = False
    cache_raw: bool = True

    output_json: str = "volatility_report.json"
    output_csv: str = "volatility_summary.csv"
    output_html: str = "volatility_dashboard.html"

    max_chart_points: int = 360
    max_dashboard_instruments: int = 500
    random_seed: int = 42


def _safe(a: Any, b: Any) -> float:
    try:
        if b is None or not np.isfinite(float(b)) or float(b) == 0:
            return np.nan
        return float(a) / float(b)
    except Exception:
        return np.nan


def _finite(v: Any, default: float = np.nan) -> float:
    try:
        out = float(v)
        return out if np.isfinite(out) else default
    except Exception:
        return default


def _norm_col(c: str) -> str:
    c = str(c).strip().lower()
    c = c.replace("%", "pct").replace(".", "_").replace(" ", "_")
    return re.sub(r"__+", "_", c)


def _instrument_slug(instrument: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", instrument)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date_utc(date_str: str, end_of_day: bool = False) -> datetime:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.replace(tzinfo=timezone.utc)


def _resolve_date_range(cfg: VolatilityConfig) -> Tuple[str, Optional[str], int, int]:
    if cfg.start_date:
        start_dt = _parse_date_utc(cfg.start_date)
    else:
        start_dt = _now_utc() - timedelta(days=90)
    if cfg.end_date:
        end_dt = _parse_date_utc(cfg.end_date, end_of_day=True)
        end_label: Optional[str] = cfg.end_date
    else:
        end_dt = _now_utc()
        end_label = None
    if end_dt <= start_dt:
        raise ValueError("end_date must be after start_date")
    return (
        start_dt.strftime("%Y-%m-%d"),
        end_label,
        int(start_dt.timestamp()),
        int(end_dt.timestamp()),
    )


def _resolution_seconds(resolution: str) -> int:
    token = str(resolution).strip().lower()
    if token.endswith("m"):
        return int(token[:-1]) * 60
    if token.endswith("h"):
        return int(token[:-1]) * 3600
    if token.endswith("d"):
        return int(token[:-1]) * 86400
    return int(float(token)) * 60


def _annualization_from_index(idx: pd.DatetimeIndex) -> Tuple[str, int]:
    if len(idx) < 2:
        return "unknown", 365
    diffs = np.diff(idx.asi8) / 1e9
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if len(diffs) == 0:
        return "unknown", 365
    med = float(np.median(diffs))
    minutes = med / 60.0
    bars_per_year = int(round(365.0 * 24.0 * 3600.0 / med))
    if minutes < 1.5:
        return "1m", bars_per_year
    if minutes < 7.5:
        return "5m", bars_per_year
    if minutes < 20:
        return "15m", bars_per_year
    if minutes < 45:
        return "30m", bars_per_year
    if minutes < 90:
        return "1h", bars_per_year
    if minutes < 300:
        return "4h", bars_per_year
    if minutes < 1200:
        return "12h", bars_per_year
    if minutes < 2400:
        return "1d", bars_per_year
    if minutes < 10080:
        return "1w", max(1, bars_per_year)
    return "custom", max(1, bars_per_year)


def _pct_rank(values: Sequence[float], x: float) -> float:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0 or not np.isfinite(x):
        return np.nan
    return float((np.sum(arr <= x) - 0.5) / len(arr))


def _robust_z(values: Sequence[float], x: float) -> float:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 3 or not np.isfinite(x):
        return np.nan
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    if mad <= 1e-12:
        std = float(np.std(arr, ddof=1))
        return float((x - med) / std) if std > 1e-12 else 0.0
    return float((x - med) / (1.4826 * mad))


def _class_from_percentile(p: float) -> str:
    if not np.isfinite(p):
        return "med"
    if p >= 0.90:
        return "extreme_high"
    if p >= 0.70:
        return "high"
    if p <= 0.10:
        return "extreme_low"
    if p <= 0.30:
        return "low"
    return "med"


def _class_rank(cls: str) -> int:
    return {
        "extreme_low": 0,
        "low": 1,
        "med": 2,
        "high": 3,
        "extreme_high": 4,
    }.get(cls, 2)


def _confidence_label(score: float) -> str:
    if score >= 0.76:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def _clean_numeric_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)


def _downsample_xy(
    dates: Sequence[Any], values: Sequence[Any], max_points: int
) -> Dict[str, List[Any]]:
    pairs = []
    for d, v in zip(dates, values):
        fv = _finite(v)
        if np.isfinite(fv):
            pairs.append((str(pd.Timestamp(d)), fv))
    if len(pairs) <= max_points:
        return {"dates": [p[0] for p in pairs], "values": [p[1] for p in pairs]}
    idx = np.linspace(0, len(pairs) - 1, max_points).round().astype(int)
    sampled = [pairs[i] for i in idx]
    return {"dates": [p[0] for p in sampled], "values": [p[1] for p in sampled]}


def _serialise(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _serialise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialise(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _serialise(obj.tolist())
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return f if np.isfinite(f) else None
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    if isinstance(obj, (pd.Timestamp, datetime)):
        return str(obj)
    if isinstance(obj, pd.Series):
        return _serialise(obj.tolist())
    if pd.isna(obj) if not isinstance(obj, (str, bytes, list, tuple, dict)) else False:
        return None
    return obj


def fetch_active_instruments(cfg: VolatilityConfig, session: Optional[requests.Session] = None) -> List[str]:
    sess = session or requests.Session()
    params = [("margin_currency_short_name[]", cfg.margin_currency)]
    last_error: Optional[Exception] = None
    for attempt in range(cfg.retries):
        try:
            response = sess.get(
                ACTIVE_INSTRUMENTS_URL,
                params=params,
                timeout=cfg.request_timeout,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError(f"Unexpected active instruments payload: {type(data).__name__}")
            return sorted(str(x) for x in data if isinstance(x, str) and x.strip())
        except Exception as exc:
            last_error = exc
            time.sleep(cfg.retry_backoff ** attempt)
    raise RuntimeError(f"Could not fetch active instruments: {last_error}")


def fetch_coindcx_candles(
    pair: str,
    cfg: VolatilityConfig,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    sess = session or requests.Session()
    start_label, end_label, start_ts, end_ts = _resolve_date_range(cfg)
    resolution_seconds = _resolution_seconds(cfg.resolution)
    max_duration = cfg.api_limit * resolution_seconds
    cache_dir = Path(cfg.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_name = (
        f"{_instrument_slug(pair)}_{start_label}_{end_label or 'now'}_"
        f"{cfg.resolution}_{cfg.pcode}.csv"
    )
    cache_path = cache_dir / cache_name

    if cfg.cache_raw and cache_path.exists() and not cfg.refresh_cache:
        return pd.read_csv(cache_path)

    all_candles: List[Dict[str, Any]] = []
    current_end_ts = end_ts
    while current_end_ts > start_ts:
        chunk_start_ts = max(start_ts, current_end_ts - max_duration)
        params = {
            "pair": pair,
            "from": chunk_start_ts,
            "to": current_end_ts,
            "resolution": cfg.resolution,
            "pcode": cfg.pcode,
        }
        payload: Optional[Any] = None
        last_error: Optional[Exception] = None
        for attempt in range(cfg.retries):
            try:
                response = sess.get(CANDLE_URL, params=params, timeout=cfg.request_timeout)
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(cfg.retry_backoff ** attempt)
        if payload is None:
            raise RuntimeError(f"{pair}: candlestick request failed: {last_error}")

        if isinstance(payload, dict):
            candles = payload.get("data", [])
        elif isinstance(payload, list):
            candles = payload
        else:
            candles = []
        if not candles:
            break

        all_candles.extend(candles)
        current_end_ts = chunk_start_ts - 1
        time.sleep(cfg.request_sleep)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles)
    if "time" in df.columns:
        df = df.drop_duplicates(subset=["time"])
        df["timestamp"] = pd.to_datetime(df["time"], unit="ms", utc=True, errors="coerce")
        df = df.drop(columns=["time"])
    elif "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.drop_duplicates(subset=["timestamp"])
    else:
        raise ValueError(f"{pair}: candle payload has no time/timestamp column")

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    if cfg.cache_raw:
        df.to_csv(cache_path, index=False)
    return df


def load_ohlcv(src: Any) -> Tuple[pd.DataFrame, str, int]:
    if isinstance(src, pd.DataFrame):
        df = src.copy()
    else:
        path = Path(str(src))
        if path.exists():
            df = pd.read_csv(path)
        else:
            raise FileNotFoundError(str(src))

    df.columns = [_norm_col(c) for c in df.columns]
    ts_candidates = ["datetime", "date", "time", "timestamp", "ts", "dt"]
    if isinstance(df.index, pd.DatetimeIndex) and not any(c in df.columns for c in ts_candidates):
        df = df.reset_index()

    ts_col = next((c for c in ts_candidates if c in df.columns), df.columns[0])
    df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
    df = df.dropna(subset=[ts_col]).sort_values(ts_col).set_index(ts_col)
    df.index.name = "datetime"

    aliases = {
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c", "adj_close", "adj close"],
        "volume": ["volume", "vol", "v"],
    }
    rename_map: Dict[str, str] = {}
    for canon, candidates in aliases.items():
        for c in candidates:
            if c in df.columns:
                rename_map[c] = canon
                break
    df = df.rename(columns=rename_map)

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"Missing required OHLC column: {col}")
        df[col] = _clean_numeric_series(df[col])
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = _clean_numeric_series(df["volume"]).fillna(0.0)

    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]
    freq_label, ppy = _annualization_from_index(df.index)

    o = df["open"]
    h = df["high"]
    lo = df["low"]
    c = df["close"]
    prev_c = c.shift(1)

    df["ret_cc"] = np.log(c / prev_c)
    df["ret_simple"] = c.pct_change()
    df["ret_abs"] = df["ret_cc"].abs()
    df["ret_sq"] = df["ret_cc"] ** 2
    df["ret_intraday"] = np.log(c / o)
    df["ret_overnight"] = np.log(o / prev_c)
    df["hl_range"] = h - lo
    df["hl_range_pct"] = df["hl_range"] / prev_c
    df["body_pct"] = (c - o).abs() / prev_c
    df["true_range"] = np.maximum(h - lo, np.maximum((h - prev_c).abs(), (lo - prev_c).abs()))
    df["true_range_pct"] = df["true_range"] / prev_c
    df["atr_14"] = df["true_range"].ewm(span=14, min_periods=14).mean()
    df["atr_pct"] = df["atr_14"] / c
    rng = df["hl_range"].replace(0, np.nan)
    df["bar_efficiency"] = (c - o).abs() / rng
    df["dollar_volume"] = c * df["volume"]

    vol_ma20 = df["volume"].rolling(20, min_periods=1).mean()
    df["rel_volume"] = df["volume"] / vol_ma20.replace(0, np.nan)
    df = df.iloc[1:].copy()
    return df, freq_label, ppy


def audit_data_quality(df: pd.DataFrame) -> Dict[str, Any]:
    n = int(len(df))
    res: Dict[str, Any] = {
        "n_bars": n,
        "start": str(df.index.min()) if n else None,
        "end": str(df.index.max()) if n else None,
    }
    if n == 0:
        return {**res, "quality_score": 0.0}

    for col in ["open", "high", "low", "close", "volume"]:
        res[f"missing_{col}"] = int(df[col].isna().sum()) if col in df.columns else n

    ohlc_ok = (
        (df["high"] >= df["low"])
        & (df["high"] >= df["open"])
        & (df["high"] >= df["close"])
        & (df["low"] <= df["open"])
        & (df["low"] <= df["close"])
    )
    n_bad = int((~ohlc_ok).sum())
    res["ohlc_inconsistent_bars"] = n_bad
    res["ohlc_consistency_pct"] = float(1.0 - n_bad / max(n, 1))
    res["duplicate_timestamps"] = int(df.index.duplicated().sum())
    res["zero_volume_bars"] = int((df["volume"] == 0).sum())
    res["zero_volume_pct"] = float((df["volume"] == 0).mean())
    res["flat_bars"] = int((df["high"] == df["low"]).sum())
    res["flat_bars_pct"] = float((df["high"] == df["low"]).mean())

    if n > 2:
        diffs = np.diff(df.index.asi8) / 1e9
        med_diff = float(np.median(diffs)) if len(diffs) else np.nan
        res["median_bar_interval_sec"] = med_diff
        res["n_gaps_gt_3x_median"] = int(np.sum(diffs > 3 * med_diff)) if med_diff > 0 else 0
    else:
        res["n_gaps_gt_3x_median"] = 0
        res["median_bar_interval_sec"] = np.nan

    r = df["ret_cc"].replace([np.inf, -np.inf], np.nan).dropna()
    sigma = float(r.std(ddof=1)) if len(r) > 10 else 0.0
    res["outlier_bars_10sigma"] = int((r.abs() > 10 * sigma).sum()) if sigma > 0 else 0

    penalties = (
        0.20 * min(1.0, n_bad / max(n, 1) * 10)
        + 0.12 * min(1.0, res["zero_volume_pct"])
        + 0.12 * min(1.0, res["flat_bars_pct"])
        + 0.10 * min(1.0, res["duplicate_timestamps"] / max(n, 1) * 20)
        + 0.16 * min(1.0, res["n_gaps_gt_3x_median"] / max(n, 1) * 25)
        + 0.10 * min(1.0, res["outlier_bars_10sigma"] / max(n, 1) * 100)
        + 0.20 * (1.0 if n < 80 else 0.0)
    )
    res["quality_score"] = round(max(0.0, 1.0 - penalties), 4)
    return res


def _parkinson_vol(h: pd.Series, l: pd.Series, window: int) -> pd.Series:
    hl = np.log(h / l)
    return np.sqrt((hl**2).rolling(window).mean() / (4 * np.log(2)))


def _garman_klass_vol(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series, window: int) -> pd.Series:
    u = np.log(h / o)
    d = np.log(l / o)
    cc = np.log(c / o)
    gk = 0.5 * (u - d) ** 2 - (2 * np.log(2) - 1) * cc**2
    return np.sqrt(gk.clip(lower=0).rolling(window).mean())


def _rogers_satchell_vol(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series, window: int) -> pd.Series:
    rs = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)
    return np.sqrt(rs.clip(lower=0).rolling(window).mean())


def _yang_zhang_vol(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series, window: int) -> pd.Series:
    if window <= 1:
        return pd.Series(index=c.index, dtype=float)
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    log_oc = np.log(o / c.shift(1))
    log_co = np.log(c / o)
    var_overnight = log_oc.rolling(window).var()
    var_close = log_co.rolling(window).var()
    rs = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)
    var_rs = rs.rolling(window).mean()
    yz_var = var_overnight + k * var_close + (1 - k) * var_rs
    return np.sqrt(yz_var.clip(lower=0))


def _acf(arr: Sequence[float], lag: int) -> float:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    if lag <= 0 or len(x) <= lag + 2:
        return np.nan
    a = x[:-lag]
    b = x[lag:]
    if np.std(a) <= 1e-12 or np.std(b) <= 1e-12:
        return np.nan
    return float(np.corrcoef(a, b)[0, 1])


def _linregress(y: Sequence[float]) -> Dict[str, float]:
    arr = np.asarray(y, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 5:
        return {"slope": np.nan, "p": np.nan, "r2": np.nan}
    x = np.arange(len(arr), dtype=float)
    if _SCIPY:
        lr = stats.linregress(x, arr)
        return {
            "slope": float(lr.slope),
            "p": float(lr.pvalue),
            "r2": float(lr.rvalue**2),
        }
    x0 = x - x.mean()
    y0 = arr - arr.mean()
    slope = _safe(np.sum(x0 * y0), np.sum(x0**2))
    return {"slope": slope, "p": np.nan, "r2": np.nan}


def _std_ci_chi_square(arr: Sequence[float], ppy: int, ci: float = 0.95) -> Tuple[float, float]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return np.nan, np.nan
    s = float(np.std(x, ddof=1))
    if not _SCIPY or s <= 0:
        return np.nan, np.nan
    alpha = 1.0 - ci
    df = n - 1
    lower = math.sqrt(df * s * s / stats.chi2.ppf(1 - alpha / 2, df)) * math.sqrt(ppy)
    upper = math.sqrt(df * s * s / stats.chi2.ppf(alpha / 2, df)) * math.sqrt(ppy)
    return float(lower), float(upper)


def _mannwhitney_p(a: Sequence[float], b: Sequence[float]) -> float:
    if not _SCIPY:
        return np.nan
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    aa = aa[np.isfinite(aa)]
    bb = bb[np.isfinite(bb)]
    if len(aa) < 5 or len(bb) < 5:
        return np.nan
    try:
        return float(stats.mannwhitneyu(aa, bb, alternative="two-sided").pvalue)
    except Exception:
        return np.nan


def _safe_ljungbox(arr: Sequence[float], lags: Iterable[int]) -> Dict[str, Dict[str, float]]:
    if not _SM:
        return {}
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    out: Dict[str, Dict[str, float]] = {}
    for lag in lags:
        if len(x) <= lag + 5:
            continue
        try:
            lb = acorr_ljungbox(x, lags=[lag], return_df=True)
            out[f"lag_{lag}"] = {
                "stat": float(lb["lb_stat"].iloc[0]),
                "p": float(lb["lb_pvalue"].iloc[0]),
            }
        except Exception:
            pass
    return out


def _safe_arch_lm(arr: Sequence[float]) -> Dict[str, float]:
    if not _SM:
        return {}
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 40:
        return {}
    try:
        resid = x - np.mean(x)
        lm_stat, lm_p, _, _ = het_arch(resid, nlags=min(10, len(x) // 5))
        return {"stat": float(lm_stat), "p": float(lm_p)}
    except Exception:
        return {}


def _moment_skew_kurt(arr: Sequence[float]) -> Tuple[float, float]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 4:
        return np.nan, np.nan
    if _SCIPY:
        return float(stats.skew(x)), float(stats.kurtosis(x))
    mu = float(np.mean(x))
    sig = float(np.std(x, ddof=1))
    if sig <= 1e-14:
        return np.nan, np.nan
    z = (x - mu) / sig
    return float(np.mean(z**3)), float(np.mean(z**4) - 3.0)


def _distribution_diagnostics(arr: Sequence[float], ppy: int) -> Dict[str, Any]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return {"error": "insufficient data"}

    sk, ku = _moment_skew_kurt(x)
    mu = float(np.mean(x))
    sig = float(np.std(x, ddof=1))
    qs = [0.01, 0.025, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.975, 0.99]
    pct_keys = {f"p{str(q * 100).replace('.', '_').rstrip('0').rstrip('_')}": float(np.quantile(x, q)) for q in qs}
    res: Dict[str, Any] = {
        "n": n,
        "mean": mu,
        "std": sig,
        "ann_mean": mu * ppy,
        "ann_vol": sig * math.sqrt(ppy),
        "skew": sk,
        "kurt_fisher": ku,
        "kurt_pearson": ku + 3.0 if np.isfinite(ku) else np.nan,
        "percentiles": pct_keys,
    }

    tests: Dict[str, Any] = {}
    if _SCIPY and sig > 0:
        try:
            jb = stats.jarque_bera(x)
            tests["jarque_bera"] = {
                "stat": float(getattr(jb, "statistic", jb[0])),
                "p": float(getattr(jb, "pvalue", jb[1])),
            }
        except Exception:
            pass
        if n >= 20:
            try:
                nt = stats.normaltest(x)
                tests["dagostino_k2"] = {
                    "stat": float(getattr(nt, "statistic", nt[0])),
                    "p": float(getattr(nt, "pvalue", nt[1])),
                }
            except Exception:
                pass
            try:
                ks = stats.kstest(x, "norm", args=(mu, sig))
                tests["ks_norm"] = {
                    "stat": float(getattr(ks, "statistic", ks[0])),
                    "p": float(getattr(ks, "pvalue", ks[1])),
                }
            except Exception:
                pass
        if n >= 20:
            try:
                ad = stats.anderson(x, dist="norm")
                idx = int(np.argmin(np.abs(np.asarray(ad.significance_level) - 5.0)))
                tests["anderson_darling"] = {
                    "stat": float(ad.statistic),
                    "crit_5pct": float(ad.critical_values[idx]),
                    "reject_5pct": bool(ad.statistic > ad.critical_values[idx]),
                }
            except Exception:
                pass
    res["normality_tests"] = tests

    agg_kurt: Dict[str, float] = {}
    sx = pd.Series(x)
    for k in [1, 2, 5, 10, 21]:
        if k * 5 > n:
            break
        agg = sx.rolling(k).sum().dropna().values
        if len(agg) > 10:
            _, aku = _moment_skew_kurt(agg)
            agg_kurt[str(k)] = float(aku + 3.0) if np.isfinite(aku) else np.nan
    res["agg_gaussianity_kurt"] = agg_kurt

    gains = x[x > 0]
    losses = x[x < 0]
    sk_g, _ = _moment_skew_kurt(gains)
    sk_l, _ = _moment_skew_kurt(losses)
    res["gain_loss"] = {
        "n_gains": int(len(gains)),
        "n_losses": int(len(losses)),
        "mean_gain": float(np.mean(gains)) if len(gains) else 0.0,
        "mean_loss": float(np.mean(losses)) if len(losses) else 0.0,
        "std_gains": float(np.std(gains, ddof=1)) if len(gains) > 1 else 0.0,
        "std_losses": float(np.std(losses, ddof=1)) if len(losses) > 1 else 0.0,
        "skew_gains": sk_g,
        "skew_losses": sk_l,
    }
    return res


def _runs_test(arr: Sequence[float]) -> Dict[str, float]:
    if not _SCIPY:
        return {}
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    signs = np.sign(x)
    signs = signs[signs != 0]
    n = len(signs)
    if n < 12:
        return {}
    n_pos = int((signs > 0).sum())
    n_neg = int(n - n_pos)
    if n_pos == 0 or n_neg == 0:
        return {}
    runs = 1 + int(np.sum(signs[1:] != signs[:-1]))
    exp_runs = 1.0 + 2.0 * n_pos * n_neg / n
    var_runs = (2.0 * n_pos * n_neg * (2.0 * n_pos * n_neg - n)) / (n**2 * (n - 1))
    if var_runs <= 0:
        return {}
    z = (runs - exp_runs) / math.sqrt(var_runs)
    return {
        "runs": int(runs),
        "expected": float(exp_runs),
        "z": float(z),
        "p": float(2 * (1 - stats.norm.cdf(abs(z)))),
    }


def _hurst_rs(arr: Sequence[float], min_w: int = 10, max_w: int = 500) -> Dict[str, float]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    max_w = min(max_w, n // 4)
    if n < 40 or max_w < min_w:
        return {"H": np.nan, "r2": np.nan, "p": np.nan, "points": 0}
    windows: List[int] = []
    w = max(2, min_w)
    while w <= max_w:
        windows.append(w)
        w = int(w * 1.4) + 1
    log_n: List[float] = []
    log_rs: List[float] = []
    for win in sorted(set(windows)):
        chunks = n // win
        if chunks < 2:
            continue
        vals = []
        for i in range(chunks):
            chunk = x[i * win : (i + 1) * win]
            sd = float(np.std(chunk, ddof=1))
            if sd <= 1e-14:
                continue
            z = np.cumsum(chunk - np.mean(chunk))
            vals.append((float(np.max(z)) - float(np.min(z))) / sd)
        if vals:
            log_n.append(math.log(win))
            log_rs.append(math.log(float(np.mean(vals))))
    if len(log_n) < 3:
        return {"H": np.nan, "r2": np.nan, "p": np.nan, "points": len(log_n)}
    if _SCIPY:
        lr = stats.linregress(log_n, log_rs)
        return {"H": float(lr.slope), "r2": float(lr.rvalue**2), "p": float(lr.pvalue), "points": len(log_n)}
    slope, intercept = np.polyfit(np.asarray(log_n), np.asarray(log_rs), 1)
    pred = slope * np.asarray(log_n) + intercept
    ss_res = float(np.sum((np.asarray(log_rs) - pred) ** 2))
    ss_tot = float(np.sum((np.asarray(log_rs) - np.mean(log_rs)) ** 2))
    return {"H": float(slope), "r2": 1.0 - _safe(ss_res, ss_tot), "p": np.nan, "points": len(log_n)}


def _variance_ratio_test(arr: Sequence[float], q: int) -> Dict[str, float]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if q <= 1 or n < q * 3:
        return {"VR": np.nan, "z": np.nan, "p": np.nan}
    mu = float(np.mean(x))
    sig1 = float(np.sum((x - mu) ** 2) / max(n - 1, 1))
    rq = np.asarray([np.sum(x[i : i + q]) for i in range(n - q + 1)], dtype=float)
    sigq = float(np.sum((rq - q * mu) ** 2) / max(n - 1, 1))
    vr = _safe(sigq, q * sig1)
    if not np.isfinite(vr):
        return {"VR": np.nan, "z": np.nan, "p": np.nan}
    delta = np.zeros(q - 1, dtype=float)
    den = float(np.sum((x - mu) ** 2) ** 2)
    if den <= 1e-20:
        return {"VR": float(vr), "z": np.nan, "p": np.nan}
    for k in range(1, q):
        delta[k - 1] = np.sum((x[k:] - mu) ** 2 * (x[:-k] - mu) ** 2) / den
    theta = 0.0
    for k in range(1, q):
        theta += (2.0 * (q - k) / q) ** 2 * delta[k - 1]
    z = (vr - 1.0) / math.sqrt(theta) if theta > 0 else np.nan
    p = float(2 * (1 - stats.norm.cdf(abs(z)))) if _SCIPY and np.isfinite(z) else np.nan
    return {"VR": float(vr), "z": float(z), "p": p}


def _tail_risk_diagnostics(arr: Sequence[float], ppy: int, cfg: VolatilityConfig) -> Dict[str, Any]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 30:
        return {"error": "insufficient data"}
    alpha = cfg.tail_pct
    var_h = float(np.quantile(x, alpha))
    tail = x[x <= var_h]
    cvar_h = float(np.mean(tail)) if len(tail) else var_h
    mu = float(np.mean(x))
    sig = float(np.std(x, ddof=1))
    sk, ku = _moment_skew_kurt(x)
    res: Dict[str, Any] = {
        "alpha": alpha,
        "hist_VaR": var_h,
        "hist_CVaR": cvar_h,
        "loss_VaR_abs": abs(var_h),
        "loss_CVaR_abs": abs(cvar_h),
        "max_loss": float(np.min(x)),
        "max_gain": float(np.max(x)),
        "max_loss_max_gain_ratio": _safe(abs(np.min(x)), np.max(x)),
    }
    if _SCIPY and sig > 0:
        z = float(stats.norm.ppf(alpha))
        res["param_VaR_norm"] = float(mu + sig * z)
        cf_z = (
            z
            + (z**2 - 1.0) * sk / 6.0
            + (z**3 - 3.0 * z) * ku / 24.0
            - (2.0 * z**3 - 5.0 * z) * sk**2 / 36.0
        )
        res["cornish_fisher_VaR"] = float(mu + sig * cf_z)
        if n <= cfg.slow_fit_max_bars:
            try:
                t_df, t_loc, t_scale = stats.t.fit(x)
                res["param_VaR_t"] = float(stats.t.ppf(alpha, t_df, t_loc, t_scale))
                res["t_fit"] = {"df": float(t_df), "loc": float(t_loc), "scale": float(t_scale)}
            except Exception:
                pass
        elif np.isfinite(ku) and ku > 1e-6:
            df_t = float(max(4.1, min(200.0, 6.0 / ku + 4.0)))
            scale_t = sig / math.sqrt(df_t / (df_t - 2.0))
            res["param_VaR_t_mom"] = float(mu + stats.t.ppf(alpha, df_t) * scale_t)
            res["t_fit_moments"] = {"df": df_t, "scale": scale_t}

    abs_x = np.sort(np.abs(x))[::-1]
    k = max(5, int(cfg.hill_k_frac * n))
    k = min(k, n - 1)
    if k > 1 and abs_x[k] > 0:
        log_ratios = np.log(abs_x[:k] / abs_x[k])
        avg = float(np.mean(log_ratios))
        res["hill_tail_index"] = float(1.0 / avg) if avg > 0 else np.nan
        res["hill_k"] = int(k)

    threshold = float(np.quantile(x, 1.0 - cfg.gpd_threshold_q))
    exceedances = threshold - x[x < threshold]
    if len(exceedances) > 10:
        res["gpd_left_tail"] = {
            "n_exceedances": int(len(exceedances)),
            "threshold": threshold,
        }
        if _SCIPY and n <= cfg.slow_fit_max_bars * 2:
            try:
                shape, _, scale = stats.genpareto.fit(exceedances, floc=0)
                res["gpd_left_tail"].update({"shape_xi": float(shape), "scale_beta": float(scale)})
                p_exceed = len(exceedances) / n
                if scale > 0 and p_exceed > 0:
                    if abs(shape) > 1e-12:
                        gpd_var = threshold - (scale / shape) * ((alpha / p_exceed) ** (-shape) - 1.0)
                    else:
                        gpd_var = threshold + scale * math.log(alpha / p_exceed)
                    res["gpd_VaR"] = float(gpd_var)
                    res["gpd_CVaR"] = (
                        float((gpd_var + scale - shape * threshold) / (1.0 - shape))
                        if shape < 1.0
                        else np.nan
                    )
            except Exception:
                pass

    q95 = float(np.quantile(x, 0.95))
    q05 = float(abs(np.quantile(x, 0.05)))
    res["tail_ratio_95_05"] = _safe(q95, q05)
    res["tail_loss_asymmetry"] = _safe(abs(np.quantile(x, 0.01)), np.quantile(x, 0.99))
    return res


def _extreme_event_diagnostics(arr: Sequence[float], ppy: int, cfg: VolatilityConfig) -> Dict[str, Any]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 60:
        return {"error": "insufficient data"}
    abs_x = np.abs(x)
    rv = float(np.sum(x**2))
    bv = float((math.pi / 2.0) * np.sum(abs_x[1:] * abs_x[:-1]))
    jump_var = max(rv - bv, 0.0)
    sig = float(np.std(x, ddof=1))
    threshold = cfg.jump_zscore_thresh * sig
    jump_mask = abs_x > threshold if threshold > 0 else np.zeros(n, dtype=bool)
    jumps = x[jump_mask]
    res: Dict[str, Any] = {
        "bns_jump_test": {
            "realized_variance": rv,
            "bipower_variation": bv,
            "jump_variation": jump_var,
            "jump_pct_of_rv": float(jump_var / rv) if rv > 0 else 0.0,
            "continuous_vol_ann": float(math.sqrt(max(bv, 0.0) * ppy / n)),
            "jump_vol_ann": float(math.sqrt(jump_var * ppy / n)),
        },
        "jump_statistics": {
            "n_jumps": int(len(jumps)),
            "jump_frequency": float(len(jumps) / n),
            "threshold_used": threshold,
            "mean_jump_size": float(np.mean(np.abs(jumps))) if len(jumps) else 0.0,
            "max_jump": float(np.max(np.abs(jumps))) if len(jumps) else 0.0,
            "pct_negative_jumps": float(np.mean(jumps < 0)) if len(jumps) else np.nan,
            "mean_positive_jump": float(np.mean(jumps[jumps > 0])) if np.any(jumps > 0) else np.nan,
            "mean_negative_jump": float(np.mean(jumps[jumps < 0])) if np.any(jumps < 0) else np.nan,
        },
    }

    jump_idx = np.where(jump_mask)[0]
    if len(jump_idx) >= 3:
        inter = np.diff(jump_idx)
        if len(inter) > 2 and np.mean(inter) > 0:
            cv = float(np.std(inter, ddof=1) / np.mean(inter))
            res["jump_clustering"] = {
                "mean_inter_arrival": float(np.mean(inter)),
                "std_inter_arrival": float(np.std(inter, ddof=1)),
                "cv_inter_arrival": cv,
                "clustering_detected": bool(cv > 1.0),
            }

    block_size = max(5, int(cfg.extremal_block_size))
    n_blocks = n // block_size
    if n_blocks >= 5:
        q_left = float(np.quantile(x, cfg.tail_pct))
        block_hits = [
            int(np.any(x[i * block_size : (i + 1) * block_size] < q_left))
            for i in range(n_blocks)
        ]
        theta = float(np.mean(block_hits))
        res["extremal_index"] = {
            "theta": theta,
            "n_blocks": int(n_blocks),
            "block_size": int(block_size),
            "effective_return_period_mult": float(1.0 / theta) if theta > 0 else np.nan,
            "interpretation": (
                "independent extremes"
                if theta > 0.9
                else "moderate clustering"
                if theta > 0.5
                else "strong extreme clustering"
            ),
        }

    rv_roll = pd.Series(x).rolling(cfg.default_vol_window).std().dropna().values
    if len(rv_roll) > 30:
        aligned = x[-len(rv_roll) :]
        med = float(np.median(rv_roll))
        low = aligned[rv_roll <= med]
        high = aligned[rv_roll > med]
        if len(low) > 10 and len(high) > 10:
            low_p1 = float(np.quantile(low, 0.01))
            high_p1 = float(np.quantile(high, 0.01))
            res["conditional_extremes"] = {
                "low_vol_p1": low_p1,
                "low_vol_p5": float(np.quantile(low, 0.05)),
                "high_vol_p1": high_p1,
                "high_vol_p5": float(np.quantile(high, 0.05)),
                "tail_amplification": _safe(high_p1, low_p1),
            }

    equity = np.exp(np.cumsum(x))
    hwm = np.maximum.accumulate(equity)
    dd = (equity - hwm) / hwm
    dd_neg = dd[dd < 0]
    if len(dd_neg) > 10:
        dar95 = float(np.quantile(dd_neg, 0.05))
        dar99 = float(np.quantile(dd_neg, 0.01))
        res["drawdown_at_risk"] = {
            "DaR_95": dar95,
            "DaR_99": dar99,
            "CDaR_95": float(np.mean(dd_neg[dd_neg <= dar95])),
            "mean_drawdown": float(np.mean(dd_neg)),
            "pct_time_in_dd": float(len(dd_neg) / n),
        }
    return res


def _sample_entropy(arr: Sequence[float], m: int, r_tol: float, limit: int) -> Dict[str, float]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) > limit:
        idx = np.linspace(0, len(x) - 1, limit).round().astype(int)
        x = x[idx]
    n = len(x)
    if n < m + 4 or r_tol <= 0:
        return {"SampEn": np.nan, "m": int(m), "r": float(r_tol), "n_used": int(n)}

    def count_matches(width: int) -> int:
        try:
            emb = np.lib.stride_tricks.sliding_window_view(x, width)
        except Exception:
            emb = np.asarray([x[i : i + width] for i in range(n - width + 1)])
        count = 0
        for i in range(len(emb) - 1):
            dist = np.max(np.abs(emb[i + 1 :] - emb[i]), axis=1)
            count += int(np.sum(dist <= r_tol))
        return count

    b = count_matches(m)
    a = count_matches(m + 1)
    se = float(-math.log(a / b)) if a > 0 and b > 0 else np.nan
    return {"SampEn": se, "m": int(m), "r": float(r_tol), "n_used": int(n)}


def _permutation_entropy(arr: Sequence[float], dim: int, tau: int = 1) -> float:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if dim < 2 or n < (dim - 1) * tau + 2:
        return np.nan
    patterns: Dict[Tuple[int, ...], int] = {}
    stop = n - (dim - 1) * tau
    for i in range(stop):
        window = x[i : i + dim * tau : tau]
        if len(window) == dim:
            key = tuple(np.argsort(window, kind="mergesort"))
            patterns[key] = patterns.get(key, 0) + 1
    total = sum(patterns.values())
    if total <= 0:
        return np.nan
    probs = np.asarray(list(patterns.values()), dtype=float) / total
    h = float(-np.sum(probs * np.log2(probs + 1e-14)))
    h_max = math.log2(math.factorial(dim))
    return float(h / h_max) if h_max > 0 else np.nan


def _lempel_ziv_binary_complexity(binary_seq: Sequence[int]) -> float:
    bits = "".join("1" if int(x) else "0" for x in binary_seq)
    n = len(bits)
    if n < 2:
        return np.nan
    seen = set()
    token = ""
    count = 0
    for ch in bits:
        token += ch
        if token not in seen:
            seen.add(token)
            count += 1
            token = ""
    if token:
        count += 1
    baseline = n / max(math.log2(n), 1.0)
    return float(count / baseline) if baseline > 0 else np.nan


def _entropy_diagnostics(arr: Sequence[float], cfg: VolatilityConfig) -> Dict[str, Any]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 60:
        return {"error": "insufficient data"}
    sig = float(np.std(x, ddof=1))
    counts, _ = np.histogram(x, bins=max(4, cfg.entropy_bins))
    probs = counts.astype(float) / max(float(counts.sum()), 1.0)
    probs = probs[probs > 1e-14]
    h = float(-np.sum(probs * np.log(probs)))
    h_norm = float(h / math.log(max(2, cfg.entropy_bins)))
    res: Dict[str, Any] = {
        "shannon_entropy": {
            "H_empirical": h,
            "H_normalized": h_norm,
            "H_gaussian": float(0.5 * math.log(2 * math.pi * math.e * sig**2)) if sig > 0 else np.nan,
        }
    }
    r_tol = cfg.sample_entropy_r_mult * sig
    res["sample_entropy"] = _sample_entropy(x, cfg.sample_entropy_m, r_tol, cfg.sample_entropy_limit)

    pe: Dict[str, float] = {}
    for dim in cfg.entropy_embed_dims:
        pe[str(dim)] = _permutation_entropy(x, dim)
    vals = [v for v in pe.values() if np.isfinite(v)]
    res["permutation_entropy"] = pe
    res["permutation_entropy_summary"] = {
        "mean_normalized_PE": float(np.mean(vals)) if vals else np.nan,
        "complexity_assessment": (
            "highly predictable"
            if vals and float(np.mean(vals)) < 0.7
            else "moderate complexity"
            if vals and float(np.mean(vals)) < 0.9
            else "nearly random"
            if vals
            else "unknown"
        ),
    }

    abs_x = np.abs(x)
    vol_state = (abs_x > np.median(abs_x)).astype(int)
    res["lempel_ziv"] = {
        "LZC_volatility": _lempel_ziv_binary_complexity(vol_state),
        "state_threshold": float(np.median(abs_x)),
    }

    window = min(max(cfg.default_vol_window * 4, 80), n // 3)
    if window >= 60:
        step = max(1, window // 4)
        ent = []
        for end in range(window, n + 1, step):
            val = _permutation_entropy(x[end - window : end], 4)
            if np.isfinite(val):
                ent.append(val)
        if len(ent) > 5:
            ent_arr = np.asarray(ent, dtype=float)
            d_ent = np.diff(ent_arr)
            res["entropy_production"] = {
                "mean_rate": float(np.mean(d_ent)),
                "std_rate": float(np.std(d_ent, ddof=1)) if len(d_ent) > 1 else np.nan,
                "current_trend": "increasing complexity" if np.mean(d_ent[-3:]) > 0 else "decreasing complexity",
                "current_PE": float(ent_arr[-1]),
            }
    return res


def _memory_diagnostics(arr: Sequence[float], cfg: VolatilityConfig) -> Dict[str, Any]:
    x = np.asarray(arr, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 100:
        return {"error": "insufficient data"}
    abs_x = np.abs(x)
    sq_x = x**2
    h_abs = _hurst_rs(abs_x, cfg.hurst_min_window, min(cfg.hurst_max_window, n // 4))
    h_sq = _hurst_rs(sq_x, cfg.hurst_min_window, min(cfg.hurst_max_window, n // 4))
    out: Dict[str, Any] = {
        "volatility_memory": {
            "H_abs_returns": h_abs.get("H", np.nan),
            "H_sq_returns": h_sq.get("H", np.nan),
            "long_memory_confirmed": bool(
                _finite(h_abs.get("H"), 0.5) > 0.6 or _finite(h_sq.get("H"), 0.5) > 0.6
            ),
            "hurst_abs_detail": h_abs,
            "hurst_sq_detail": h_sq,
        }
    }
    max_lag = min(20, n // 5)
    mi_decay: Dict[str, float] = {}
    bins = max(4, min(cfg.entropy_bins, 20))
    for lag in range(1, max_lag + 1):
        a = abs_x[:-lag]
        b = abs_x[lag:]
        try:
            hist_xy, _, _ = np.histogram2d(a, b, bins=bins)
            total = hist_xy.sum()
            if total <= 0:
                continue
            pxy = hist_xy / total
            px = pxy.sum(axis=1)
            py = pxy.sum(axis=0)
            mi = 0.0
            nz = np.argwhere(pxy > 1e-14)
            for i, j in nz:
                if px[i] > 1e-14 and py[j] > 1e-14:
                    mi += pxy[i, j] * math.log(pxy[i, j] / (px[i] * py[j]))
            mi_decay[str(lag)] = float(mi)
        except Exception:
            pass
    out["mutual_information_decay_abs_returns"] = mi_decay
    if len(mi_decay) >= 4:
        lags = np.asarray([float(k) for k in mi_decay.keys()])
        vals = np.asarray(list(mi_decay.values()), dtype=float)
        mask = vals > 0
        if mask.sum() >= 4:
            if _SCIPY:
                lr = stats.linregress(lags[mask], np.log(vals[mask] + 1e-14))
                slope = float(lr.slope)
                r2 = float(lr.rvalue**2)
            else:
                slope, intercept = np.polyfit(lags[mask], np.log(vals[mask] + 1e-14), 1)
                pred = slope * lags[mask] + intercept
                r2 = 1.0 - _safe(np.sum((np.log(vals[mask] + 1e-14) - pred) ** 2), np.sum((np.log(vals[mask] + 1e-14) - np.mean(np.log(vals[mask] + 1e-14))) ** 2))
            out["mi_decay_rate"] = {
                "exp_decay_rate": float(slope),
                "half_life_lags": float(-math.log(2) / slope) if slope < -1e-6 else np.nan,
                "r2": r2,
            }
    return out


def _rolling_diagnostics(r: pd.Series, ppy: int, cfg: VolatilityConfig) -> Dict[str, Any]:
    s = r.replace([np.inf, -np.inf], np.nan).dropna()
    n = len(s)
    w = min(cfg.rolling_z_window, max(cfg.default_vol_window, n // 3))
    if n < w + 20:
        return {"error": "insufficient data"}
    roll_std = s.rolling(w).std()
    roll_vol = (roll_std * math.sqrt(ppy)).dropna()
    roll_skew = s.rolling(w).skew().dropna()
    roll_kurt = s.rolling(w).kurt().dropna()

    def cv(vals: pd.Series) -> float:
        vals = vals.replace([np.inf, -np.inf], np.nan).dropna()
        return _safe(vals.std(ddof=1), abs(vals.mean())) if len(vals) > 3 else np.nan

    x = s.values
    sig = float(np.std(x, ddof=1))
    out: Dict[str, Any] = {
        "window": int(w),
        "stability_scores": {
            "vol_cv": cv(roll_vol),
            "skew_cv": cv(roll_skew),
            "kurt_cv": cv(roll_kurt),
        },
        "variance_ratio": {f"q_{q}": _variance_ratio_test(x, q) for q in cfg.vr_lags},
        "runs_test": _runs_test(x),
        "ar1_coeff": _acf(x, 1),
    }
    if sig > 0:
        centered = x - float(np.mean(x))
        cs = np.cumsum(centered)
        stat = float((np.max(cs) - np.min(cs)) / (sig * math.sqrt(n)))
        break_idx = int(np.argmax(np.abs(cs)))
        out["cusum"] = {
            "stat": stat,
            "break_detected_5pct_approx": bool(stat > 1.36),
            "break_date": str(s.index[break_idx]) if 0 <= break_idx < len(s.index) else None,
        }
    return out


def _episode_durations(labels: np.ndarray, state: int) -> List[int]:
    durations: List[int] = []
    cur = 0
    for label in labels:
        if int(label) == state:
            cur += 1
        elif cur:
            durations.append(cur)
            cur = 0
    if cur:
        durations.append(cur)
    return durations


def analyse_instrument(
    instrument: str,
    raw_df: pd.DataFrame,
    cfg: VolatilityConfig,
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
    n = int(len(df))
    r = df["ret_cc"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < max(20, min(cfg.min_bars, 40)):
        result["error"] = f"insufficient bars after cleaning: {len(r)}"
        result["quality"] = quality
        return result

    ann = math.sqrt(ppy)
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    rolling_vol: Dict[int, pd.Series] = {}
    term_structure: Dict[str, Dict[str, float]] = {}
    for window in cfg.vol_windows:
        s = (r.rolling(window).std() * ann).dropna()
        rolling_vol[window] = s
        if len(s):
            s_chg = s.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
            term_structure[str(window)] = {
                "current_ann": float(s.iloc[-1]),
                "mean_ann": float(s.mean()),
                "median_ann": float(s.median()),
                "p10_ann": float(s.quantile(0.10)),
                "p90_ann": float(s.quantile(0.90)),
                "p95_ann": float(s.quantile(0.95)),
                "history_percentile": _pct_rank(s.values, float(s.iloc[-1])),
                "vol_of_vol": float(s_chg.std(ddof=1)) if len(s_chg) > 2 else np.nan,
            }

    default_window = cfg.default_vol_window
    rv_default = rolling_vol.get(default_window, pd.Series(dtype=float))
    if rv_default.empty:
        rv_default = (r.rolling(default_window).std() * ann).dropna()
        rolling_vol[default_window] = rv_default

    estimators: Dict[str, Dict[str, float]] = {}
    estimator_sources = {
        "close_close": r.rolling(default_window).std(),
        "parkinson": _parkinson_vol(h, l, default_window),
        "garman_klass": _garman_klass_vol(o, h, l, c, default_window),
        "rogers_satchell": _rogers_satchell_vol(o, h, l, c, default_window),
        "yang_zhang": _yang_zhang_vol(o, h, l, c, default_window),
    }
    for name, series in estimator_sources.items():
        s = (series * ann).replace([np.inf, -np.inf], np.nan).dropna()
        if len(s):
            estimators[name] = {
                "current_ann": float(s.iloc[-1]),
                "mean_ann": float(s.mean()),
                "median_ann": float(s.median()),
                "p90_ann": float(s.quantile(0.90)),
            }

    current_ann = float(rv_default.iloc[-1]) if len(rv_default) else np.nan
    mean_ann = float(rv_default.mean()) if len(rv_default) else np.nan
    median_ann = float(rv_default.median()) if len(rv_default) else np.nan
    hist_pct = _pct_rank(rv_default.values, current_ann) if len(rv_default) else np.nan

    z_window = min(cfg.rolling_z_window, max(10, len(rv_default) // 2))
    rv_mean = rv_default.rolling(z_window, min_periods=max(10, z_window // 3)).mean()
    rv_std = rv_default.rolling(z_window, min_periods=max(10, z_window // 3)).std()
    rv_z = ((rv_default - rv_mean) / rv_std.replace(0, np.nan)).dropna()
    current_z = float(rv_z.iloc[-1]) if len(rv_z) else np.nan

    rv_pct_chg = rv_default.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    vol_of_vol = float(rv_pct_chg.std(ddof=1)) if len(rv_pct_chg) > 2 else np.nan
    vol_of_vol_abs = float(rv_default.diff().dropna().std(ddof=1)) if len(rv_default) > 3 else np.nan
    vol_cv = _safe(rv_default.std(ddof=1), rv_default.mean()) if len(rv_default) > 3 else np.nan

    trend_series = rv_default.tail(cfg.trend_window)
    trend = _linregress(trend_series.values)
    trend["slope_pct_per_bar"] = _safe(trend.get("slope"), np.nanmean(trend_series.values))

    spikes: Dict[str, Any] = {}
    spike_dates: List[str] = []
    if len(rv_z):
        spike_mask = rv_z > cfg.spike_zscore_thresh
        spike_dates = [str(pd.Timestamp(x)) for x in rv_z.index[spike_mask]]
        n_spikes = int(spike_mask.sum())
        r_at_spike = r.reindex(rv_z.index[spike_mask]).dropna()
        ret_sigma = float(r.std(ddof=1))
        jump_driven = int((r_at_spike.abs() > cfg.jump_zscore_thresh * ret_sigma).sum()) if ret_sigma > 0 else 0
        spikes = {
            "n_spikes": n_spikes,
            "spike_frequency": float(n_spikes / len(rv_z)) if len(rv_z) else 0.0,
            "mean_spike_zscore": float(rv_z[spike_mask].mean()) if n_spikes else np.nan,
            "max_spike_zscore": float(rv_z[spike_mask].max()) if n_spikes else np.nan,
            "latest_spike": spike_dates[-1] if spike_dates else None,
            "jump_driven_spikes": jump_driven,
            "cluster_driven_spikes": int(n_spikes - jump_driven),
            "jump_driven_pct": float(jump_driven / n_spikes) if n_spikes else 0.0,
        }
        if n_spikes >= 3 and len(rv_default) > 10:
            loc_map = {ts: j for j, ts in enumerate(rv_default.index)}
            rv_vals = rv_default.values
            rv_median = float(np.nanmedian(rv_vals))
            recovery_bars = []
            for ts in rv_z.index[spike_mask]:
                si = loc_map.get(ts)
                if si is None:
                    continue
                for j in range(si + 1, min(si + cfg.rolling_z_window * 2, len(rv_vals))):
                    if rv_vals[j] < rv_median:
                        recovery_bars.append(j - si)
                        break
            if recovery_bars:
                spikes["spike_recovery"] = {
                    "mean_recovery_bars": float(np.mean(recovery_bars)),
                    "median_recovery_bars": float(np.median(recovery_bars)),
                    "max_recovery_bars": int(max(recovery_bars)),
                    "pct_recovered": float(len(recovery_bars) / n_spikes),
                }
    else:
        spikes = {
            "n_spikes": 0,
            "spike_frequency": 0.0,
            "mean_spike_zscore": np.nan,
            "max_spike_zscore": np.nan,
            "latest_spike": None,
            "jump_driven_spikes": 0,
            "cluster_driven_spikes": 0,
            "jump_driven_pct": 0.0,
        }

    regimes: Dict[str, Any] = {}
    if len(rv_default) >= 30:
        median_vol = float(rv_default.median())
        labels = np.where(rv_default.values > median_vol, 1, 0)
        tm = np.zeros((2, 2), dtype=float)
        for i in range(len(labels) - 1):
            tm[labels[i], labels[i + 1]] += 1
        row_sum = tm.sum(axis=1, keepdims=True)
        row_sum[row_sum == 0] = 1.0
        tmn = tm / row_sum
        regimes["transition_matrix"] = {
            "low_to_low": float(tmn[0, 0]),
            "low_to_high": float(tmn[0, 1]),
            "high_to_low": float(tmn[1, 0]),
            "high_to_high": float(tmn[1, 1]),
        }
        regimes["current_binary_regime"] = "high_vol" if labels[-1] == 1 else "low_vol"
        regime_stats: Dict[str, Dict[str, float]] = {}
        for lbl, name in [(0, "low_vol"), (1, "high_vol")]:
            idx = rv_default.index[labels == lbl]
            sub_r = r.reindex(idx).dropna()
            if len(sub_r) >= 5:
                sub_vals = sub_r.values
                sk, ku = _moment_skew_kurt(sub_vals)
                sig_sub = float(np.std(sub_vals, ddof=1))
                regime_stats[name] = {
                    "n_bars": int(len(sub_r)),
                    "pct_time": float(len(sub_r) / max(len(rv_default), 1)),
                    "mean_ret": float(np.mean(sub_vals)),
                    "ann_ret": float(np.mean(sub_vals) * ppy),
                    "ann_vol": float(sig_sub * ann) if sig_sub > 0 else np.nan,
                    "sharpe": float(np.mean(sub_vals) / sig_sub * ann) if sig_sub > 0 else np.nan,
                    "skew": sk,
                    "kurt": ku,
                    "win_rate": float(np.mean(sub_vals > 0)),
                }
        regimes["regime_stats"] = regime_stats
        for state, name in [(0, "low_vol"), (1, "high_vol")]:
            durations = _episode_durations(labels, state)
            if durations:
                darr = np.asarray(durations, dtype=float)
                regimes[f"{name}_duration"] = {
                    "n_episodes": int(len(durations)),
                    "mean": float(darr.mean()),
                    "median": float(np.median(darr)),
                    "p90": float(np.quantile(darr, 0.90)),
                    "max": int(darr.max()),
                }
                if len(darr) >= 5:
                    geo_p = _safe(1.0, np.mean(darr))
                    regimes[f"regime_duration_fit_{name}"] = {
                        "n_episodes": int(len(darr)),
                        "geometric_p": geo_p,
                        "expected_duration": float(1.0 / geo_p) if np.isfinite(geo_p) and geo_p > 0 else np.nan,
                        "duration_std": float(np.std(darr, ddof=1)),
                        "duration_skew": float(stats.skew(darr)) if _SCIPY and len(darr) > 3 else np.nan,
                        "p10_duration": float(np.quantile(darr, 0.10)),
                        "p90_duration": float(np.quantile(darr, 0.90)),
                    }
    regimes["current_history_class"] = _class_from_percentile(hist_pct)

    r_vals = r.values
    abs_r = np.abs(r_vals)
    neg_mask = r_vals < 0
    pos_mask = r_vals >= 0
    asymmetry: Dict[str, Any] = {}
    if len(r_vals) > 30 and neg_mask.sum() > 5 and pos_mask.sum() > 5:
        next_abs = np.abs(r_vals[1:])
        prev_neg = neg_mask[:-1]
        prev_pos = pos_mask[:-1]
        after_neg = next_abs[prev_neg]
        after_pos = next_abs[prev_pos]
        asymmetry["vol_after_negative_return"] = float(np.mean(after_neg)) if len(after_neg) else np.nan
        asymmetry["vol_after_positive_return"] = float(np.mean(after_pos)) if len(after_pos) else np.nan
        asymmetry["leverage_ratio"] = _safe(
            asymmetry["vol_after_negative_return"],
            asymmetry["vol_after_positive_return"],
        )
        asymmetry["leverage_mannwhitney_p"] = _mannwhitney_p(after_neg, after_pos)

        neg_vol = float(np.std(r_vals[neg_mask], ddof=1)) if neg_mask.sum() > 2 else np.nan
        pos_vol = float(np.std(r_vals[pos_mask], ddof=1)) if pos_mask.sum() > 2 else np.nan
        asymmetry["downside_vol_ann"] = neg_vol * ann if np.isfinite(neg_vol) else np.nan
        asymmetry["upside_vol_ann"] = pos_vol * ann if np.isfinite(pos_vol) else np.nan
        asymmetry["downside_upside_ratio"] = _safe(neg_vol, pos_vol)

    memory: Dict[str, Any] = {
        "acf_returns": {f"lag_{lag}": _acf(r_vals, lag) for lag in cfg.acf_lags},
        "acf_abs_returns": {f"lag_{lag}": _acf(abs_r, lag) for lag in cfg.acf_lags},
        "acf_squared_returns": {f"lag_{lag}": _acf(r_vals**2, lag) for lag in cfg.acf_lags},
        "ljungbox_returns": _safe_ljungbox(r_vals, cfg.ljungbox_lags),
        "ljungbox_abs_returns": _safe_ljungbox(abs_r, cfg.ljungbox_lags),
        "ljungbox_squared_returns": _safe_ljungbox(r_vals**2, cfg.ljungbox_lags),
        "runs_test": _runs_test(r_vals),
    }
    if len(rv_default) > 10:
        rv_ar1 = _acf(rv_default.values, 1)
        memory["rolling_vol_ar1"] = rv_ar1
        if np.isfinite(rv_ar1) and 0 < rv_ar1 < 1:
            memory["rolling_vol_half_life_bars"] = float(-math.log(2) / math.log(rv_ar1))
        else:
            memory["rolling_vol_half_life_bars"] = np.nan

    abs_return_quantiles = {
        "p50": float(np.quantile(abs_r, 0.50)),
        "p75": float(np.quantile(abs_r, 0.75)),
        "p90": float(np.quantile(abs_r, 0.90)),
        "p95": float(np.quantile(abs_r, 0.95)),
        "p99": float(np.quantile(abs_r, 0.99)),
    }
    ret_sigma = float(np.std(r_vals, ddof=1))
    extreme_rates = {}
    for level in cfg.extreme_sigma_levels:
        key = f"abs_ret_gt_{str(level).replace('.', '_')}sigma_rate"
        extreme_rates[key] = float(np.mean(abs_r > level * ret_sigma)) if ret_sigma > 0 else np.nan
    top_decile = abs_r[abs_r >= np.quantile(abs_r, 0.90)]

    tail_and_extremes = {
        "abs_return_quantiles": abs_return_quantiles,
        "extreme_rates": extreme_rates,
        "mean_top_decile_abs_return": float(np.mean(top_decile)) if len(top_decile) else np.nan,
        "max_abs_return": float(np.max(abs_r)) if len(abs_r) else np.nan,
    }
    distribution = _distribution_diagnostics(r_vals, ppy)
    tail_risk = _tail_risk_diagnostics(r_vals, ppy, cfg)
    extreme_events = _extreme_event_diagnostics(r_vals, ppy, cfg)
    entropy_info = _entropy_diagnostics(r_vals, cfg)
    volatility_memory = _memory_diagnostics(r_vals, cfg)
    rolling_diagnostics = _rolling_diagnostics(r, ppy, cfg)
    tail_and_extremes.update(
        {
            "tail_risk": tail_risk,
            "extreme_events": extreme_events,
        }
    )

    range_features = {
        "mean_hl_range_pct": float(df["hl_range_pct"].replace([np.inf, -np.inf], np.nan).mean()),
        "median_hl_range_pct": float(df["hl_range_pct"].replace([np.inf, -np.inf], np.nan).median()),
        "p95_hl_range_pct": float(df["hl_range_pct"].replace([np.inf, -np.inf], np.nan).quantile(0.95)),
        "current_atr_pct": float(df["atr_pct"].dropna().iloc[-1]) if df["atr_pct"].dropna().size else np.nan,
        "mean_atr_pct": float(df["atr_pct"].replace([np.inf, -np.inf], np.nan).mean()),
        "range_vol_ratio": _safe(
            df["hl_range_pct"].replace([np.inf, -np.inf], np.nan).mean(),
            np.mean(abs_r),
        ),
        "mean_bar_efficiency": float(df["bar_efficiency"].replace([np.inf, -np.inf], np.nan).mean()),
    }

    fold_stats: Dict[str, Any] = {}
    if len(r_vals) >= 100:
        folds = np.array_split(r_vals, 5)
        fold_vols = [float(np.std(f, ddof=1) * ann) for f in folds if len(f) > 5 and np.std(f, ddof=1) > 0]
        if len(fold_vols) >= 3:
            fold_stats = {
                "vol_ann_by_fold": fold_vols,
                "mean_vol_ann": float(np.mean(fold_vols)),
                "vol_cv": _safe(np.std(fold_vols, ddof=1), abs(np.mean(fold_vols))),
                "min_vol_ann": float(np.min(fold_vols)),
                "max_vol_ann": float(np.max(fold_vols)),
                "max_to_min_ratio": _safe(np.max(fold_vols), np.min(fold_vols)),
            }

    term_short = term_structure.get(str(min(cfg.vol_windows)), {}).get("current_ann", np.nan)
    term_long = term_structure.get(str(max(cfg.vol_windows)), {}).get("current_ann", np.nan)
    term_slope = _safe(term_long - term_short, term_short)

    arch_lm = _safe_arch_lm(r_vals)
    vol_ci = _std_ci_chi_square(r_vals, ppy, ci=0.95)
    abs_acf_lag10 = memory["acf_abs_returns"].get("lag_10", np.nan)
    acf_sig_level = 1.96 / math.sqrt(len(r_vals)) if len(r_vals) > 0 else np.nan
    normality_tests = distribution.get("normality_tests", {})
    bns = extreme_events.get("bns_jump_test", {})
    jump_stats = extreme_events.get("jump_statistics", {})
    jump_clustering = extreme_events.get("jump_clustering", {})
    conditional_extremes = extreme_events.get("conditional_extremes", {})
    extremal_index = extreme_events.get("extremal_index", {})
    drawdown_at_risk = extreme_events.get("drawdown_at_risk", {})
    entropy_summary = entropy_info.get("permutation_entropy_summary", {})
    sample_entropy = entropy_info.get("sample_entropy", {})
    entropy_production = entropy_info.get("entropy_production", {})
    lzc = entropy_info.get("lempel_ziv", {})
    vol_memory_summary = volatility_memory.get("volatility_memory", {})
    rolling_stability = rolling_diagnostics.get("stability_scores", {})
    rolling_cusum = rolling_diagnostics.get("cusum", {})
    variance_ratio = rolling_diagnostics.get("variance_ratio", {})
    vr5 = variance_ratio.get("q_5", {})
    runs = memory.get("runs_test", {})
    gpd_left = tail_risk.get("gpd_left_tail", {})
    spike_recovery = spikes.get("spike_recovery", {})

    inference = {
        "ann_vol_ci_95_normal_theory": {"lo": vol_ci[0], "hi": vol_ci[1]},
        "current_vol_zscore": current_z,
        "current_vol_history_percentile": hist_pct,
        "arch_lm": arch_lm,
        "normality_tests": normality_tests,
        "runs_test": runs,
        "abs_return_acf_lag10": abs_acf_lag10,
        "abs_return_acf_lag10_significant": bool(abs(abs_acf_lag10) > acf_sig_level)
        if np.isfinite(abs_acf_lag10) and np.isfinite(acf_sig_level)
        else None,
        "vol_trend_p": trend.get("p"),
        "leverage_mannwhitney_p": asymmetry.get("leverage_mannwhitney_p", np.nan),
        "fold_vol_stability": fold_stats,
        "rolling_diagnostics": rolling_diagnostics,
    }

    summary = {
        "instrument": instrument,
        "n_bars": n,
        "freq": freq_label,
        "ppy": ppy,
        "start": str(df.index.min()),
        "end": str(df.index.max()),
        "price_last": float(df["close"].iloc[-1]),
        "quality_score": quality.get("quality_score", np.nan),
        "current_ann_vol": current_ann,
        "mean_ann_vol": mean_ann,
        "median_ann_vol": median_ann,
        "vol_history_percentile": hist_pct,
        "vol_z": current_z,
        "vol_of_vol": vol_of_vol,
        "vol_of_vol_abs": vol_of_vol_abs,
        "vol_cv": vol_cv,
        "return_skew": distribution.get("skew", np.nan),
        "return_kurt_fisher": distribution.get("kurt_fisher", np.nan),
        "jarque_bera_p": normality_tests.get("jarque_bera", {}).get("p", np.nan),
        "dagostino_k2_p": normality_tests.get("dagostino_k2", {}).get("p", np.nan),
        "spike_frequency": spikes.get("spike_frequency", np.nan),
        "n_spikes": spikes.get("n_spikes", 0),
        "max_spike_zscore": spikes.get("max_spike_zscore", np.nan),
        "latest_spike": spikes.get("latest_spike"),
        "spike_mean_recovery_bars": spike_recovery.get("mean_recovery_bars", np.nan),
        "spike_pct_recovered": spike_recovery.get("pct_recovered", np.nan),
        "acf_abs_lag10": abs_acf_lag10,
        "acf_sq_lag10": memory["acf_squared_returns"].get("lag_10", np.nan),
        "runs_p": runs.get("p", np.nan),
        "rolling_vol_ar1": memory.get("rolling_vol_ar1", np.nan),
        "rolling_vol_half_life_bars": memory.get("rolling_vol_half_life_bars", np.nan),
        "rolling_vol_stability_cv": rolling_stability.get("vol_cv", np.nan),
        "rolling_skew_cv": rolling_stability.get("skew_cv", np.nan),
        "rolling_kurt_cv": rolling_stability.get("kurt_cv", np.nan),
        "variance_ratio_5": vr5.get("VR", np.nan),
        "variance_ratio_5_p": vr5.get("p", np.nan),
        "cusum_stat": rolling_cusum.get("stat", np.nan),
        "cusum_break_detected": rolling_cusum.get("break_detected_5pct_approx", None),
        "downside_upside_ratio": asymmetry.get("downside_upside_ratio", np.nan),
        "leverage_ratio": asymmetry.get("leverage_ratio", np.nan),
        "range_vol_ratio": range_features.get("range_vol_ratio", np.nan),
        "current_atr_pct": range_features.get("current_atr_pct", np.nan),
        "p95_abs_return": abs_return_quantiles["p95"],
        "p99_abs_return": abs_return_quantiles["p99"],
        "extreme_3sigma_rate": extreme_rates.get("abs_ret_gt_3_0sigma_rate", np.nan),
        "hist_var_5": tail_risk.get("hist_VaR", np.nan),
        "hist_cvar_5": tail_risk.get("hist_CVaR", np.nan),
        "var_5_loss_abs": tail_risk.get("loss_VaR_abs", np.nan),
        "cvar_5_loss_abs": tail_risk.get("loss_CVaR_abs", np.nan),
        "cornish_fisher_var_5": tail_risk.get("cornish_fisher_VaR", np.nan),
        "tail_ratio_95_05": tail_risk.get("tail_ratio_95_05", np.nan),
        "tail_loss_asymmetry": tail_risk.get("tail_loss_asymmetry", np.nan),
        "hill_tail_index": tail_risk.get("hill_tail_index", np.nan),
        "gpd_shape_xi": gpd_left.get("shape_xi", np.nan),
        "jump_pct_of_rv": bns.get("jump_pct_of_rv", np.nan),
        "continuous_vol_ann": bns.get("continuous_vol_ann", np.nan),
        "jump_vol_ann": bns.get("jump_vol_ann", np.nan),
        "jump_frequency": jump_stats.get("jump_frequency", np.nan),
        "jump_clustering_cv": jump_clustering.get("cv_inter_arrival", np.nan),
        "extremal_index_theta": extremal_index.get("theta", np.nan),
        "conditional_tail_amplification": conditional_extremes.get("tail_amplification", np.nan),
        "drawdown_at_risk_95": drawdown_at_risk.get("DaR_95", np.nan),
        "conditional_drawdown_at_risk_95_abs": abs(drawdown_at_risk.get("DaR_95", np.nan))
        if np.isfinite(_finite(drawdown_at_risk.get("DaR_95")))
        else np.nan,
        "permutation_entropy": entropy_summary.get("mean_normalized_PE", np.nan),
        "sample_entropy": sample_entropy.get("SampEn", np.nan),
        "volatility_lzc": lzc.get("LZC_volatility", np.nan),
        "entropy_rate": entropy_production.get("mean_rate", np.nan),
        "entropy_current_pe": entropy_production.get("current_PE", np.nan),
        "hurst_abs_returns": vol_memory_summary.get("H_abs_returns", np.nan),
        "hurst_sq_returns": vol_memory_summary.get("H_sq_returns", np.nan),
        "long_memory_flag": vol_memory_summary.get("long_memory_confirmed", None),
        "term_slope_short_to_long": term_slope,
        "trend_slope_pct_per_bar": trend.get("slope_pct_per_bar", np.nan),
        "trend_p": trend.get("p", np.nan),
        "regime_high_persistence": regimes.get("transition_matrix", {}).get("high_to_high", np.nan),
        "regime_low_persistence": regimes.get("transition_matrix", {}).get("low_to_low", np.nan),
        "arch_lm_p": arch_lm.get("p", np.nan) if arch_lm else np.nan,
        "ann_vol_ci_lo": vol_ci[0],
        "ann_vol_ci_hi": vol_ci[1],
        "fold_vol_cv": fold_stats.get("vol_cv", np.nan),
        "own_history_class": regimes.get("current_history_class", "med"),
        "current_binary_regime": regimes.get("current_binary_regime", None),
    }
    for window in cfg.vol_windows:
        summary[f"term_{window}_current_ann"] = term_structure.get(str(window), {}).get("current_ann", np.nan)
        summary[f"term_{window}_mean_ann"] = term_structure.get(str(window), {}).get("mean_ann", np.nan)
        summary[f"term_{window}_vol_of_vol"] = term_structure.get(str(window), {}).get("vol_of_vol", np.nan)

    result.update(
        {
            "meta": {
                "instrument": instrument,
                "n_bars": n,
                "freq": freq_label,
                "ppy": ppy,
                "start": str(df.index.min()),
                "end": str(df.index.max()),
                "resolution": cfg.resolution,
                "annualization": "crypto_365d_24x7",
            },
            "quality": quality,
            "summary": summary,
            "volatility": {
                "estimators": estimators,
                "term_structure": term_structure,
                "current_ann_vol": current_ann,
                "mean_ann_vol": mean_ann,
                "median_ann_vol": median_ann,
                "vol_of_vol": vol_of_vol,
                "vol_of_vol_abs": vol_of_vol_abs,
                "vol_cv": vol_cv,
                "trend": trend,
            },
            "features": {
                "spikes": spikes,
                "regimes": regimes,
                "asymmetry": asymmetry,
                "memory": memory,
                "volatility_memory": volatility_memory,
                "rolling_diagnostics": rolling_diagnostics,
                "distribution": distribution,
                "entropy": entropy_info,
                "tail_and_extremes": tail_and_extremes,
                "tail_risk": tail_risk,
                "extreme_events": extreme_events,
                "range": range_features,
                "fold_stability": fold_stats,
            },
            "inference": inference,
            "timeseries": {
                "rolling_vol": _downsample_xy(rv_default.index, rv_default.values, cfg.max_chart_points),
                "rolling_vol_zscore": _downsample_xy(rv_z.index, rv_z.values, cfg.max_chart_points),
                "close": _downsample_xy(df.index, df["close"].values, cfg.max_chart_points),
            },
            "spike_dates": spike_dates[-cfg.max_chart_points :],
        }
    )
    return result


FEATURE_METRICS = {
    "current_ann_vol": {"label": "Current Ann Vol", "weight": 0.24},
    "mean_ann_vol": {"label": "Mean Ann Vol", "weight": 0.12},
    "p95_abs_return": {"label": "P95 Abs Return", "weight": 0.07},
    "vol_of_vol": {"label": "Vol Of Vol", "weight": 0.09},
    "rolling_vol_stability_cv": {"label": "Rolling Vol CV", "weight": 0.05},
    "spike_frequency": {"label": "Spike Frequency", "weight": 0.05},
    "jump_pct_of_rv": {"label": "Jump Variance Share", "weight": 0.05},
    "cvar_5_loss_abs": {"label": "CVaR 5% Loss", "weight": 0.08},
    "tail_loss_asymmetry": {"label": "Tail Loss Asymmetry", "weight": 0.04},
    "conditional_tail_amplification": {"label": "High-Vol Tail Amp", "weight": 0.04},
    "range_vol_ratio": {"label": "Range/AbsRet Ratio", "weight": 0.03},
    "acf_abs_lag10": {"label": "AbsRet ACF L10", "weight": 0.04},
    "regime_high_persistence": {"label": "High-Regime Persistence", "weight": 0.04},
    "hurst_abs_returns": {"label": "Hurst |Returns|", "weight": 0.03},
    "downside_upside_ratio": {"label": "Down/Up Vol Ratio", "weight": 0.03},
    "permutation_entropy": {"label": "Permutation Entropy", "weight": 0.0, "inverse": True},
    "sample_entropy": {"label": "Sample Entropy", "weight": 0.0, "inverse": True},
    "hill_tail_index": {"label": "Hill Tail Index", "weight": 0.0, "inverse": True},
    "variance_ratio_5": {"label": "Variance Ratio q=5", "weight": 0.0},
    "cusum_stat": {"label": "CUSUM Instability", "weight": 0.0},
}


def classify_cross_section(
    results: Dict[str, Dict[str, Any]],
    cfg: VolatilityConfig,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    rows = []
    for instrument, info in results.items():
        if "summary" in info and "error" not in info:
            rows.append(dict(info["summary"]))
    if not rows:
        return [], [], {}

    df = pd.DataFrame(rows)
    for metric, spec in FEATURE_METRICS.items():
        if metric not in df.columns:
            df[metric] = np.nan
        pct = df[metric].rank(pct=True, method="average")
        if spec.get("inverse"):
            pct = 1.0 - pct
        df[f"{metric}_percentile"] = pct

    weights = {m: spec["weight"] for m, spec in FEATURE_METRICS.items()}
    weight_sum = sum(weights.values())
    composite = np.zeros(len(df), dtype=float)
    for metric, weight in weights.items():
        pct = df[f"{metric}_percentile"].fillna(0.5).astype(float).values
        composite += weight * pct
    df["composite_vol_score"] = composite / max(weight_sum, 1e-12)

    current_values = df["current_ann_vol"].astype(float).replace([np.inf, -np.inf], np.nan).values
    median_current = float(np.nanmedian(current_values))
    mad_current = float(np.nanmedian(np.abs(current_values - median_current)))
    if not np.isfinite(mad_current) or mad_current <= 1e-12:
        mad_current = float(np.nanstd(current_values))

    classes = []
    confidences = []
    confidence_labels = []
    support_notes = []
    for _, row in df.iterrows():
        score = _finite(row["composite_vol_score"], 0.5)
        current_z = _robust_z(current_values, _finite(row.get("current_ann_vol")))
        cls = _class_from_percentile(score)
        if np.isfinite(current_z):
            if current_z >= 2.0 and _class_rank(cls) < _class_rank("extreme_high"):
                cls = "extreme_high"
            elif current_z >= 1.0 and _class_rank(cls) < _class_rank("high"):
                cls = "high"
            elif current_z <= -2.0 and _class_rank(cls) > _class_rank("extreme_low"):
                cls = "extreme_low"
            elif current_z <= -1.0 and _class_rank(cls) > _class_rank("low"):
                cls = "low"

        ci_lo = _finite(row.get("ann_vol_ci_lo"))
        ci_hi = _finite(row.get("ann_vol_ci_hi"))
        quality = _finite(row.get("quality_score"), 0.0)
        n_bars = _finite(row.get("n_bars"), 0.0)
        distance = min(1.0, abs(score - 0.5) * 2.0)
        data_support = min(1.0, n_bars / max(cfg.min_bars, 1))
        ci_support = 0.5
        if np.isfinite(ci_lo) and np.isfinite(ci_hi):
            if cls in ("high", "extreme_high"):
                ci_support = 1.0 if ci_lo > median_current else 0.35 if ci_hi < median_current else 0.60
            elif cls in ("low", "extreme_low"):
                ci_support = 1.0 if ci_hi < median_current else 0.35 if ci_lo > median_current else 0.60
            else:
                ci_support = 0.75 if ci_lo <= median_current <= ci_hi else 0.60
        confidence = max(
            0.0,
            min(1.0, 0.22 + 0.34 * distance + 0.18 * quality + 0.14 * data_support + 0.12 * ci_support),
        )
        notes = []
        if np.isfinite(current_z):
            notes.append(f"robust_z={current_z:.2f}")
        notes.append(f"score_pct={score:.2f}")
        if np.isfinite(ci_lo) and np.isfinite(ci_hi):
            notes.append(f"ci95=[{ci_lo:.2%},{ci_hi:.2%}]")

        classes.append(cls)
        confidences.append(confidence)
        confidence_labels.append(_confidence_label(confidence))
        support_notes.append("; ".join(notes))

    df["volatility_class"] = classes
    df["classification_confidence"] = confidences
    df["classification_confidence_label"] = confidence_labels
    df["classification_support"] = support_notes
    df["current_ann_vol_robust_z"] = [
        _robust_z(current_values, _finite(x)) for x in df["current_ann_vol"].values
    ]

    for _, row in df.iterrows():
        instrument = str(row["instrument"])
        if instrument in results:
            classification = {
                "class": row["volatility_class"],
                "label": VOL_CLASS_LABELS.get(row["volatility_class"], "Med"),
                "composite_score": _finite(row["composite_vol_score"]),
                "confidence": _finite(row["classification_confidence"]),
                "confidence_label": row["classification_confidence_label"],
                "support": row["classification_support"],
                "current_ann_vol_robust_z": _finite(row["current_ann_vol_robust_z"]),
                "cross_section_percentiles": {
                    metric: _finite(row.get(f"{metric}_percentile")) for metric in FEATURE_METRICS
                },
            }
            results[instrument]["classification"] = classification
            results[instrument]["summary"].update(
                {
                    "volatility_class": classification["class"],
                    "volatility_class_label": classification["label"],
                    "composite_vol_score": classification["composite_score"],
                    "classification_confidence": classification["confidence"],
                    "classification_confidence_label": classification["confidence_label"],
                    "classification_support": classification["support"],
                    "current_ann_vol_robust_z": classification["current_ann_vol_robust_z"],
                }
            )

    feature_matrix: List[Dict[str, Any]] = []
    feature_classes_by_instrument: Dict[str, Dict[str, Any]] = {}
    for metric, spec in FEATURE_METRICS.items():
        values = df[metric].astype(float).replace([np.inf, -np.inf], np.nan).values
        for _, row in df.iterrows():
            value = _finite(row.get(metric))
            pctl = _pct_rank(values, value)
            if spec.get("inverse") and np.isfinite(pctl):
                pctl = 1.0 - pctl
            rz = _robust_z(values, value)
            if spec.get("inverse") and np.isfinite(rz):
                rz = -rz
            cls = _class_from_percentile(pctl)
            item = {
                "instrument": row["instrument"],
                "feature": metric,
                "feature_label": spec["label"],
                "value": value,
                "percentile": pctl,
                "robust_z": rz,
                "class": cls,
                "class_label": VOL_CLASS_LABELS.get(cls, "Med"),
            }
            feature_matrix.append(item)
            feature_classes_by_instrument.setdefault(str(row["instrument"]), {})[metric] = item

    for instrument, fmap in feature_classes_by_instrument.items():
        if instrument in results:
            results[instrument]["feature_classes"] = fmap

    q = {}
    for metric in [
        "current_ann_vol",
        "mean_ann_vol",
        "vol_of_vol",
        "spike_frequency",
        "cvar_5_loss_abs",
        "jump_pct_of_rv",
        "rolling_vol_stability_cv",
        "regime_high_persistence",
        "hurst_abs_returns",
        "permutation_entropy",
    ]:
        vals = df[metric].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        if len(vals):
            q[metric] = {
                "p10": float(vals.quantile(0.10)),
                "p30": float(vals.quantile(0.30)),
                "p50": float(vals.quantile(0.50)),
                "p70": float(vals.quantile(0.70)),
                "p90": float(vals.quantile(0.90)),
                "mean": float(vals.mean()),
                "std": float(vals.std(ddof=1)) if len(vals) > 1 else np.nan,
            }

    class_counts = df["volatility_class"].value_counts().to_dict()
    high_pressure = (
        class_counts.get("high", 0) + class_counts.get("extreme_high", 0)
    ) / max(len(df), 1)

    cross_section = {
        "n_instruments": int(len(df)),
        "classification_thresholds": {
            "percentile_bands": {
                "extreme_low": "<= 10th percentile",
                "low": "10th-30th percentile",
                "med": "30th-70th percentile",
                "high": "70th-90th percentile",
                "extreme_high": ">= 90th percentile",
            },
            "robust_z_overrides": {
                "extreme_high": "z >= 2.0",
                "high": "z >= 1.0",
                "low": "z <= -1.0",
                "extreme_low": "z <= -2.0",
            },
        },
        "metric_quantiles": q,
        "class_counts": {cls: int(class_counts.get(cls, 0)) for cls in VOL_CLASS_ORDER},
        "market_vol_pressure": {
            "high_or_extreme_high_pct": float(high_pressure),
            "median_current_ann_vol": q.get("current_ann_vol", {}).get("p50", np.nan),
            "median_vol_z": float(df["vol_z"].replace([np.inf, -np.inf], np.nan).median()),
            "median_spike_frequency": q.get("spike_frequency", {}).get("p50", np.nan),
            "median_cvar_5_loss_abs": q.get("cvar_5_loss_abs", {}).get("p50", np.nan),
            "median_jump_pct_of_rv": q.get("jump_pct_of_rv", {}).get("p50", np.nan),
            "long_memory_pct": float(df["long_memory_flag"].fillna(False).astype(bool).mean())
            if "long_memory_flag" in df.columns
            else np.nan,
            "arch_cluster_pct": float((df["arch_lm_p"].astype(float) < 0.05).mean())
            if "arch_lm_p" in df.columns
            else np.nan,
        },
    }

    summary_rows = [dict(row) for row in df.replace([np.inf, -np.inf], np.nan).to_dict(orient="records")]
    return summary_rows, feature_matrix, cross_section


def _feature_correlation_matrix(summary_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not summary_rows:
        return {"features": [], "matrix": [], "p_values": []}
    df = pd.DataFrame(summary_rows)
    features = [
        "current_ann_vol",
        "mean_ann_vol",
        "vol_of_vol",
        "rolling_vol_stability_cv",
        "spike_frequency",
        "jump_pct_of_rv",
        "acf_abs_lag10",
        "downside_upside_ratio",
        "range_vol_ratio",
        "p95_abs_return",
        "cvar_5_loss_abs",
        "tail_loss_asymmetry",
        "regime_high_persistence",
        "hurst_abs_returns",
        "permutation_entropy",
        "term_slope_short_to_long",
    ]
    available = [f for f in features if f in df.columns]
    corr = df[available].astype(float).replace([np.inf, -np.inf], np.nan).corr()
    p_values = pd.DataFrame(np.nan, index=available, columns=available)
    if _SCIPY:
        for a in available:
            for b in available:
                aa = df[a].astype(float).replace([np.inf, -np.inf], np.nan)
                bb = df[b].astype(float).replace([np.inf, -np.inf], np.nan)
                valid = aa.notna() & bb.notna()
                if valid.sum() >= 4:
                    try:
                        p_values.loc[a, b] = stats.spearmanr(aa[valid], bb[valid]).pvalue
                    except Exception:
                        pass
    return {
        "features": available,
        "matrix": corr.replace([np.inf, -np.inf], np.nan).values.tolist(),
        "p_values": p_values.values.tolist(),
    }


def _spike_timeline(results: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for info in results.values():
        for stamp in info.get("spike_dates", []):
            day = str(pd.Timestamp(stamp).date())
            counts[day] = counts.get(day, 0) + 1
    return [{"date": d, "count": counts[d]} for d in sorted(counts)]


def build_combined_analytics(
    results: Dict[str, Dict[str, Any]],
    raw_frames: Dict[str, pd.DataFrame],
    cfg: VolatilityConfig,
) -> Dict[str, Any]:
    valid = {k: v for k, v in results.items() if "summary" in v and "error" not in v}
    names = sorted(
        valid.keys(),
        key=lambda nm: _finite(valid[nm].get("summary", {}).get("current_ann_vol"), -np.inf),
        reverse=True,
    )
    corr_names = names[: min(CORRELATION_UNIVERSE_CAP, len(names))]

    ret_series: Dict[str, pd.Series] = {}
    for name in corr_names:
        if name not in raw_frames or raw_frames[name].empty:
            continue
        df = raw_frames[name]
        if "close" not in df.columns:
            continue
        close = pd.to_numeric(df["close"], errors="coerce")
        if "timestamp" in df.columns:
            ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            s = pd.Series(close.values, index=ts).dropna()
        else:
            s = close.dropna()
        if len(s) > 10:
            ret_series[name] = np.log(s / s.shift(1)).dropna()

    ret_corr_data: Dict[str, Any] = {"instruments": [], "matrix": []}
    if len(ret_series) >= 2:
        ret_df = pd.DataFrame(ret_series).dropna()
        if len(ret_df) > 10:
            rc = ret_df.corr()
            ret_corr_data = {
                "instruments": list(rc.columns),
                "matrix": rc.replace([np.inf, -np.inf], np.nan).fillna(0).values.tolist(),
            }

    vol_series: Dict[str, pd.Series] = {}
    for name in corr_names:
        ts = valid[name].get("timeseries", {}).get("rolling_vol", {})
        if ts.get("dates") and ts.get("values"):
            vals = [_finite(v, np.nan) for v in ts["values"]]
            vol_series[name] = pd.Series(vals, index=pd.to_datetime(ts["dates"]))

    vol_corr_data: Dict[str, Any] = {"instruments": [], "matrix": []}
    if len(vol_series) >= 2:
        vol_df = pd.DataFrame(vol_series).dropna()
        if len(vol_df) > 5:
            vc = vol_df.corr()
            vol_corr_data = {
                "instruments": list(vc.columns),
                "matrix": vc.replace([np.inf, -np.inf], np.nan).fillna(0).values.tolist(),
            }

    estimator_comparison: List[Dict[str, Any]] = []
    for name in names:
        est = valid[name].get("volatility", {}).get("estimators", {})
        row: Dict[str, Any] = {"instrument": name}
        for ename in ["close_close", "parkinson", "garman_klass", "rogers_satchell", "yang_zhang"]:
            row[ename] = _finite(est.get(ename, {}).get("current_ann"))
        estimator_comparison.append(row)

    return {
        "correlation_policy": {
            "universe_size": int(len(names)),
            "correlation_universe_cap": int(CORRELATION_UNIVERSE_CAP),
            "included_instruments": corr_names,
            "capped": bool(len(names) > len(corr_names)),
        },
        "return_correlation": ret_corr_data,
        "vol_correlation": vol_corr_data,
        "estimator_comparison": estimator_comparison,
    }


def build_ranking_tables(summary_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not summary_rows:
        return summary_rows

    ranking_metrics = [
        ("current_ann_vol", True),
        ("mean_ann_vol", True),
        ("vol_of_vol", True),
        ("rolling_vol_stability_cv", True),
        ("spike_frequency", True),
        ("jump_pct_of_rv", True),
        ("cvar_5_loss_abs", True),
        ("vol_z", True),
        ("composite_vol_score", True),
        ("acf_abs_lag10", True),
        ("regime_high_persistence", True),
        ("hurst_abs_returns", True),
        ("permutation_entropy", False),
        ("downside_upside_ratio", True),
        ("quality_score", False),
    ]
    for metric, descending in ranking_metrics:
        indexed = []
        for i, row in enumerate(summary_rows):
            v = _finite(row.get(metric))
            if np.isfinite(v):
                indexed.append((i, v))
        indexed.sort(key=lambda x: x[1], reverse=descending)
        for rank, (i, _) in enumerate(indexed, 1):
            summary_rows[i][f"{metric}_rank"] = rank
        done = {x[0] for x in indexed}
        for i in range(len(summary_rows)):
            if i not in done:
                summary_rows[i][f"{metric}_rank"] = None
    return summary_rows


def run_volatility_analysis(
    cfg: VolatilityConfig,
    instruments: Optional[List[str]] = None,
    include_regex: Optional[str] = None,
    exclude_regex: Optional[str] = None,
    max_instruments: Optional[int] = None,
    demo: bool = False,
) -> Dict[str, Any]:
    start_label, end_label, _, _ = _resolve_date_range(cfg)
    session = requests.Session()

    if demo:
        selected = instruments or [f"B-DEMO{i:02d}_USDT" for i in range(1, 25)]
        if max_instruments:
            selected = selected[:max_instruments]
        raw_data = _make_demo_data(selected, cfg)
    else:
        if instruments:
            selected = sorted(set(instruments))
        else:
            selected = fetch_active_instruments(cfg, session=session)
        if include_regex:
            rx = re.compile(include_regex)
            selected = [x for x in selected if rx.search(x)]
        if exclude_regex:
            rx = re.compile(exclude_regex)
            selected = [x for x in selected if not rx.search(x)]
        if max_instruments:
            selected = selected[:max_instruments]
        raw_data = {}

    print(f"[INFO] Analysing {len(selected)} instruments")
    instrument_results: Dict[str, Dict[str, Any]] = {}
    raw_frames: Dict[str, pd.DataFrame] = {}
    errors: Dict[str, str] = {}

    for i, instrument in enumerate(selected, start=1):
        print(f"[INFO] ({i}/{len(selected)}) {instrument}")
        try:
            if demo:
                df = raw_data[instrument]
            else:
                df = fetch_coindcx_candles(instrument, cfg, session=session)
            raw_frames[instrument] = df
            info = analyse_instrument(instrument, df, cfg)
            instrument_results[instrument] = info
            if "error" in info:
                errors[instrument] = str(info["error"])
        except Exception as exc:
            msg = str(exc)
            errors[instrument] = msg
            instrument_results[instrument] = {"instrument": instrument, "error": msg}
            print(f"[WARN] {instrument}: {msg}")

    summary_rows, feature_matrix, cross_section = classify_cross_section(instrument_results, cfg)
    corr_matrix = _feature_correlation_matrix(summary_rows)
    summary_rows = build_ranking_tables(summary_rows)
    combined = build_combined_analytics(instrument_results, raw_frames, cfg)

    report = {
        "meta": {
            "created_at_utc": str(_now_utc()),
            "start_date": start_label,
            "end_date": end_label or str(_now_utc().date()),
            "resolution": cfg.resolution,
            "margin_currency": cfg.margin_currency,
            "pcode": cfg.pcode,
            "n_requested": len(selected),
            "n_analyzed": len(summary_rows),
            "n_errors": len(errors),
            "source": "demo_synthetic" if demo else "coindcx_public_api",
            "config": asdict(cfg),
        },
        "cross_section": cross_section,
        "summary": summary_rows,
        "feature_matrix": feature_matrix,
        "feature_correlation_matrix": corr_matrix,
        "spike_timeline": _spike_timeline(instrument_results),
        "combined_analytics": combined,
        "instruments": instrument_results,
        "errors": errors,
    }
    return report


def _make_demo_data(instruments: List[str], cfg: VolatilityConfig) -> Dict[str, pd.DataFrame]:
    rng = np.random.default_rng(cfg.random_seed)
    start_label, end_label, start_ts, end_ts = _resolve_date_range(cfg)
    res_sec = _resolution_seconds(cfg.resolution)
    periods = max(220, min(5000, int((end_ts - start_ts) / res_sec)))
    idx = pd.date_range(
        start=pd.Timestamp(start_ts, unit="s", tz="UTC"),
        periods=periods,
        freq=f"{res_sec}s",
    )
    data: Dict[str, pd.DataFrame] = {}
    for i, instrument in enumerate(instruments):
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
        intrarange = np.maximum(np.abs(shock) * rng.uniform(1.5, 3.5, periods), sigma * 0.8)
        high = np.maximum(open_, close) * (1 + intrarange)
        low = np.minimum(open_, close) * np.maximum(0.01, 1 - intrarange)
        volume = rng.lognormal(mean=10.5 - i * 0.015, sigma=0.7, size=periods)
        data[instrument] = pd.DataFrame(
            {
                "timestamp": idx,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return data


def build_dashboard(report: Dict[str, Any], output_path: str) -> None:
    n_summary = len(report.get("summary", []))
    if n_summary <= SMALL_UNIVERSE_MAX:
        strategy = "full"
    elif n_summary <= MEDIUM_UNIVERSE_MAX:
        strategy = "grouped"
    else:
        strategy = "batched"
    dashboard_data = {
        "meta": report.get("meta", {}),
        "rendering": {
            "topK": TOP_K,
            "smallUniverseMax": SMALL_UNIVERSE_MAX,
            "mediumUniverseMax": MEDIUM_UNIVERSE_MAX,
            "tablePageSize": TABLE_PAGE_SIZE,
            "strategy": strategy,
            "nSummaryRows": n_summary,
        },
        "crossSection": report.get("cross_section", {}),
        "summary": report.get("summary", []),
        "featureMatrix": report.get("feature_matrix", []),
        "featureCorrelationMatrix": report.get("feature_correlation_matrix", {}),
        "spikeTimeline": report.get("spike_timeline", []),
        "combinedAnalytics": {
            "correlation_policy": report.get("combined_analytics", {}).get("correlation_policy", {}),
            "vol_correlation": report.get("combined_analytics", {}).get("vol_correlation", {}),
        },
        "instruments": {
            k: {
                "classification": v.get("classification", {}),
                "summary": v.get("summary", {}),
                "volatility": {"estimators": v.get("volatility", {}).get("estimators", {})},
                "features": {
                    "regimes": v.get("features", {}).get("regimes", {}),
                    "spikes": v.get("features", {}).get("spikes", {}),
                },
                "inference": {
                    "ann_vol_ci_95_normal_theory": v.get("inference", {}).get(
                        "ann_vol_ci_95_normal_theory", {}
                    )
                },
                "timeseries": {
                    "rolling_vol": v.get("timeseries", {}).get("rolling_vol", {}),
                },
                "spike_dates": v.get("spike_dates", []),
            }
            for k, v in report.get("instruments", {}).items()
            if "summary" in v and "error" not in v
        },
    }
    data_json = json.dumps(_serialise(dashboard_data), separators=(",", ":")).replace("</", "<\\/")
    html = _dashboard_template().replace("__VOLATILITY_DATA__", data_json)
    Path(output_path).write_text(html, encoding="utf-8")
    print(f"[INFO] HTML dashboard saved to {output_path}")


def _dashboard_template() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volatility Intelligence Terminal</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e17;--panel:rgba(15,20,35,.88);--panel-border:rgba(255,255,255,.07);--ink:#e2e8f0;--muted:#7a849b;--dim:#4a5568;--accent:#00d4aa;--accent2:#7c5cfc;--danger:#ff4d6a;--warn:#ffb347;--xhigh:#ff2d55;--high:#ff8c42;--med:#5a6478;--low:#4da6ff;--xlow:#00d4aa;--row-hover:rgba(0,212,170,.06);--selected:rgba(0,212,170,.12);font-family:Inter,system-ui,sans-serif}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);min-height:100vh}
::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#2d3748;border-radius:3px}
header{padding:16px 24px;background:linear-gradient(135deg,#0f1724,#141c2e);border-bottom:1px solid var(--panel-border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
h1{font-size:18px;font-weight:800;letter-spacing:-.3px;background:linear-gradient(90deg,#00d4aa,#7c5cfc);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.meta-row{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.meta-row span{display:flex;align-items:center;gap:4px}
.shell{display:grid;grid-template-columns:260px minmax(0,1fr);gap:14px;padding:14px;max-width:2200px;margin:0 auto}
aside{position:sticky;top:12px;align-self:start;background:var(--panel);border:1px solid var(--panel-border);border-radius:10px;padding:14px;max-height:calc(100vh - 24px);overflow-y:auto;backdrop-filter:blur(16px)}
.ftitle{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--accent);margin:14px 0 8px}
.ftitle:first-child{margin-top:0}
label{display:grid;gap:4px;font-size:11px;color:var(--muted);margin:6px 0}
input,select{width:100%;height:32px;border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:0 8px;color:var(--ink);background:rgba(0,0,0,.3);font-size:12px;font-family:inherit;outline:none;transition:border-color .2s}
input:focus,select:focus{border-color:var(--accent)}
.checks{display:grid;grid-template-columns:1fr 1fr;gap:4px 8px;margin:6px 0}
.checks label{display:flex;align-items:center;gap:6px;margin:0;color:var(--ink);font-size:12px;cursor:pointer}
.checks input[type=checkbox]{width:14px;height:14px;accent-color:var(--accent)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.btn-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px}
button{height:32px;border:1px solid rgba(255,255,255,.1);border-radius:6px;background:rgba(255,255,255,.04);color:var(--ink);font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .2s}
button:hover{background:rgba(255,255,255,.08)}
button.primary{background:var(--accent);border-color:var(--accent);color:#0a0e17;font-weight:700}
button.primary:hover{background:#00e6b8}
main{min-width:0;display:grid;gap:14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px}
.kpi{background:var(--panel);border:1px solid var(--panel-border);border-radius:10px;padding:12px 14px;backdrop-filter:blur(12px);transition:transform .2s}
.kpi:hover{transform:translateY(-2px)}
.kpi .lb{color:var(--muted);font-size:10px;text-transform:uppercase;font-weight:700;letter-spacing:.5px}
.kpi .vl{margin-top:6px;font-size:22px;font-weight:800;font-variant-numeric:tabular-nums}
.g2{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr);gap:14px}
.g3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.panel{background:var(--panel);border:1px solid var(--panel-border);border-radius:10px;padding:14px;backdrop-filter:blur(12px);min-width:0}
.panel h2{font-size:13px;font-weight:700;margin-bottom:10px;color:var(--ink);display:flex;align-items:center;gap:6px}
.panel h2::before{content:'';width:3px;height:14px;background:var(--accent);border-radius:2px}
.chart{width:100%;height:320px;display:block}
.chart.tall{height:420px}
.chart.short{height:250px}
svg text{fill:var(--muted);font-size:10px;font-family:Inter,system-ui,sans-serif}
.ax{stroke:rgba(255,255,255,.12);stroke-width:1;shape-rendering:crispEdges}
.gl{stroke:rgba(255,255,255,.04);stroke-width:1;shape-rendering:crispEdges}
.legend{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:11px;color:var(--muted)}
.sw{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:3px;vertical-align:-1px}
.table-wrap{overflow:auto;max-height:520px;border:1px solid var(--panel-border);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:1000px;font-size:11px;font-variant-numeric:tabular-nums}
thead th{position:sticky;top:0;z-index:2;background:#111827;border-bottom:1px solid rgba(255,255,255,.1);text-align:left;padding:8px;white-space:nowrap;cursor:pointer;color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.4px;transition:color .2s;user-select:none}
thead th:hover{color:var(--accent)}
thead th.sorted{color:var(--accent)}
tbody td{border-bottom:1px solid rgba(255,255,255,.04);padding:6px 8px;white-space:nowrap}
tbody tr{transition:background .15s}
tbody tr:hover{background:var(--row-hover)}
tbody tr.sel{background:var(--selected)}
.pill{display:inline-flex;align-items:center;border-radius:4px;padding:2px 7px;font-weight:700;font-size:10px;letter-spacing:.3px}
.pill.extreme_high{background:var(--xhigh);color:#fff}.pill.high{background:var(--high);color:#fff}.pill.med{background:var(--med);color:#fff}.pill.low{background:var(--low);color:#fff}.pill.extreme_low{background:var(--xlow);color:#0a0e17}
.detail{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}
.mc{border:1px solid var(--panel-border);border-radius:6px;padding:8px 10px}
.mc .nm{color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase}
.mc .nv{margin-top:4px;font-weight:800;font-size:15px;font-variant-numeric:tabular-nums}
.note{color:var(--muted);font-size:11px;line-height:1.5;margin-top:8px}
.tip{position:fixed;pointer-events:none;z-index:50;background:#1a2332;color:#e2e8f0;padding:8px 10px;border-radius:8px;font-size:11px;max-width:300px;display:none;border:1px solid rgba(255,255,255,.1);box-shadow:0 8px 24px rgba(0,0,0,.4)}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.panel,.kpi{animation:fadeIn .4s ease both}
@media(max-width:1100px){.shell{grid-template-columns:1fr}aside{position:static;max-height:none}.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.g2,.g3{grid-template-columns:1fr}}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.detail{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
</head>
<body>
<header>
<div><h1>Volatility Intelligence Terminal</h1></div>
<div class="meta-row" id="metaLine"></div>
</header>
<div class="shell">
<aside>
<div class="ftitle">Search</div>
<input id="search" type="search" placeholder="Filter instruments...">
<div class="ftitle">Volatility Class</div>
<div class="checks" id="classChecks"></div>
<div class="ftitle">Ranges</div>
<div class="two-col">
<label>Min Bars<input id="minBars" type="number" min="0" placeholder="0"></label>
<label>Min Quality<input id="minQuality" type="number" min="0" max="1" step=".05" placeholder="0"></label>
</div>
<div class="two-col">
<label>Vol Min<input id="volMin" type="number" step=".01" placeholder="—"></label>
<label>Vol Max<input id="volMax" type="number" min="0" step=".01" placeholder="∞"></label>
</div>
<div class="two-col">
<label>Z Min<input id="zMin" type="number" step=".25" placeholder="-∞"></label>
<label>Z Max<input id="zMax" type="number" step=".25" placeholder="∞"></label>
</div>
<div class="ftitle">Controls</div>
<label>Sort Metric<select id="sortMetric"></select></label>
<label>Scatter X<select id="scatterX"></select></label>
<label>Scatter Y<select id="scatterY"></select></label>
<label>Heatmap Window<select id="heatWindow"></select></label>
<label>Top N<input id="topN" type="number" min="3" max="250" value="30"></label>
<div class="checks">
<label><input id="archOnly" type="checkbox"> ARCH p&lt;.05</label>
<label><input id="spikeOnly" type="checkbox"> Spikes only</label>
</div>
<div class="btn-row">
<button id="resetBtn">Reset</button>
<button id="csvBtn" class="primary">Export CSV</button>
</div>
</aside>
<main>
<section class="kpis" id="kpis"></section>
<section class="g2">
<div class="panel"><h2>Ranked Volatility</h2><svg class="chart tall" id="rankChart"></svg><div class="legend" id="classLegend"></div></div>
<div class="panel"><h2>Class Distribution</h2><svg class="chart short" id="classChart"></svg><h2>Volatility Histogram</h2><svg class="chart short" id="histChart"></svg></div>
</section>
<section class="g2">
<div class="panel"><h2>Cross-Section Scatter</h2><svg class="chart" id="scatterChart"></svg></div>
<div class="panel"><h2>Class Box Plot</h2><svg class="chart" id="boxChart"></svg></div>
</section>
<section class="g2">
<div class="panel"><h2>Term Structure Heatmap</h2><svg class="chart tall" id="termHeat"></svg></div>
<div class="panel"><h2>Feature Class Heatmap</h2><svg class="chart tall" id="featHeat"></svg></div>
</section>
<section class="g2">
<div class="panel"><h2>Rolling Volatility Overlay</h2><svg class="chart" id="lineChart"></svg></div>
<div class="panel"><h2>Spike Timeline</h2><svg class="chart" id="spikeChart"></svg></div>
</section>
<section class="g2">
<div class="panel"><h2>Return Correlation</h2><svg class="chart" id="retCorr"></svg></div>
<div class="panel"><h2>Feature Correlation</h2><svg class="chart" id="featCorr"></svg></div>
</section>
<section class="panel"><h2>Instrument Rankings</h2><div class="table-wrap"><table id="tbl"></table></div></section>
<section class="panel"><h2>Instrument Drilldown</h2><div class="detail" id="detailGrid"></div><p class="note" id="detailNote">Click any instrument in charts or table to inspect.</p></section>
</main>
</div>
<div class="tip" id="tip"></div>
<script id="vol-data" type="application/json">__VOLATILITY_DATA__</script>

<script>
const D=JSON.parse(document.getElementById("vol-data").textContent);
const S=D.summary||[],I=D.instruments||{},FM=D.featureMatrix||[],CA=D.combinedAnalytics||{};
const CO=["extreme_high","high","med","low","extreme_low"];
const CL={extreme_high:"Extreme High",high:"High",med:"Med",low:"Low",extreme_low:"Extreme Low"};
const CC={extreme_high:"#ff2d55",high:"#ff8c42",med:"#5a6478",low:"#4da6ff",extreme_low:"#00d4aa"};
const LC=["#00d4aa","#7c5cfc","#ff4d6a","#ffb347","#4da6ff","#ff8c42"];
const M=[["current_ann_vol","Current Ann Vol",1],["mean_ann_vol","Mean Ann Vol",1],["vol_of_vol","Vol Of Vol",0],["spike_frequency","Spike Freq",0],["vol_z","Vol Z",0],["acf_abs_lag10","ACF L10",0],["downside_upside_ratio","Down/Up",0],["range_vol_ratio","Range/AbsRet",0],["p95_abs_return","P95 AbsRet",1],["composite_vol_score","Composite",0],["classification_confidence","Confidence",0],["quality_score","Quality",0],["n_bars","Bars",0]];
const W=Object.keys(S[0]||{}).filter(k=>/^term_\d+_current_ann$/.test(k)).map(k=>k.match(/\d+/)[0]);
let sel=new Set(S.slice().sort((a,b)=>n(b.current_ann_vol)-n(a.current_ann_vol)).slice(0,3).map(r=>r.instrument));
let sAsc=false,tSort="current_ann_vol",_deb=null;
function $(id){return document.getElementById(id)}
function n(v,f=NaN){if(v===""||v===null||v===undefined)return f;const x=Number(v);return Number.isFinite(x)?x:f}
function p(v){return Number.isFinite(n(v))?(n(v)*100).toFixed(2)+"%":"—"}
function d(v,dp=3){return Number.isFinite(n(v))?n(v).toFixed(dp):"—"}
function e(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]))}
function sv(tag,a={}){const nd=document.createElementNS("http://www.w3.org/2000/svg",tag);Object.entries(a).forEach(([k,v])=>nd.setAttribute(k,v));return nd}
function clr(nd){while(nd.firstChild)nd.removeChild(nd.firstChild)}
function dm(nd){const r=nd.getBoundingClientRect();return{w:Math.max(300,r.width||600),h:Math.max(200,r.height||300)}}
function ext(vs){const xs=vs.map(Number).filter(Number.isFinite);if(!xs.length)return[0,1];let a=Math.min(...xs),b=Math.max(...xs);if(a===b){a-=1;b+=1}return[a,b]}
function sc(dom,rng){const[d0,d1]=dom,[r0,r1]=rng,dn=d1-d0||1;return x=>r0+((x-d0)/dn)*(r1-r0)}
function at(g,t,x,y,a={}){const nd=sv("text",{x,y,...a});nd.textContent=t;g.appendChild(nd);return nd}
function fa(x){if(!Number.isFinite(x))return"—";if(Math.abs(x)>=10)return x.toFixed(0);if(Math.abs(x)>=1)return x.toFixed(2);return x.toFixed(3)}
function axes(g,xs,ys,xd,yd,b,xl,yl,pY){
g.appendChild(sv("line",{x1:b.l,y1:b.t+b.h,x2:b.l+b.w,y2:b.t+b.h,class:"ax"}));
g.appendChild(sv("line",{x1:b.l,y1:b.t,x2:b.l,y2:b.t+b.h,class:"ax"}));
for(let i=0;i<=4;i++){
const tx=xd[0]+(xd[1]-xd[0])*i/4,x=xs(tx);
g.appendChild(sv("line",{x1:x,y1:b.t,x2:x,y2:b.t+b.h,class:"gl"}));
at(g,fa(tx),x,b.t+b.h+15,{"text-anchor":"middle"});
const ty=yd[0]+(yd[1]-yd[0])*i/4,y=ys(ty);
g.appendChild(sv("line",{x1:b.l,y1:y,x2:b.l+b.w,y2:y,class:"gl"}));
at(g,pY?p(ty):fa(ty),b.l-6,y+4,{"text-anchor":"end"});
}
at(g,xl,b.l+b.w/2,b.t+b.h+34,{"text-anchor":"middle","font-weight":"700"});
const yt=at(g,yl,12,b.t+b.h/2,{"text-anchor":"middle","font-weight":"700"});
yt.setAttribute("transform",`rotate(-90 12 ${b.t+b.h/2})`);
}
function tip(ev,h){const t=$("tip");t.innerHTML=h;t.style.display="block";t.style.left=Math.min(window.innerWidth-310,ev.clientX+12)+"px";t.style.top=Math.min(window.innerHeight-100,ev.clientY+12)+"px"}
function ht(){$("tip").style.display="none"}
function bl(a,b,t){const ca=hx(a),cb=hx(b),c=ca.map((x,i)=>Math.round(x+(cb[i]-x)*Math.max(0,Math.min(1,t))));return`rgb(${c[0]},${c[1]},${c[2]})`}
function hx(h){const x=h.replace("#","");return[parseInt(x.slice(0,2),16),parseInt(x.slice(2,4),16),parseInt(x.slice(4,6),16)]}
function crc(v){if(!Number.isFinite(v))return"#1a2332";return v>=0?bl("#1a2332","#ff2d55",Math.abs(v)):bl("#1a2332","#4da6ff",Math.abs(v))}
function sl(s){return String(s).replace(/current_|ann_|returns?|frequency/g,"").replace(/_/g," ").slice(0,16)}

function setup(){
$("metaLine").innerHTML=[`Created: ${e(D.meta?.created_at_utc||"")}`,`Range: ${e(D.meta?.start_date||"")} → ${e(D.meta?.end_date||"")}`,`Res: ${e(D.meta?.resolution||"")}min`,`N=${S.length}`].map(x=>`<span>${x}</span>`).join("");
$("classChecks").innerHTML=CO.map(c=>`<label><input type="checkbox" data-class="${c}" checked><span class="sw" style="background:${CC[c]}"></span>${CL[c]}</label>`).join("");
const mo=M.map(([k,l])=>`<option value="${k}">${l}</option>`).join("");
$("sortMetric").innerHTML=mo;$("scatterX").innerHTML=mo;$("scatterY").innerHTML=mo;
$("sortMetric").value="current_ann_vol";$("scatterX").value="current_ann_vol";$("scatterY").value="vol_of_vol";
$("heatWindow").innerHTML=W.map(w=>`<option value="${w}">${w} bars</option>`).join("");
$("heatWindow").value=W.includes("21")?"21":(W[0]||"");
$("classLegend").innerHTML=CO.map(c=>`<span><span class="sw" style="background:${CC[c]}"></span>${CL[c]}</span>`).join("");
document.querySelectorAll("aside input,aside select").forEach(nd=>nd.addEventListener("input",debRender));
$("resetBtn").addEventListener("click",()=>{["search","volMin","volMax","zMin","zMax","minBars","minQuality"].forEach(id=>$(id).value="");$("topN").value=30;$("archOnly").checked=false;$("spikeOnly").checked=false;document.querySelectorAll("#classChecks input").forEach(c=>c.checked=true);renderAll()});
$("csvBtn").addEventListener("click",dlCsv);
}
function debRender(){clearTimeout(_deb);_deb=setTimeout(renderAll,120)}
function acls(){return new Set([...document.querySelectorAll("#classChecks input:checked")].map(x=>x.dataset.class))}
function fRows(){
const s=$("search").value.trim().toLowerCase(),cl=acls(),mb=n($("minBars").value,0),mq=n($("minQuality").value,0),vn=n($("volMin").value,-Infinity),vx=n($("volMax").value,Infinity),zn=n($("zMin").value,-Infinity),zx=n($("zMax").value,Infinity),ao=$("archOnly").checked,so=$("spikeOnly").checked;
return S.filter(r=>{
if(s&&!String(r.instrument).toLowerCase().includes(s))return false;
if(!cl.has(r.volatility_class||"med"))return false;
if(n(r.n_bars,0)<mb||n(r.quality_score,0)<mq)return false;
if(n(r.current_ann_vol)<vn||n(r.current_ann_vol)>vx)return false;
if(n(r.vol_z)<zn||n(r.vol_z)>zx)return false;
if(ao&&!(n(r.arch_lm_p,1)<.05))return false;
if(so&&!(n(r.n_spikes,0)>0))return false;
return true;
});}
function sRows(rows){const k=$("sortMetric").value||tSort;return rows.slice().sort((a,b)=>sAsc?n(a[k],-Infinity)-n(b[k],-Infinity):n(b[k],-Infinity)-n(a[k],-Infinity))}
function tRows(rows){return sRows(rows).slice(0,Math.max(3,n($("topN").value,30)))}
function togSel(nm){if(sel.has(nm))sel.delete(nm);else sel.add(nm);if(sel.size>6)sel=new Set([...sel].slice(-6));renderAll()}

function rKpi(rows){
const hc=rows.filter(r=>["high","extreme_high"].includes(r.volatility_class)).length;
const cv=rows.map(r=>n(r.current_ann_vol)).filter(Number.isFinite).sort((a,b)=>a-b);
const zv=rows.map(r=>n(r.vol_z)).filter(Number.isFinite).sort((a,b)=>a-b);
const md=a=>a.length?a[Math.floor(a.length/2)]:NaN;
[["Instruments",rows.length],["High Pressure",p(hc/Math.max(rows.length,1))],["Median Vol",p(md(cv))],["Median Z",d(md(zv),2)],["Selected",sel.size],["Errors",Object.keys(D.meta?.n_errors||{}).length||D.meta?.n_errors||0]].forEach(([l,v],i)=>{
const el=$("kpis");if(i===0)el.innerHTML="";
el.innerHTML+=`<div class="kpi"><div class="lb">${l}</div><div class="vl">${v}</div></div>`;
});}

function rClass(rows){
const nd=$("classChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const cts=CO.map(c=>rows.filter(r=>(r.volatility_class||"med")===c).length);
const tot=Math.max(1,rows.length);
const b={l:110,t:15,w:w-135,h:h-35};
const rh=b.h/CO.length;
CO.forEach((c,i)=>{const y=b.t+i*rh+4;const bw=Math.max(2,b.w*cts[i]/Math.max(1,...cts));
at(nd,CL[c],b.l-8,y+rh*.5,{"text-anchor":"end"});
const r=sv("rect",{x:b.l,y,width:bw,height:Math.max(6,rh-8),fill:CC[c],rx:4,opacity:.85});
r.addEventListener("mousemove",ev=>tip(ev,`<b>${CL[c]}</b><br>${cts[i]} instruments (${(cts[i]/tot*100).toFixed(1)}%)`));
r.addEventListener("mouseleave",ht);nd.appendChild(r);
at(nd,String(cts[i]),b.l+bw+6,y+rh*.5+3,{"font-weight":"700",fill:"#e2e8f0"});
});}

function rRank(rows){
const nd=$("rankChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const k=$("sortMetric").value;const lb=M.find(m=>m[0]===k)?.[1]||k;
const data=tRows(rows);const vs=data.map(r=>n(r[k],0));const mx=Math.max(1e-9,...vs.map(Math.abs));
const b={l:130,t:12,w:w-160,h:h-35};const rh=b.h/Math.max(1,data.length);
data.forEach((r,i)=>{const y=b.t+i*rh+1;const bw=Math.max(2,b.w*Math.abs(n(r[k],0))/mx);
at(nd,r.instrument,b.l-6,y+rh*.6,{"text-anchor":"end",fill:"#e2e8f0"});
const rect=sv("rect",{x:b.l,y,width:bw,height:Math.max(4,rh-3),fill:CC[r.volatility_class||"med"],rx:3,opacity:.85});
rect.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${e(lb)}: ${M.find(m=>m[0]===k)?.[2]?p(r[k]):d(r[k],4)}<br>${CL[r.volatility_class]||"Med"}`));
rect.addEventListener("mouseleave",ht);rect.addEventListener("click",()=>togSel(r.instrument));rect.style.cursor="pointer";
nd.appendChild(rect);
at(nd,M.find(m=>m[0]===k)?.[2]?p(r[k]):d(r[k],3),b.l+bw+5,y+rh*.6,{"font-weight":"700",fill:"#e2e8f0"});
});}

function rHist(rows){
const nd=$("histChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const vs=rows.map(r=>n(r.current_ann_vol)).filter(Number.isFinite);if(!vs.length)return;
const[lo,hi]=ext(vs);const nb=16;const cts=Array(nb).fill(0);
vs.forEach(v=>cts[Math.min(nb-1,Math.max(0,Math.floor((v-lo)/((hi-lo)||1)*nb)))]++);
const b={l:42,t:12,w:w-60,h:h-45};
const x=sc([lo,hi],[b.l,b.l+b.w]),y=sc([0,Math.max(1,...cts)],[b.t+b.h,b.t]);
axes(nd,x,y,[lo,hi],[0,Math.max(1,...cts)],b,"Current Ann Vol","Count",false);
cts.forEach((c,i)=>{const x0=x(lo+(hi-lo)*i/nb),x1=x(lo+(hi-lo)*(i+1)/nb);
nd.appendChild(sv("rect",{x:x0+1,y:y(c),width:Math.max(1,x1-x0-2),height:b.t+b.h-y(c),fill:"#00d4aa",opacity:.6,rx:2}));
});}

function rScatter(rows){
const nd=$("scatterChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const xK=$("scatterX").value,yK=$("scatterY").value;
const data=rows.filter(r=>Number.isFinite(n(r[xK]))&&Number.isFinite(n(r[yK])));if(!data.length)return;
const xD=ext(data.map(r=>n(r[xK]))),yD=ext(data.map(r=>n(r[yK])));
const b={l:55,t:18,w:w-75,h:h-58};
const x=sc(xD,[b.l,b.l+b.w]),y=sc(yD,[b.t+b.h,b.t]);
const xL=M.find(m=>m[0]===xK)?.[1]||xK,yL=M.find(m=>m[0]===yK)?.[1]||yK;
axes(nd,x,y,xD,yD,b,xL,yL,M.find(m=>m[0]===yK)?.[2]);
data.forEach(r=>{const c=r.volatility_class||"med";const isSel=sel.has(r.instrument);
const ci=sv("circle",{cx:x(n(r[xK])),cy:y(n(r[yK])),r:isSel?7:4,fill:CC[c],opacity:.8,stroke:isSel?"#fff":"none","stroke-width":isSel?2:0});
ci.style.cursor="pointer";
ci.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${e(xL)}: ${d(r[xK],4)}<br>${e(yL)}: ${d(r[yK],4)}<br>${CL[c]}`));
ci.addEventListener("mouseleave",ht);ci.addEventListener("click",()=>togSel(r.instrument));
nd.appendChild(ci);
});}

function rBox(rows){
const nd=$("boxChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const gs=CO.map(c=>rows.filter(r=>(r.volatility_class||"med")===c).map(r=>n(r.current_ann_vol)).filter(Number.isFinite));
const all=gs.flat();if(!all.length)return;
const yD=ext(all);const b={l:55,t:18,w:w-75,h:h-65};
const y=sc(yD,[b.t+b.h,b.t]);const step=b.w/CO.length;
for(let i=0;i<=4;i++){const ty=yD[0]+(yD[1]-yD[0])*i/4;const yp=y(ty);
nd.appendChild(sv("line",{x1:b.l,y1:yp,x2:b.l+b.w,y2:yp,class:"gl"}));
at(nd,p(ty),b.l-6,yp+4,{"text-anchor":"end"});}
gs.forEach((vs,i)=>{if(!vs.length)return;vs.sort((a,b)=>a-b);
const q=pv=>vs[Math.min(vs.length-1,Math.max(0,Math.floor((vs.length-1)*pv)))];
const cx=b.l+step*i+step/2;const q1=q(.25),md=q(.5),q3=q(.75),lo=q(.05),hi=q(.95);
nd.appendChild(sv("line",{x1:cx,y1:y(lo),x2:cx,y2:y(hi),stroke:CC[CO[i]],"stroke-width":2}));
nd.appendChild(sv("rect",{x:cx-step*.22,y:y(q3),width:step*.44,height:Math.max(2,y(q1)-y(q3)),fill:CC[CO[i]],opacity:.5,rx:3}));
nd.appendChild(sv("line",{x1:cx-step*.22,y1:y(md),x2:cx+step*.22,y2:y(md),stroke:"#fff","stroke-width":2}));
at(nd,CL[CO[i]],cx,b.t+b.h+22,{"text-anchor":"middle",fill:"#e2e8f0"});
});}

function rTermHeat(rows){
const nd=$("termHeat");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const data=tRows(rows);if(!data.length||!W.length)return;
const b={l:125,t:26,w:w-145,h:h-42};const cw=b.w/W.length;const ch=Math.max(10,b.h/data.length);
const vs=data.flatMap(r=>W.map(wi=>n(r[`term_${wi}_current_ann`]))).filter(Number.isFinite);
const[lo,hi]=ext(vs);
W.forEach((wi,j)=>at(nd,`${wi}`,b.l+j*cw+cw/2,18,{"text-anchor":"middle","font-weight":"700",fill:"#e2e8f0"}));
data.forEach((r,i)=>{const y=b.t+i*ch;
at(nd,r.instrument,b.l-6,y+ch*.7,{"text-anchor":"end",fill:"#a0aec0"});
W.forEach((wi,j)=>{const v=n(r[`term_${wi}_current_ann`]);const t=Number.isFinite(v)?(v-lo)/((hi-lo)||1):0;
const f=bl("#0f1724","#ff2d55",t);
const rc=sv("rect",{x:b.l+j*cw,y,width:Math.max(1,cw-1),height:Math.max(1,ch-1),fill:f,rx:1});
rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${wi}-bar vol: ${p(v)}`));
rc.addEventListener("mouseleave",ht);rc.addEventListener("click",()=>togSel(r.instrument));rc.style.cursor="pointer";
nd.appendChild(rc);});});}

function rFeatHeat(rows){
const nd=$("featHeat");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const dr=tRows(rows);const nms=dr.map(r=>r.instrument);
const feats=[...new Set(FM.map(f=>f.feature))];
const lm=Object.fromEntries(FM.map(f=>[f.feature,f.feature_label||f.feature]));
const lu=new Map(FM.map(f=>[f.instrument+"|"+f.feature,f]));
if(!nms.length||!feats.length)return;
const b={l:125,t:68,w:w-145,h:h-85};const cw=b.w/feats.length;const ch=Math.max(10,b.h/nms.length);
feats.forEach((f,j)=>{const tx=at(nd,sl(lm[f]),b.l+j*cw+cw/2,60,{"text-anchor":"end","font-weight":"600",fill:"#a0aec0"});
tx.setAttribute("transform",`rotate(-45 ${b.l+j*cw+cw/2} 60)`);});
nms.forEach((nm,i)=>{const y=b.t+i*ch;
at(nd,nm,b.l-6,y+ch*.7,{"text-anchor":"end",fill:"#a0aec0"});
feats.forEach((f,j)=>{const it=lu.get(nm+"|"+f);const c=it?.class||"med";
const rc=sv("rect",{x:b.l+j*cw,y,width:Math.max(1,cw-1),height:Math.max(1,ch-1),fill:CC[c],opacity:.78,rx:1});
rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(nm)}</b><br>${e(lm[f])}<br>${CL[c]}: ${d(it?.value,4)}<br>pctl: ${d(it?.percentile,2)}`));
rc.addEventListener("mouseleave",ht);rc.addEventListener("click",()=>togSel(nm));rc.style.cursor="pointer";
nd.appendChild(rc);});});}

function rLine(){
const nd=$("lineChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const nms=[...sel].filter(nm=>I[nm]);if(!nms.length){at(nd,"Select instruments to compare",18,30,{"font-weight":"600",fill:"#7a849b"});return;}
const sr=nms.map(nm=>{const ts=I[nm]?.timeseries?.rolling_vol||{dates:[],values:[]};return{nm,vs:(ts.values||[]).map(n),ds:ts.dates||[]}}).filter(s=>s.vs.length);
const allY=sr.flatMap(s=>s.vs).filter(Number.isFinite);if(!allY.length)return;
const ml=Math.max(...sr.map(s=>s.vs.length));
const xD=[0,Math.max(1,ml-1)],yD=ext(allY);
const b={l:55,t:18,w:w-78,h:h-58};
const x=sc(xD,[b.l,b.l+b.w]),y=sc(yD,[b.t+b.h,b.t]);
axes(nd,x,y,xD,yD,b,"Time","Ann Vol",true);
sr.forEach((s,idx)=>{const pts=s.vs.map((v,i)=>Number.isFinite(v)?`${i?"L":"M"}${x(i)},${y(v)}`:"").filter(Boolean).join(" ");
if(pts){nd.appendChild(sv("path",{d:pts,fill:"none",stroke:LC[idx%LC.length],"stroke-width":2,opacity:.85}));
at(nd,s.nm,b.l+8,b.t+14+idx*15,{fill:LC[idx%LC.length],"font-weight":"700"});}});}

function rSpike(rows){
const nd=$("spikeChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const al=new Set(rows.map(r=>r.instrument));const cts=new Map();
Object.entries(I).forEach(([nm,info])=>{if(!al.has(nm))return;
(info.spike_dates||[]).forEach(st=>{const dy=String(st).slice(0,10);if(dy)cts.set(dy,(cts.get(dy)||0)+1);});});
const tl=[...cts.entries()].sort((a,b)=>a[0].localeCompare(b[0])).map(([dt,ct])=>({dt,ct}));
if(!tl.length){at(nd,"No spikes detected",18,30,{"font-weight":"600",fill:"#7a849b"});return;}
const b={l:42,t:18,w:w-60,h:h-55};const mx=Math.max(1,...tl.map(d=>n(d.ct,0)));
const y=sc([0,mx],[b.t+b.h,b.t]);const bw=Math.max(1,b.w/tl.length);
for(let i=0;i<=4;i++){const ty=mx*i/4;const yp=y(ty);nd.appendChild(sv("line",{x1:b.l,y1:yp,x2:b.l+b.w,y2:yp,class:"gl"}));at(nd,fa(ty),b.l-6,yp+4,{"text-anchor":"end"});}
tl.forEach((dd,i)=>{const rc=sv("rect",{x:b.l+i*bw,y:y(n(dd.ct,0)),width:Math.max(1,bw-1),height:b.t+b.h-y(n(dd.ct,0)),fill:"#ff8c42",opacity:.65,rx:1});
rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(dd.dt)}</b><br>Spikes: ${dd.ct}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc);});}

function rCorr(id,cm){
const nd=$(id);clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);
const feats=cm.features||cm.instruments||[];const mx=cm.matrix||[];if(!feats.length)return;
const sz=Math.min(w-130,h-90);const b={l:115,t:68,w:sz,h:sz};const cell=b.w/feats.length;
feats.forEach((f,i)=>{const tx=at(nd,sl(f),b.l+i*cell+cell/2,60,{"text-anchor":"end","font-weight":"600",fill:"#a0aec0"});
tx.setAttribute("transform",`rotate(-45 ${b.l+i*cell+cell/2} 60)`);
at(nd,sl(f),b.l-6,b.t+i*cell+cell*.65,{"text-anchor":"end","font-weight":"600",fill:"#a0aec0"});});
feats.forEach((f,i)=>feats.forEach((g,j)=>{const v=n(mx[i]?.[j]);const rc=sv("rect",{x:b.l+j*cell,y:b.t+i*cell,width:Math.max(1,cell-1),height:Math.max(1,cell-1),fill:crc(v),rx:1});
rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(f)}</b> vs <b>${e(g)}</b><br>corr: ${d(v,3)}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc);}));}

function rTbl(rows){
const tbl=$("tbl");
const cols=[["instrument","Instrument","t"],["volatility_class","Class","c"],["current_ann_vol","Curr Vol","p"],["mean_ann_vol","Mean Vol","p"],["vol_z","Vol Z","n"],["vol_of_vol","VoV","n"],["spike_frequency","Spike","p"],["composite_vol_score","Score","n"],["acf_abs_lag10","ACF","n"],["downside_upside_ratio","D/U","n"],["quality_score","Qual","n"],["n_bars","Bars","i"],["classification_confidence","Conf","p"]];
const data=sRows(rows).slice(0,500);
const hd=`<thead><tr>${cols.map(([k,l])=>`<th data-key="${k}" class="${tSort===k?"sorted":""}">${l}${tSort===k?(sAsc?" ↑":" ↓"):""}</th>`).join("")}</tr></thead>`;
const bd=`<tbody>${data.map(r=>`<tr data-inst="${e(r.instrument)}" class="${sel.has(r.instrument)?"sel":""}">
${cols.map(([k,,t])=>`<td>${t==="c"?`<span class="pill ${r[k]||"med"}">${CL[r[k]]||"Med"}</span>`:t==="p"?p(r[k]):t==="n"?d(r[k],3):t==="i"?(Number.isFinite(n(r[k]))?Math.round(n(r[k])):"—"):e(r[k])}</td>`).join("")}</tr>`).join("")}</tbody>`;
tbl.innerHTML=hd+bd;
tbl.querySelectorAll("th").forEach(th=>th.addEventListener("click",()=>{const k=th.dataset.key;sAsc=tSort===k?!sAsc:false;tSort=k;renderAll()}));
tbl.querySelectorAll("tbody tr").forEach(tr=>tr.addEventListener("click",()=>togSel(tr.dataset.inst)));}

function rDetail(rows){
const nms=[...sel].filter(nm=>I[nm]);const nm=nms[0]||rows[0]?.instrument;
if(!nm||!I[nm]){$("detailGrid").innerHTML="";$("detailNote").textContent="Click an instrument to inspect.";return;}
const info=I[nm],s=info.summary||{},inf=info.inference||{};
const est=info.volatility?.estimators||{};
const cells=[["Instrument",nm],["Class",CL[s.volatility_class]||"Med"],["Current Vol",p(s.current_ann_vol)],["Mean Vol",p(s.mean_ann_vol)],["History Pctl",p(s.vol_history_percentile)],["Vol Z",d(s.vol_z,2)],["Vol-of-Vol",d(s.vol_of_vol,4)],["Spike Freq",p(s.spike_frequency)],["ARCH-LM p",d(s.arch_lm_p,4)],["ACF |r| L10",d(s.acf_abs_lag10,3)],["Down/Up",d(s.downside_upside_ratio,3)],["Quality",d(s.quality_score,3)],
["CC Est",p(est.close_close?.current_ann)],["Parkinson",p(est.parkinson?.current_ann)],["Garman-Klass",p(est.garman_klass?.current_ann)],["Rogers-Satchell",p(est.rogers_satchell?.current_ann)],["Yang-Zhang",p(est.yang_zhang?.current_ann)],["Confidence",p(s.classification_confidence)]];
$("detailGrid").innerHTML=cells.map(([k,v])=>`<div class="mc"><div class="nm">${e(k)}</div><div class="nv">${e(v)}</div></div>`).join("");
const ci=inf.ann_vol_ci_95_normal_theory||{};
$("detailNote").textContent=`${s.classification_support||"—"} | 95% CI: ${p(ci.lo)} – ${p(ci.hi)} | Bars: ${s.n_bars||"—"} | Freq: ${s.freq||"—"}`;}

function dlCsv(){
const rows=fRows();if(!rows.length)return;
const ks=Object.keys(rows[0]);
const csv=[ks.join(",")].concat(rows.map(r=>ks.map(k=>`"${String(r[k]??"").replace(/"/g,'""')}"`).join(","))).join("\n");
const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));a.download="volatility_export.csv";a.click();}

function renderAll(){
const rows=fRows();
rKpi(rows);rClass(rows);rRank(rows);rHist(rows);rScatter(rows);rBox(rows);
rTermHeat(rows);rFeatHeat(rows);rLine();rSpike(rows);
rCorr("retCorr",CA.return_correlation||{});rCorr("featCorr",D.featureCorrelationMatrix||{});
rTbl(rows);rDetail(rows);
}
setup();renderAll();
window.addEventListener("resize",()=>{clearTimeout(_deb);_deb=setTimeout(()=>renderAll(),200)});

</script>
</body>
</html>"""


def _dashboard_template() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volatility Terminal</title>
<style>
:root{--bg:#080b12;--panel:#101722;--panel2:#0c121b;--line:#243145;--ink:#e6edf7;--muted:#8a98ad;--accent:#19c7a0;--violet:#8b6ff5;--xhigh:#ff3d61;--high:#ff9f43;--med:#697386;--low:#55a7ff;--xlow:#19c7a0;--hover:#182437;--sel:#17362f;font-family:Inter,Segoe UI,Roboto,Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-size:12px}button,input,select{font:inherit}
header{position:sticky;top:0;z-index:20;background:#0b1019;border-bottom:1px solid var(--line);padding:12px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:800}.meta{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted)}.meta span{border:1px solid var(--line);background:#101722;border-radius:6px;padding:5px 8px;white-space:nowrap}
.shell{display:grid;grid-template-columns:286px minmax(0,1fr);gap:12px;padding:12px;max-width:2280px;margin:0 auto}
aside{position:sticky;top:58px;align-self:start;max-height:calc(100vh - 70px);overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.ftitle{font-size:10px;font-weight:800;text-transform:uppercase;color:var(--accent);margin:14px 0 7px}.ftitle:first-child{margin-top:0}
label{display:grid;gap:4px;color:var(--muted);font-size:11px;margin:6px 0}input,select{width:100%;height:31px;background:#090e16;color:var(--ink);border:1px solid #2a3548;border-radius:6px;padding:0 8px;outline:none}input:focus,select:focus{border-color:var(--accent)}
.checks{display:grid;grid-template-columns:1fr 1fr;gap:6px}.checks label{display:flex;align-items:center;gap:6px;margin:0;color:var(--ink);min-width:0}.checks input{width:14px;height:14px;accent-color:var(--accent);padding:0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}.btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
button{height:31px;border:1px solid #2a3548;border-radius:6px;background:#121a27;color:var(--ink);font-weight:700;cursor:pointer}button:hover{background:#182335}button:disabled{opacity:.42;cursor:not-allowed}.primary{background:var(--accent);border-color:var(--accent);color:#04100d}
main{display:grid;gap:12px;min-width:0}.batchbar{position:sticky;top:58px;z-index:15;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;background:#0d131d;border:1px solid var(--line);border-radius:8px;padding:10px 12px}.batch-title{font-weight:800}.batch-sub{color:var(--muted);margin-top:2px}.batch-actions{display:flex;gap:8px;flex-wrap:wrap}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px 12px;min-width:0}.kpi .lb{color:var(--muted);font-size:10px;font-weight:800;text-transform:uppercase}.kpi .vl{margin-top:5px;font-size:20px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}.grid3{display:grid;grid-template-columns:1.1fr 1fr 1fr;gap:12px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;min-width:0;overflow:hidden}.panel-body{padding:12px;min-width:0}
summary{list-style:none;cursor:pointer;padding:11px 12px;border-bottom:1px solid var(--line);font-weight:800;display:flex;justify-content:space-between}summary::-webkit-details-marker{display:none}summary::after{content:"Collapse";color:var(--muted);font-size:10px}details:not([open]) summary{border-bottom:0}details:not([open]) summary::after{content:"Expand"}
.chart{width:100%;height:300px;display:block}.chart.short{height:220px}.chart.tall{height:380px}svg text{font-size:10px;fill:var(--muted);font-family:Inter,Segoe UI,Arial,sans-serif}.ax{stroke:#344154;stroke-width:1;shape-rendering:crispEdges}.gl{stroke:#1d2634;stroke-width:1;shape-rendering:crispEdges}
.legend{display:flex;flex-wrap:wrap;gap:8px;color:var(--muted);font-size:11px;margin-top:8px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.note{color:var(--muted);line-height:1.5;margin:7px 0 0}.table-head{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap}.table-tools{display:flex;align-items:center;gap:8px;color:var(--muted);flex-wrap:wrap}
.table-wrap{border:1px solid var(--line);border-radius:8px;overflow:auto;max-height:540px;background:#0b111a}table{border-collapse:collapse;width:100%;min-width:1420px;font-size:11px;font-variant-numeric:tabular-nums}thead th{position:sticky;top:0;z-index:2;background:#121a27;color:var(--muted);border-bottom:1px solid var(--line);text-align:left;padding:8px;white-space:nowrap;cursor:pointer;text-transform:uppercase;font-size:10px}thead th.sorted{color:var(--accent)}tbody tr:nth-child(odd){background:#0c121b}tbody tr:nth-child(even){background:#101823}tbody tr:hover{background:var(--hover)}tbody tr.active{background:var(--sel)}tbody td{padding:7px 8px;border-bottom:1px solid #162030;white-space:nowrap}
.pill{display:inline-flex;border-radius:5px;padding:2px 7px;font-size:10px;font-weight:800;color:#fff}.pill.extreme_high{background:var(--xhigh)}.pill.high{background:var(--high)}.pill.med{background:var(--med)}.pill.low{background:var(--low)}.pill.extreme_low{background:var(--xlow);color:#04100d}
.detail{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:8px}.mc{border:1px solid var(--line);border-radius:7px;padding:8px;background:#0c121b;min-width:0}.mc .nm{font-size:10px;color:var(--muted);font-weight:800;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.mc .nv{font-size:14px;font-weight:800;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
.tip{position:fixed;z-index:60;pointer-events:none;display:none;max-width:330px;background:#182131;color:var(--ink);border:1px solid #354258;border-radius:8px;padding:8px 10px;box-shadow:0 12px 36px rgba(0,0,0,.45);font-size:11px;line-height:1.45}
@media(max-width:1180px){.shell{grid-template-columns:1fr}aside{position:static;max-height:none}.batchbar{position:static}.grid2,.grid3{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:680px){header{position:static}.shell{padding:8px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.batchbar{grid-template-columns:1fr}.detail{grid-template-columns:repeat(2,minmax(0,1fr))}.chart{height:260px}}
</style>
</head>
<body>
<header><h1>Volatility Intelligence Terminal</h1><div class="meta" id="metaLine"></div></header>
<div class="shell">
<aside>
<div class="ftitle">Search</div><input id="search" type="search" placeholder="Instrument filter">
<div class="ftitle">Volatility Class</div><div class="checks" id="classChecks"></div>
<div class="ftitle">Ranges</div><div class="two"><label>Min Bars<input id="minBars" type="number" min="0" placeholder="0"></label><label>Min Quality<input id="minQuality" type="number" min="0" max="1" step=".05" placeholder="0"></label></div><div class="two"><label>Vol Min<input id="volMin" type="number" min="0" step=".01" placeholder="Any"></label><label>Vol Max<input id="volMax" type="number" min="0" step=".01" placeholder="Any"></label></div><div class="two"><label>Z Min<input id="zMin" type="number" step=".25" placeholder="Any"></label><label>Z Max<input id="zMax" type="number" step=".25" placeholder="Any"></label></div>
<div class="ftitle">Analytics Filters</div><div class="checks"><label><input id="archOnly" type="checkbox"> ARCH p&lt;.05</label><label><input id="spikeOnly" type="checkbox"> Spikes</label><label><input id="jumpOnly" type="checkbox"> Jump vol</label><label><input id="memoryOnly" type="checkbox"> Long memory</label></div>
<div class="ftitle">Controls</div><label>Sort Metric<select id="sortMetric"></select></label><label>Scatter X<select id="scatterX"></select></label><label>Scatter Y<select id="scatterY"></select></label><label>Rows Per Page<select id="pageSize"><option>25</option><option selected>50</option><option>100</option></select></label><div class="btns"><button id="resetBtn">Reset</button><button id="csvBtn" class="primary">Export CSV</button></div>
</aside>
<main>
<section class="batchbar"><div><div class="batch-title" id="batchTitle"></div><div class="batch-sub" id="batchSub"></div></div><div class="batch-actions"><button id="prevBatch">Prev 5</button><button id="nextBatch">Next 5</button></div></section>
<section class="kpis" id="kpis"></section>
<section class="grid2"><details class="panel" open><summary>Ranked Volatility</summary><div class="panel-body"><svg class="chart tall" id="rankChart"></svg><div class="legend" id="classLegend"></div></div></details><details class="panel" open><summary>Cross-Section Breadth</summary><div class="panel-body"><svg class="chart short" id="classChart"></svg><svg class="chart short" id="histChart"></svg></div></details></section>
<section class="grid2"><details class="panel" open><summary>Cross-Section Scatter</summary><div class="panel-body"><svg class="chart" id="scatterChart"></svg><p class="note" id="scatterNote"></p></div></details><details class="panel" open><summary>Class Distribution Box</summary><div class="panel-body"><svg class="chart" id="boxChart"></svg></div></details></section>
<section class="grid3"><details class="panel" open><summary>Term Structure</summary><div class="panel-body"><svg class="chart tall" id="termHeat"></svg></div></details><details class="panel" open><summary>Feature Regime Map</summary><div class="panel-body"><svg class="chart tall" id="featHeat"></svg></div></details><details class="panel" open><summary>Estimator Map</summary><div class="panel-body"><svg class="chart tall" id="estHeat"></svg></div></details></section>
<section class="grid2"><details class="panel" open><summary>Rolling Volatility Overlay</summary><div class="panel-body"><svg class="chart" id="lineChart"></svg></div></details><details class="panel" open><summary>Spike Timeline</summary><div class="panel-body"><svg class="chart" id="spikeChart"></svg></div></details></section>
<section class="grid2"><details class="panel" open><summary>Rolling Vol Correlation</summary><div class="panel-body"><svg class="chart" id="volCorr"></svg><p class="note" id="corrNote"></p></div></details><details class="panel" open><summary>Feature Correlation</summary><div class="panel-body"><svg class="chart" id="featCorr"></svg></div></details></section>
<section class="panel"><div class="panel-body"><div class="table-head"><strong>Searchable Rankings</strong><div class="table-tools"><button id="prevPage">Prev Page</button><span id="pageInfo"></span><button id="nextPage">Next Page</button></div></div><div class="table-wrap"><table id="tbl"></table></div></div></section>
<details class="panel" open><summary>Instrument Drilldown</summary><div class="panel-body"><div class="detail" id="detailGrid"></div><p class="note" id="detailNote"></p></div></details>
</main>
</div>
<div class="tip" id="tip"></div>
<script id="vol-data" type="application/json">__VOLATILITY_DATA__</script>
<script>
const D=JSON.parse(document.getElementById("vol-data").textContent);
const S=(D.summary||[]).map((r,i)=>Object.assign({_row:i},r));
const I=D.instruments||{},FM=D.featureMatrix||[],CA=D.combinedAnalytics||{},R=D.rendering||{};
const TOP_K=R.topK||5,SMALL=R.smallUniverseMax||25,MEDIUM=R.mediumUniverseMax||250;
const CO=["extreme_high","high","med","low","extreme_low"];
const CL={extreme_high:"Extreme High",high:"High",med:"Med",low:"Low",extreme_low:"Extreme Low"};
const CC={extreme_high:"#ff3d61",high:"#ff9f43",med:"#697386",low:"#55a7ff",extreme_low:"#19c7a0"};
const LC=["#19c7a0","#8b6ff5","#ff3d61","#ff9f43","#55a7ff"];
const M=[["current_ann_vol","Current Ann Vol","p"],["mean_ann_vol","Mean Ann Vol","p"],["vol_z","Vol Z","n"],["vol_of_vol","Vol Of Vol","n"],["rolling_vol_stability_cv","Rolling Vol CV","n"],["spike_frequency","Spike Freq","p"],["jump_pct_of_rv","Jump Var Share","p"],["cvar_5_loss_abs","CVaR 5% Loss","p"],["tail_loss_asymmetry","Tail Loss Asym","n"],["conditional_tail_amplification","Tail Amp","n"],["acf_abs_lag10","ACF |r| L10","n"],["regime_high_persistence","High Regime Persist","p"],["hurst_abs_returns","Hurst |r|","n"],["permutation_entropy","Perm Entropy","n"],["sample_entropy","Sample Entropy","n"],["range_vol_ratio","Range/AbsRet","n"],["downside_upside_ratio","Down/Up Vol","n"],["composite_vol_score","Composite Score","n"],["classification_confidence","Confidence","p"],["quality_score","Quality","n"],["n_bars","Bars","i"]];
const W=Object.keys(S[0]||{}).filter(k=>/^term_\d+_current_ann$/.test(k)).map(k=>k.match(/\d+/)[0]);
const FMAP=new Map(FM.map(f=>[f.instrument+"|"+f.feature,f]));
const FEATURE_ORDER=M.map(m=>m[0]).filter(k=>FM.some(f=>f.feature===k)).slice(0,16);
const SPIKES=new Map(Object.entries(I).map(([nm,info])=>[nm,(info.spike_dates||[]).map(x=>String(x).slice(0,10)).filter(Boolean)]));
let state={sort:"current_ann_vol",asc:false,batch:0,page:0,detail:null},deb=null,resizeDeb=null;
function $(id){return document.getElementById(id)}
function n(v,f=NaN){if(v===null||v===undefined||v==="")return f;const x=Number(v);return Number.isFinite(x)?x:f}
function p(v){const x=n(v);return Number.isFinite(x)?(x*100).toFixed(2)+"%":"-"}
function d(v,dp=3){const x=n(v);return Number.isFinite(x)?x.toFixed(dp):"-"}
function fmt(v,t){if(t==="p")return p(v);if(t==="i")return Number.isFinite(n(v))?String(Math.round(n(v))):"-";if(t==="b")return v===true?"Yes":v===false?"No":"-";if(t==="n")return d(v,3);return e(v)}
function e(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]))}
function sv(tag,a={}){const nd=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const[k,v]of Object.entries(a))nd.setAttribute(k,v);return nd}
function clr(nd){while(nd.firstChild)nd.removeChild(nd.firstChild)}
function dm(nd){const r=nd.getBoundingClientRect();return{w:Math.max(320,r.width||620),h:Math.max(210,r.height||300)}}
function ext(vs){const xs=vs.map(Number).filter(Number.isFinite);if(!xs.length)return[0,1];let a=Math.min(...xs),b=Math.max(...xs);if(a===b){const pad=Math.abs(a)||1;a-=pad*.05;b+=pad*.05}return[a,b]}
function sc(dom,rng){const[d0,d1]=dom,[r0,r1]=rng,dn=d1-d0||1;return x=>r0+((x-d0)/dn)*(r1-r0)}
function at(g,t,x,y,a={}){const nd=sv("text",Object.assign({x:x,y:y},a));nd.textContent=t;g.appendChild(nd);return nd}
function fa(x){if(!Number.isFinite(x))return"-";if(Math.abs(x)>=10)return x.toFixed(0);if(Math.abs(x)>=1)return x.toFixed(2);return x.toFixed(3)}
function hx(h){const x=h.replace("#","");return[parseInt(x.slice(0,2),16),parseInt(x.slice(2,4),16),parseInt(x.slice(4,6),16)]}
function bl(a,b,t){const ca=hx(a),cb=hx(b),u=Math.max(0,Math.min(1,t));const c=ca.map((x,i)=>Math.round(x+(cb[i]-x)*u));return`rgb(${c[0]},${c[1]},${c[2]})`}
function crc(v){const x=n(v);if(!Number.isFinite(x))return"#101722";return x>=0?bl("#101722","#ff3d61",Math.abs(x)):bl("#101722","#55a7ff",Math.abs(x))}
function sl(s,m=15){return String(s||"").replace(/current_|ann_|returns?|frequency|volatility/g,"").replace(/_/g," ").trim().slice(0,m)}
function tip(ev,h){const t=$("tip");t.innerHTML=h;t.style.display="block";t.style.left=Math.min(window.innerWidth-350,ev.clientX+12)+"px";t.style.top=Math.min(window.innerHeight-130,ev.clientY+12)+"px"}
function ht(){$("tip").style.display="none"}
function axes(g,xs,ys,xd,yd,b,xl,yl,yPct=false){g.appendChild(sv("line",{x1:b.l,y1:b.t+b.h,x2:b.l+b.w,y2:b.t+b.h,class:"ax"}));g.appendChild(sv("line",{x1:b.l,y1:b.t,x2:b.l,y2:b.t+b.h,class:"ax"}));for(let i=0;i<=4;i++){const tx=xd[0]+(xd[1]-xd[0])*i/4,x=xs(tx),ty=yd[0]+(yd[1]-yd[0])*i/4,y=ys(ty);g.appendChild(sv("line",{x1:x,y1:b.t,x2:x,y2:b.t+b.h,class:"gl"}));g.appendChild(sv("line",{x1:b.l,y1:y,x2:b.l+b.w,y2:y,class:"gl"}));at(g,fa(tx),x,b.t+b.h+16,{"text-anchor":"middle"});at(g,yPct?p(ty):fa(ty),b.l-7,y+4,{"text-anchor":"end"})}at(g,xl,b.l+b.w/2,b.t+b.h+34,{"text-anchor":"middle","font-weight":"800"});const yt=at(g,yl,13,b.t+b.h/2,{"text-anchor":"middle","font-weight":"800"});yt.setAttribute("transform",`rotate(-90 13 ${b.t+b.h/2})`)}
function metricLabel(k){return (M.find(m=>m[0]===k)||[k,k])[1]}function metricType(k){return (M.find(m=>m[0]===k)||[k,k,"n"])[2]}
function scaleMode(rows){if(rows.length<=SMALL)return"full";if(rows.length<=MEDIUM)return"grouped";return"batched"}
function classes(){return new Set([...document.querySelectorAll("#classChecks input:checked")].map(x=>x.dataset.class))}
function filteredRows(){const q=$("search").value.trim().toLowerCase(),cl=classes(),mb=n($("minBars").value,0),mq=n($("minQuality").value,0),v0=n($("volMin").value,-Infinity),v1=n($("volMax").value,Infinity),z0=n($("zMin").value,-Infinity),z1=n($("zMax").value,Infinity),arch=$("archOnly").checked,spike=$("spikeOnly").checked,jump=$("jumpOnly").checked,mem=$("memoryOnly").checked;return S.filter(r=>{if(q&&!String(r.instrument).toLowerCase().includes(q))return false;if(!cl.has(r.volatility_class||"med"))return false;if(n(r.n_bars,0)<mb||n(r.quality_score,0)<mq)return false;if(n(r.current_ann_vol)<v0||n(r.current_ann_vol)>v1)return false;if(n(r.vol_z)<z0||n(r.vol_z)>z1)return false;if(arch&&!(n(r.arch_lm_p,1)<.05))return false;if(spike&&!(n(r.n_spikes,0)>0))return false;if(jump&&!(n(r.jump_pct_of_rv,0)>0))return false;if(mem&&r.long_memory_flag!==true)return false;return true})}
function sortedRows(rows){const k=state.sort;if(k==="instrument")return rows.slice().sort((a,b)=>state.asc?String(a.instrument).localeCompare(String(b.instrument)):String(b.instrument).localeCompare(String(a.instrument)));return rows.slice().sort((a,b)=>state.asc?n(a[k],-Infinity)-n(b[k],-Infinity):n(b[k],-Infinity)-n(a[k],-Infinity))}
function pageSize(){return Math.max(10,n($("pageSize").value,50))}
function batchRows(rows){const sr=sortedRows(rows);if(rows.length<=SMALL)return sr;return sr.slice(state.batch*TOP_K,state.batch*TOP_K+TOP_K)}
function clamp(rows){const bc=Math.max(1,Math.ceil(sortedRows(rows).length/TOP_K));state.batch=Math.max(0,Math.min(state.batch,bc-1));const pc=Math.max(1,Math.ceil(rows.length/pageSize()));state.page=Math.max(0,Math.min(state.page,pc-1));if(!state.detail||!rows.some(r=>r.instrument===state.detail))state.detail=(batchRows(rows)[0]||rows[0]||{}).instrument||null}
function resetBatch(){state.batch=0;state.page=0;renderAll()}function debRender(){clearTimeout(deb);deb=setTimeout(resetBatch,110)}function setDetail(nm){state.detail=nm;renderAll()}
function setup(){$("metaLine").innerHTML=[`Range ${e(D.meta?.start_date||"")} to ${e(D.meta?.end_date||"")}`,`Res ${e(D.meta?.resolution||"")}m`,`Analysed ${S.length}`,`Errors ${D.meta?.n_errors||0}`].map(x=>`<span>${x}</span>`).join("");$("classChecks").innerHTML=CO.map(c=>`<label><input type="checkbox" data-class="${c}" checked><span class="sw" style="background:${CC[c]}"></span>${CL[c]}</label>`).join("");const opts=M.map(([k,l])=>`<option value="${k}">${l}</option>`).join("");$("sortMetric").innerHTML=opts;$("scatterX").innerHTML=opts;$("scatterY").innerHTML=opts;$("sortMetric").value=state.sort;$("scatterX").value="current_ann_vol";$("scatterY").value="vol_of_vol";$("classLegend").innerHTML=CO.map(c=>`<span><span class="sw" style="background:${CC[c]}"></span>${CL[c]}</span>`).join("");document.querySelectorAll("aside input,aside select").forEach(nd=>nd.addEventListener("input",()=>{if(nd.id==="sortMetric")state.sort=nd.value;state.batch=0;state.page=0;renderAll()}));$("prevBatch").addEventListener("click",()=>{state.batch--;renderAll()});$("nextBatch").addEventListener("click",()=>{state.batch++;renderAll()});$("prevPage").addEventListener("click",()=>{state.page--;renderAll()});$("nextPage").addEventListener("click",()=>{state.page++;renderAll()});$("resetBtn").addEventListener("click",()=>{["search","volMin","volMax","zMin","zMax","minBars","minQuality"].forEach(id=>$(id).value="");["archOnly","spikeOnly","jumpOnly","memoryOnly"].forEach(id=>$(id).checked=false);document.querySelectorAll("#classChecks input").forEach(x=>x.checked=true);$("sortMetric").value="current_ann_vol";$("scatterX").value="current_ann_vol";$("scatterY").value="vol_of_vol";state={sort:"current_ann_vol",asc:false,batch:0,page:0,detail:null};renderAll()});$("csvBtn").addEventListener("click",downloadCsv)}
function renderBatch(rows,batch,mode){const sr=sortedRows(rows),bc=Math.max(1,Math.ceil(sr.length/TOP_K)),start=rows.length<=SMALL?1:state.batch*TOP_K+1,end=rows.length<=SMALL?sr.length:Math.min(sr.length,state.batch*TOP_K+TOP_K);$("batchTitle").textContent=`${mode.toUpperCase()} rendering - ${rows.length<=SMALL?"all instruments":`batch ${state.batch+1} of ${bc}`}`;$("batchSub").textContent=rows.length?`Showing ${start}-${end} of ${sr.length} after filters, sorted by ${metricLabel(state.sort)}. Heavy charts render at TOP_K=${TOP_K}.`:"No rows match filters.";$("prevBatch").disabled=rows.length<=SMALL||state.batch<=0;$("nextBatch").disabled=rows.length<=SMALL||state.batch>=bc-1}
function renderKpis(rows,mode){const med=a=>{const x=a.filter(Number.isFinite).sort((a,b)=>a-b);return x.length?x[Math.floor(x.length/2)]:NaN},high=rows.filter(r=>["high","extreme_high"].includes(r.volatility_class)).length;const vals=[["Filtered",rows.length],["Mode",mode],["High Pressure",p(high/Math.max(rows.length,1))],["Median Vol",p(med(rows.map(r=>n(r.current_ann_vol))))],["Median CVaR",p(med(rows.map(r=>n(r.cvar_5_loss_abs))))],["Jump Share",p(med(rows.map(r=>n(r.jump_pct_of_rv))))],["Long Memory",p(rows.filter(r=>r.long_memory_flag===true).length/Math.max(rows.length,1))],["Batch Size",rows.length<=SMALL?rows.length:TOP_K]];$("kpis").innerHTML=vals.map(([k,v])=>`<div class="kpi"><div class="lb">${e(k)}</div><div class="vl">${e(v)}</div></div>`).join("")}
function renderClass(rows){const nd=$("classChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const cts=CO.map(c=>rows.filter(r=>(r.volatility_class||"med")===c).length),mx=Math.max(1,...cts),b={l:112,t:12,w:w-142,h:h-32},rh=b.h/CO.length;CO.forEach((c,i)=>{const y=b.t+i*rh+3,bw=b.w*cts[i]/mx;at(nd,CL[c],b.l-8,y+rh*.6,{"text-anchor":"end"});const rc=sv("rect",{x:b.l,y,width:Math.max(2,bw),height:Math.max(8,rh-7),fill:CC[c],rx:3,opacity:.9});rc.addEventListener("mousemove",ev=>tip(ev,`<b>${CL[c]}</b><br>${cts[i]} instruments`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc);at(nd,String(cts[i]),b.l+bw+6,y+rh*.6,{"font-weight":"800",fill:"#e6edf7"})})}
function renderHist(rows){const nd=$("histChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const vs=rows.map(r=>n(r.current_ann_vol)).filter(Number.isFinite);if(!vs.length)return;const[lo,hi]=ext(vs),bins=18,cts=Array(bins).fill(0);vs.forEach(v=>cts[Math.min(bins-1,Math.max(0,Math.floor((v-lo)/((hi-lo)||1)*bins)))]++);const b={l:46,t:12,w:w-62,h:h-48},x=sc([lo,hi],[b.l,b.l+b.w]),y=sc([0,Math.max(1,...cts)],[b.t+b.h,b.t]);axes(nd,x,y,[lo,hi],[0,Math.max(1,...cts)],b,"Current Ann Vol","Count");cts.forEach((c,i)=>{const x0=x(lo+(hi-lo)*i/bins),x1=x(lo+(hi-lo)*(i+1)/bins);nd.appendChild(sv("rect",{x:x0+1,y:y(c),width:Math.max(1,x1-x0-2),height:b.t+b.h-y(c),fill:"#19c7a0",opacity:.58,rx:2}))})}
function renderRank(rows,batch){const nd=$("rankChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const data=batch.length?batch:rows.slice(0,TOP_K),k=state.sort,type=metricType(k),mx=Math.max(1e-12,...data.map(r=>Math.abs(n(r[k],0)))),b={l:142,t:12,w:w-176,h:h-38},rh=b.h/Math.max(1,data.length);data.forEach((r,i)=>{const y=b.t+i*rh+2,v=n(r[k],0),bw=b.w*Math.abs(v)/mx,c=r.volatility_class||"med";at(nd,r.instrument,b.l-7,y+rh*.62,{"text-anchor":"end",fill:"#e6edf7"});const rc=sv("rect",{x:b.l,y,width:Math.max(2,bw),height:Math.max(8,rh-5),fill:CC[c],rx:3,opacity:.92});rc.style.cursor="pointer";rc.addEventListener("click",()=>setDetail(r.instrument));rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${e(metricLabel(k))}: ${fmt(v,type)}<br>${CL[c]||"Med"}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc);at(nd,fmt(v,type),b.l+bw+6,y+rh*.62,{"font-weight":"800",fill:"#e6edf7"})})}
function renderScatter(rows,batch,mode){const nd=$("scatterChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const xk=$("scatterX").value,yk=$("scatterY").value;let data=mode==="batched"?batch:rows;if(mode==="grouped")data=sortedRows(rows).slice(0,Math.min(160,rows.length));data=data.filter(r=>Number.isFinite(n(r[xk]))&&Number.isFinite(n(r[yk])));if(!data.length)return;const xd=ext(data.map(r=>n(r[xk]))),yd=ext(data.map(r=>n(r[yk]))),b={l:58,t:18,w:w-80,h:h-62},x=sc(xd,[b.l,b.l+b.w]),y=sc(yd,[b.t+b.h,b.t]);axes(nd,x,y,xd,yd,b,metricLabel(xk),metricLabel(yk),metricType(yk)==="p");const dense=data.length>80;data.forEach(r=>{const c=r.volatility_class||"med",active=r.instrument===state.detail;const ci=sv("circle",{cx:x(n(r[xk])),cy:y(n(r[yk])),r:active?7:(dense?3:4.5),fill:CC[c],opacity:active?1:(dense?.52:.82),stroke:active?"#fff":"none","stroke-width":active?2:0});ci.style.cursor="pointer";ci.addEventListener("click",()=>setDetail(r.instrument));ci.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${e(metricLabel(xk))}: ${fmt(r[xk],metricType(xk))}<br>${e(metricLabel(yk))}: ${fmt(r[yk],metricType(yk))}`));ci.addEventListener("mouseleave",ht);nd.appendChild(ci)});$("scatterNote").textContent=mode==="full"?"Full scatter universe rendered.":mode==="grouped"?`Density-aware grouped mode: ${data.length} ranked instruments shown from ${rows.length} filtered.`:`Batched mode: ${data.length} instruments shown for this TOP_K window.`}
function renderBox(rows){const nd=$("boxChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const gs=CO.map(c=>rows.filter(r=>(r.volatility_class||"med")===c).map(r=>n(r.current_ann_vol)).filter(Number.isFinite)),all=gs.flat();if(!all.length)return;const yd=ext(all),b={l:58,t:18,w:w-82,h:h-66},y=sc(yd,[b.t+b.h,b.t]),step=b.w/CO.length;for(let i=0;i<=4;i++){const ty=yd[0]+(yd[1]-yd[0])*i/4,yp=y(ty);nd.appendChild(sv("line",{x1:b.l,y1:yp,x2:b.l+b.w,y2:yp,class:"gl"}));at(nd,p(ty),b.l-7,yp+4,{"text-anchor":"end"})}gs.forEach((vs,i)=>{if(!vs.length)return;vs.sort((a,b)=>a-b);const q=t=>vs[Math.min(vs.length-1,Math.max(0,Math.floor((vs.length-1)*t)))],cx=b.l+step*i+step/2,q1=q(.25),md=q(.5),q3=q(.75),lo=q(.05),hi=q(.95);nd.appendChild(sv("line",{x1:cx,y1:y(lo),x2:cx,y2:y(hi),stroke:CC[CO[i]],"stroke-width":2}));nd.appendChild(sv("rect",{x:cx-step*.24,y:y(q3),width:step*.48,height:Math.max(2,y(q1)-y(q3)),fill:CC[CO[i]],opacity:.55,rx:3}));nd.appendChild(sv("line",{x1:cx-step*.24,y1:y(md),x2:cx+step*.24,y2:y(md),stroke:"#fff","stroke-width":2}));at(nd,CL[CO[i]],cx,b.t+b.h+24,{"text-anchor":"middle"})})}
function heatColor(v,lo,hi){if(!Number.isFinite(v))return"#101722";return bl("#111722","#ff3d61",(v-lo)/((hi-lo)||1))}
function renderTerm(batch){const nd=$("termHeat");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);if(!batch.length||!W.length){at(nd,"No term structure data",18,30,{"font-weight":"800"});return}const b={l:130,t:30,w:w-150,h:h-50},cw=b.w/W.length,ch=Math.max(18,b.h/batch.length),vals=batch.flatMap(r=>W.map(wi=>n(r[`term_${wi}_current_ann`]))).filter(Number.isFinite),[lo,hi]=ext(vals);W.forEach((wi,j)=>at(nd,wi,b.l+j*cw+cw/2,20,{"text-anchor":"middle","font-weight":"800",fill:"#e6edf7"}));batch.forEach((r,i)=>{const y=b.t+i*ch;at(nd,r.instrument,b.l-7,y+ch*.62,{"text-anchor":"end",fill:"#e6edf7"});W.forEach((wi,j)=>{const v=n(r[`term_${wi}_current_ann`]);const rc=sv("rect",{x:b.l+j*cw,y:y,width:Math.max(2,cw-2),height:Math.max(12,ch-3),fill:heatColor(v,lo,hi),rx:2});rc.style.cursor="pointer";rc.addEventListener("click",()=>setDetail(r.instrument));rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${wi}-bar ann vol: ${p(v)}<br>VoV: ${d(r[`term_${wi}_vol_of_vol`],3)}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc)})})}
function renderFeature(batch){const nd=$("featHeat");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const feats=FEATURE_ORDER;if(!batch.length||!feats.length)return;const b={l:130,t:78,w:w-150,h:h-96},cw=b.w/feats.length,ch=Math.max(18,b.h/batch.length);feats.forEach((f,j)=>{const tx=at(nd,sl(metricLabel(f),13),b.l+j*cw+cw/2,70,{"text-anchor":"end","font-weight":"800"});tx.setAttribute("transform",`rotate(-45 ${b.l+j*cw+cw/2} 70)`)});batch.forEach((r,i)=>{const y=b.t+i*ch;at(nd,r.instrument,b.l-7,y+ch*.62,{"text-anchor":"end",fill:"#e6edf7"});feats.forEach((f,j)=>{const it=FMAP.get(r.instrument+"|"+f),c=it?.class||"med";const rc=sv("rect",{x:b.l+j*cw,y:y,width:Math.max(2,cw-2),height:Math.max(12,ch-3),fill:CC[c],opacity:.86,rx:2});rc.style.cursor="pointer";rc.addEventListener("click",()=>setDetail(r.instrument));rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${e(metricLabel(f))}<br>${CL[c]||"Med"}<br>value ${d(it?.value,4)} | pctl ${d(it?.percentile,2)}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc)})})}
function renderEstimator(batch){const nd=$("estHeat");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const cols=["close_close","parkinson","garman_klass","rogers_satchell","yang_zhang"];if(!batch.length)return;const b={l:130,t:62,w:w-150,h:h-82},cw=b.w/cols.length,ch=Math.max(18,b.h/batch.length),vals=[];batch.forEach(r=>{const est=I[r.instrument]?.volatility?.estimators||{};cols.forEach(c=>vals.push(n(est[c]?.current_ann))) });const[lo,hi]=ext(vals);cols.forEach((c,j)=>{const tx=at(nd,sl(c,12),b.l+j*cw+cw/2,54,{"text-anchor":"end","font-weight":"800"});tx.setAttribute("transform",`rotate(-45 ${b.l+j*cw+cw/2} 54)`)});batch.forEach((r,i)=>{const est=I[r.instrument]?.volatility?.estimators||{},y=b.t+i*ch;at(nd,r.instrument,b.l-7,y+ch*.62,{"text-anchor":"end",fill:"#e6edf7"});cols.forEach((c,j)=>{const v=n(est[c]?.current_ann);const rc=sv("rect",{x:b.l+j*cw,y:y,width:Math.max(2,cw-2),height:Math.max(12,ch-3),fill:heatColor(v,lo,hi),rx:2});rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(r.instrument)}</b><br>${e(c)}: ${p(v)}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc)})})}
function renderLine(batch){const nd=$("lineChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const series=batch.map(r=>{const ts=I[r.instrument]?.timeseries?.rolling_vol||{};return{nm:r.instrument,vs:(ts.values||[]).map(n)}}).filter(s=>s.vs.length);if(!series.length){at(nd,"No rolling volatility series for this batch",18,30,{"font-weight":"800"});return}const all=series.flatMap(s=>s.vs).filter(Number.isFinite),ml=Math.max(...series.map(s=>s.vs.length)),xd=[0,Math.max(1,ml-1)],yd=ext(all),b={l:58,t:18,w:w-82,h:h-62},x=sc(xd,[b.l,b.l+b.w]),y=sc(yd,[b.t+b.h,b.t]);axes(nd,x,y,xd,yd,b,"Time","Ann Vol",true);series.forEach((s,i)=>{const pts=s.vs.map((v,j)=>Number.isFinite(v)?`${j?"L":"M"}${x(j)},${y(v)}`:"").filter(Boolean).join(" ");if(pts)nd.appendChild(sv("path",{d:pts,fill:"none",stroke:LC[i%LC.length],"stroke-width":2,opacity:.9}));at(nd,s.nm,b.l+8,b.t+14+i*15,{fill:LC[i%LC.length],"font-weight":"800"})})}
function renderSpike(batch){const nd=$("spikeChart");clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);const counts=new Map();batch.forEach(r=>(SPIKES.get(r.instrument)||[]).forEach(dy=>counts.set(dy,(counts.get(dy)||0)+1)));const data=[...counts.entries()].sort((a,b)=>a[0].localeCompare(b[0])).map(([dt,ct])=>({dt,ct}));if(!data.length){at(nd,"No spikes in current visualization batch",18,30,{"font-weight":"800"});return}const b={l:46,t:18,w:w-64,h:h-58},mx=Math.max(1,...data.map(d=>d.ct)),y=sc([0,mx],[b.t+b.h,b.t]),bw=Math.max(2,b.w/data.length);for(let i=0;i<=4;i++){const ty=mx*i/4,yp=y(ty);nd.appendChild(sv("line",{x1:b.l,y1:yp,x2:b.l+b.w,y2:yp,class:"gl"}));at(nd,fa(ty),b.l-7,yp+4,{"text-anchor":"end"})}data.forEach((d,i)=>{const rc=sv("rect",{x:b.l+i*bw,y:y(d.ct),width:Math.max(1,bw-1),height:b.t+b.h-y(d.ct),fill:"#ff9f43",opacity:.72,rx:1});rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(d.dt)}</b><br>spikes: ${d.ct}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc)})}
function renderCorrMatrix(id,cm,batch,subsetByBatch){const nd=$(id);clr(nd);const{w,h}=dm(nd);nd.setAttribute("viewBox",`0 0 ${w} ${h}`);let labels=cm.features||cm.instruments||[],matrix=cm.matrix||[];if(subsetByBatch&&cm.instruments){const wanted=batch.map(r=>r.instrument),idx=wanted.map(nm=>labels.indexOf(nm)).filter(i=>i>=0);labels=idx.map(i=>cm.instruments[i]);matrix=idx.map(i=>idx.map(j=>cm.matrix?.[i]?.[j]))}$("corrNote").textContent=CA.correlation_policy?.capped?`Correlation universe capped at ${CA.correlation_policy.correlation_universe_cap} highest-vol instruments for memory control.`:"";if(!labels.length){at(nd,"No correlation data for this batch",18,30,{"font-weight":"800"});return}const sz=Math.min(w-130,h-88),b={l:116,t:66,w:sz,h:sz},cell=b.w/labels.length;labels.forEach((f,i)=>{const tx=at(nd,sl(f,14),b.l+i*cell+cell/2,58,{"text-anchor":"end","font-weight":"800"});tx.setAttribute("transform",`rotate(-45 ${b.l+i*cell+cell/2} 58)`);at(nd,sl(f,15),b.l-7,b.t+i*cell+cell*.66,{"text-anchor":"end","font-weight":"800"})});labels.forEach((f,i)=>labels.forEach((g,j)=>{const v=n(matrix[i]?.[j]);const rc=sv("rect",{x:b.l+j*cell,y:b.t+i*cell,width:Math.max(2,cell-1),height:Math.max(2,cell-1),fill:crc(v),rx:1});rc.addEventListener("mousemove",ev=>tip(ev,`<b>${e(f)}</b> vs <b>${e(g)}</b><br>corr ${d(v,3)}`));rc.addEventListener("mouseleave",ht);nd.appendChild(rc)}))}
function renderTable(rows){const tbl=$("tbl"),cols=[["instrument","Instrument","t"],["volatility_class","Class","c"],["current_ann_vol","Curr Vol","p"],["vol_z","Z","n"],["vol_of_vol","VoV","n"],["rolling_vol_stability_cv","Roll CV","n"],["spike_frequency","Spike","p"],["jump_pct_of_rv","Jump","p"],["cvar_5_loss_abs","CVaR","p"],["tail_loss_asymmetry","Tail Asym","n"],["regime_high_persistence","Regime P","p"],["hurst_abs_returns","H |r|","n"],["permutation_entropy","PE","n"],["composite_vol_score","Score","n"],["quality_score","Qual","n"],["n_bars","Bars","i"]];const sr=sortedRows(rows),ps=pageSize(),pages=Math.max(1,Math.ceil(sr.length/ps)),start=state.page*ps,data=sr.slice(start,start+ps);$("pageInfo").textContent=`${sr.length?start+1:0}-${Math.min(sr.length,start+ps)} of ${sr.length}`;$("prevPage").disabled=state.page<=0;$("nextPage").disabled=state.page>=pages-1;const head=`<thead><tr>${cols.map(([k,l])=>`<th data-k="${k}" class="${state.sort===k?"sorted":""}">${e(l)}${state.sort===k?(state.asc?" up":" down"):""}</th>`).join("")}</tr></thead>`;const body=`<tbody>${data.map(r=>`<tr data-inst="${e(r.instrument)}" class="${r.instrument===state.detail?"active":""}">${cols.map(([k,,t])=>`<td>${t==="c"?`<span class="pill ${r[k]||"med"}">${CL[r[k]]||"Med"}</span>`:fmt(r[k],t)}</td>`).join("")}</tr>`).join("")}</tbody>`;tbl.innerHTML=head+body;tbl.querySelectorAll("th").forEach(th=>th.addEventListener("click",()=>{const k=th.dataset.k;state.asc=state.sort===k?!state.asc:false;state.sort=k;if(M.some(m=>m[0]===k))$("sortMetric").value=k;state.batch=0;renderAll()}));tbl.querySelectorAll("tbody tr").forEach(tr=>tr.addEventListener("click",()=>setDetail(tr.dataset.inst)))}
function renderDetail(rows,batch){const nm=state.detail||batch[0]?.instrument||rows[0]?.instrument;if(!nm||!I[nm]){$("detailGrid").innerHTML="";$("detailNote").textContent="No instrument selected.";return}const info=I[nm],s=info.summary||{},est=info.volatility?.estimators||{},inf=info.inference||{},reg=info.features?.regimes||{},tm=reg.transition_matrix||{},ci=inf.ann_vol_ci_95_normal_theory||{};const cells=[["Instrument",nm],["Class",CL[s.volatility_class]||"Med"],["Current Vol",p(s.current_ann_vol)],["Mean Vol",p(s.mean_ann_vol)],["Composite",d(s.composite_vol_score,3)],["Confidence",p(s.classification_confidence)],["Vol Z",d(s.vol_z,2)],["VoV",d(s.vol_of_vol,3)],["CVaR 5%",p(s.cvar_5_loss_abs)],["Jump RV",p(s.jump_pct_of_rv)],["Jump Vol",p(s.jump_vol_ann)],["Tail Amp",d(s.conditional_tail_amplification,2)],["ARCH p",d(s.arch_lm_p,4)],["ACF |r| L10",d(s.acf_abs_lag10,3)],["Hurst |r|",d(s.hurst_abs_returns,3)],["Perm Entropy",d(s.permutation_entropy,3)],["Roll Vol CV",d(s.rolling_vol_stability_cv,3)],["High Persist",p(tm.high_to_high)],["Regime",s.current_binary_regime||"-"],["Quality",d(s.quality_score,3)],["Close-Close",p(est.close_close?.current_ann)],["Parkinson",p(est.parkinson?.current_ann)],["Garman-Klass",p(est.garman_klass?.current_ann)],["Yang-Zhang",p(est.yang_zhang?.current_ann)]];$("detailGrid").innerHTML=cells.map(([k,v])=>`<div class="mc"><div class="nm">${e(k)}</div><div class="nv">${e(v)}</div></div>`).join("");$("detailNote").textContent=`${s.classification_support||"-"} | 95% ann-vol CI ${p(ci.lo)} to ${p(ci.hi)} | Bars ${s.n_bars||"-"} | ${s.start||""} to ${s.end||""}`}
function downloadCsv(){const rows=filteredRows();if(!rows.length)return;const keys=Object.keys(rows[0]).filter(k=>!k.startsWith("_"));const csv=[keys.join(",")].concat(rows.map(r=>keys.map(k=>`"${String(r[k]??"").replace(/"/g,'""')}"`).join(","))).join("\n");const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));a.download="volatility_export.csv";a.click()}
function renderAll(){const rows=filteredRows();clamp(rows);const batch=batchRows(rows),mode=scaleMode(rows);renderBatch(rows,batch,mode);renderKpis(rows,mode);renderClass(rows);renderHist(rows);renderRank(rows,batch);renderScatter(rows,batch,mode);renderBox(rows);renderTerm(batch);renderFeature(batch);renderEstimator(batch);renderLine(batch);renderSpike(batch);renderCorrMatrix("volCorr",CA.vol_correlation||{},batch,true);renderCorrMatrix("featCorr",D.featureCorrelationMatrix||{},batch,false);renderTable(rows);renderDetail(rows,batch)}
setup();renderAll();window.addEventListener("resize",()=>{clearTimeout(resizeDeb);resizeDeb=setTimeout(renderAll,160)});
</script>
</body>
</html>"""


def save_outputs(report: Dict[str, Any], cfg: VolatilityConfig) -> None:
    serialised = _serialise(report)
    Path(cfg.output_json).write_text(json.dumps(serialised, indent=2), encoding="utf-8")
    print(f"[INFO] JSON report saved to {cfg.output_json}")
    if report.get("summary"):
        pd.DataFrame(report["summary"]).to_csv(cfg.output_csv, index=False)
        print(f"[INFO] Summary CSV saved to {cfg.output_csv}")
    build_dashboard(report, cfg.output_html)


def parse_instruments(values: Optional[List[str]], file_path: Optional[str]) -> Optional[List[str]]:
    instruments: List[str] = []
    if values:
        for value in values:
            instruments.extend([x.strip() for x in value.split(",") if x.strip()])
    if file_path:
        for line in Path(file_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                instruments.append(line)
    return sorted(set(instruments)) if instruments else None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cross-sectional volatility analytics for active CoinDCX USDT futures."
    )
    parser.add_argument("--start-date", help="UTC start date YYYY-MM-DD. Default: last 90 days.")
    parser.add_argument("--end-date", help="UTC end date YYYY-MM-DD. Default: now.")
    parser.add_argument("--resolution", default="5", help="CoinDCX candle resolution in minutes. Default: 5.")
    parser.add_argument("--margin-currency", default="USDT", help="Active instrument margin currency. Default: USDT.")
    parser.add_argument("--instrument", action="append", help="Instrument or comma-separated instruments. Can repeat.")
    parser.add_argument("--instrument-file", help="Text file with one instrument per line.")
    parser.add_argument("--include-regex", help="Only instruments matching this regex.")
    parser.add_argument("--exclude-regex", help="Exclude instruments matching this regex.")
    parser.add_argument("--max-instruments", type=int, help="Limit number of instruments for quick runs.")
    parser.add_argument("--min-bars", type=int, default=80, help="Minimum bars expected for classification confidence.")
    parser.add_argument("--cache-dir", default="volatility_cache", help="Raw candle cache directory.")
    parser.add_argument("--refresh-cache", action="store_true", help="Ignore cached candle files and refetch.")
    parser.add_argument("--no-cache", action="store_true", help="Do not read/write raw candle cache.")
    parser.add_argument("--sleep", type=float, default=0.25, help="Sleep seconds between candle requests.")
    parser.add_argument("--output-json", default="volatility_report.json")
    parser.add_argument("--output-csv", default="volatility_summary.csv")
    parser.add_argument("--output-html", default="volatility_dashboard.html")
    parser.add_argument("--demo", action="store_true", help="Run synthetic offline demo data instead of API calls.")
    return parser


def print_console_summary(report: Dict[str, Any]) -> None:
    rows = report.get("summary", [])
    if not rows:
        print("[WARN] No instruments were analysed successfully.")
        return
    df = pd.DataFrame(rows)
    counts = df["volatility_class"].value_counts().to_dict()
    print("\nVOLATILITY CLASSIFICATION")
    for cls in VOL_CLASS_ORDER:
        print(f"  {cls:13s}: {int(counts.get(cls, 0)):4d}")

    top = (
        df.sort_values("current_ann_vol", ascending=False)
        .head(12)[
            [
                "instrument",
                "volatility_class",
                "current_ann_vol",
                "mean_ann_vol",
                "vol_of_vol",
                "spike_frequency",
                "vol_z",
                "classification_confidence_label",
            ]
        ]
        .copy()
    )
    for col in ["current_ann_vol", "mean_ann_vol", "spike_frequency"]:
        top[col] = top[col].map(lambda x: f"{x:.2%}" if pd.notna(x) else "NA")
    for col in ["vol_of_vol", "vol_z"]:
        top[col] = top[col].map(lambda x: f"{x:.3f}" if pd.notna(x) else "NA")
    print("\nTOP CURRENT VOLATILITY")
    print(top.to_string(index=False))


def main() -> Dict[str, Any]:
    parser = build_arg_parser()
    args = parser.parse_args()
    cfg = VolatilityConfig(
        margin_currency=args.margin_currency,
        resolution=args.resolution,
        start_date=args.start_date,
        end_date=args.end_date,
        min_bars=args.min_bars,
        cache_dir=args.cache_dir,
        refresh_cache=args.refresh_cache,
        cache_raw=not args.no_cache,
        request_sleep=args.sleep,
        output_json=args.output_json,
        output_csv=args.output_csv,
        output_html=args.output_html,
    )
    instruments = parse_instruments(args.instrument, args.instrument_file)
    report = run_volatility_analysis(
        cfg,
        instruments=instruments,
        include_regex=args.include_regex,
        exclude_regex=args.exclude_regex,
        max_instruments=args.max_instruments,
        demo=args.demo,
    )
    save_outputs(report, cfg)
    print_console_summary(report)
    return report


if __name__ == "__main__":
    main()
