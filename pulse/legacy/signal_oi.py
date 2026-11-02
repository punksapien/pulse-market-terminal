import hashlib
import hmac
import json
import math
import os
import time
import re
import threading
import numpy as np
import requests
from typing import Any, Dict, List, Optional, Tuple

BINANCE_OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"
BYBIT_OI_URL = "https://api.bybit.com/v5/market/open-interest"
COINDCX_STATS_URL = "https://api.coindcx.com/api/v1/derivatives/futures/data/stats"
SECONDS_PER_YEAR = 31_536_000
OI_PROVIDER_COOLDOWN_SECONDS = 300

_PROVIDER_STATE = {
    "lock": threading.Lock(),
    "disabled_until": {},
}


def unavailable_oi_metrics(reason: str = "unavailable") -> Dict[str, Any]:
    return {
        "oi_available": False,
        "oi_24h_change": None,
        "oi_usd": None,
        "oi_trend": "unavailable",
        "oi_velocity": None,
        "oi_acceleration": None,
        "oi_history": [],
        "oi_source": None,
        "oi_source_symbol": None,
        "oi_asof": None,
        "oi_error": reason,
    }


def _provider_order() -> List[str]:
    configured = os.environ.get("OI_PROVIDER_ORDER", "")
    if configured:
        providers = [
            value.strip().lower()
            for value in configured.split(",")
            if value.strip().lower() in {"binance", "bybit"}
        ]
        if providers:
            return list(dict.fromkeys(providers))
    # Binance is source-compatible with CoinDCX's B-* futures mapping, but its
    # public derivatives endpoints are blocked from the current Vercel runtime.
    return ["bybit", "binance"] if os.environ.get("VERCEL") == "1" else ["binance", "bybit"]


def get_oi_provider_order() -> List[str]:
    return _provider_order()


def _provider_enabled(provider: str) -> bool:
    now = time.time()
    with _PROVIDER_STATE["lock"]:
        return _PROVIDER_STATE["disabled_until"].get(provider, 0.0) <= now


def _disable_provider_temporarily(provider: str) -> None:
    with _PROVIDER_STATE["lock"]:
        _PROVIDER_STATE["disabled_until"][provider] = (
            time.time() + OI_PROVIDER_COOLDOWN_SECONDS
        )


def _coindcx_market_symbol(cdx_pair: str, market_symbol: str = "") -> str:
    candidate = str(market_symbol or "").strip().upper()
    if candidate:
        return re.sub(r"[^A-Z0-9]", "", candidate)
    return _cdx_to_binance_symbol(cdx_pair)


def _request_provider_json(
    provider: str,
    url: str,
    params: Dict[str, Any],
    session: Optional[requests.Session] = None,
    timeout: float = 8.0,
) -> Any:
    sess = session or requests.Session()
    try:
        response = sess.get(url, params=params, timeout=timeout)
    except Exception:
        _disable_provider_temporarily(provider)
        return None

    if response.status_code in {403, 418, 429, 451} or response.status_code >= 500:
        _disable_provider_temporarily(provider)
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


def _cdx_to_binance_symbol(cdx_pair: str) -> str:
    raw = cdx_pair.replace("B-", "").replace("_", "")
    return raw.upper()


def fetch_binance_oi(symbol: str, period: str = "1h", limit: int = 48,
                     session: Optional[requests.Session] = None,
                     market_symbol: str = "") -> List[Dict]:
    bsym = _coindcx_market_symbol(symbol, market_symbol)
    payload = _request_provider_json(
        "binance",
        BINANCE_OI_URL,
        {"symbol": bsym, "period": period, "limit": limit},
        session=session,
    )
    return payload if isinstance(payload, list) else []


def fetch_bybit_oi(
    symbol: str,
    period: str = "1h",
    limit: int = 25,
    session: Optional[requests.Session] = None,
    market_symbol: str = "",
) -> List[Dict]:
    bybit_symbol = _coindcx_market_symbol(symbol, market_symbol)
    interval = {
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }.get(str(period).lower(), "1h")
    payload = _request_provider_json(
        "bybit",
        BYBIT_OI_URL,
        {
            "category": "linear",
            "symbol": bybit_symbol,
            "intervalTime": interval,
            "limit": max(2, min(int(limit), 200)),
        },
        session=session,
    )
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        return []
    result = payload.get("result", {})
    values = result.get("list", []) if isinstance(result, dict) else []
    return values if isinstance(values, list) else []


def compute_oi_metrics(
    oi_data: List[Dict],
    provider: str = "binance",
    price_hint: Optional[float] = None,
    source_symbol: str = "",
) -> Dict[str, Any]:
    if not oi_data or len(oi_data) < 2:
        return unavailable_oi_metrics(f"{provider}:insufficient_history")

    normalized = []
    for index, item in enumerate(oi_data):
        if not isinstance(item, dict):
            continue
        try:
            timestamp = int(item.get("timestamp", index))
            if provider == "bybit":
                oi_size = float(item.get("openInterest", 0))
                oi_value = (
                    oi_size * float(price_hint)
                    if price_hint is not None and float(price_hint) > 0
                    else None
                )
                change_basis = oi_size
            else:
                oi_size = float(item.get("sumOpenInterest", 0))
                oi_value = float(item.get("sumOpenInterestValue", 0))
                change_basis = oi_value
        except (TypeError, ValueError):
            continue
        if change_basis <= 0:
            continue
        normalized.append((timestamp, change_basis, oi_value, oi_size))

    normalized.sort(key=lambda value: value[0])
    vals = normalized
    if len(vals) < 2:
        return unavailable_oi_metrics(f"{provider}:invalid_history")

    latest_basis = vals[-1][1]
    earliest_basis = vals[0][1]
    latest_usd = vals[-1][2]
    change = math.log(max(latest_basis, 1e-9) / max(earliest_basis, 1e-9))

    mid = len(vals) // 2
    first_half_chg = (
        math.log(max(vals[mid][1], 1e-9) / max(vals[0][1], 1e-9))
        if mid > 0 else 0
    )
    second_half_chg = (
        math.log(max(vals[-1][1], 1e-9) / max(vals[mid][1], 1e-9))
        if mid > 0 else 0
    )
    velocity = second_half_chg
    acceleration = second_half_chg - first_half_chg

    trend = "rising" if change > 0.02 else "falling" if change < -0.02 else "neutral"
    history = [
        round(value[2], 2) if value[2] is not None else round(value[3], 8)
        for value in vals[-25:]
    ]
    return {
        "oi_available": True,
        "oi_24h_change": round(change, 4),
        "oi_usd": round(latest_usd, 2) if latest_usd is not None else None,
        "oi_trend": trend,
        "oi_velocity": round(velocity, 4),
        "oi_acceleration": round(acceleration, 4),
        "oi_history": history,
        "oi_source": provider,
        "oi_source_symbol": source_symbol or None,
        "oi_asof": vals[-1][0],
        "oi_error": None,
    }


def fetch_oi_metrics(
    symbol: str,
    market_symbol: str = "",
    price_hint: Optional[float] = None,
    period: str = "1h",
    limit: int = 25,
    session: Optional[requests.Session] = None,
    providers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    source_symbol = _coindcx_market_symbol(symbol, market_symbol)
    attempted = []
    for provider in providers or _provider_order():
        provider = str(provider).lower()
        if provider not in {"binance", "bybit"} or not _provider_enabled(provider):
            continue
        attempted.append(provider)
        if provider == "bybit":
            history = fetch_bybit_oi(
                symbol,
                period=period,
                limit=limit,
                session=session,
                market_symbol=source_symbol,
            )
        else:
            history = fetch_binance_oi(
                symbol,
                period=period,
                limit=limit,
                session=session,
                market_symbol=source_symbol,
            )
        metrics = compute_oi_metrics(
            history,
            provider=provider,
            price_hint=price_hint,
            source_symbol=source_symbol,
        )
        if metrics.get("oi_available"):
            return metrics
    attempted_label = ",".join(attempted) if attempted else "no_provider"
    return unavailable_oi_metrics(f"{attempted_label}:no_usable_oi")


def fetch_coindcx_stats(pair: str, key: str = "", secret: str = "",
                        session: Optional[requests.Session] = None) -> Dict[str, Any]:
    if not key or not secret:
        return {}
    sess = session or requests.Session()
    cdx_pair = pair.replace("_", "_")
    if not cdx_pair.startswith("B-"):
        cdx_pair = f"B-{cdx_pair}"
    ts = int(round(time.time() * 1000))
    body = json.dumps({"timestamp": ts}, separators=(",", ":"))
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    try:
        r = sess.get(
            f"{COINDCX_STATS_URL}?pair={cdx_pair}",
            data=body,
            headers={"Content-Type": "application/json",
                      "X-AUTH-APIKEY": key, "X-AUTH-SIGNATURE": sig},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "funding_rate": _extract_funding(data),
                "long_pct": data.get("position", {}).get("count_percent", {}).get("long", 50),
                "short_pct": data.get("position", {}).get("count_percent", {}).get("short", 50),
                "price_change_1h": data.get("price_change_percent", {}).get("1H", 0),
                "price_change_24h": data.get("price_change_percent", {}).get("1D", 0),
                "price_change_7d": data.get("price_change_percent", {}).get("1W", 0),
                "high_24h": data.get("high_and_low", {}).get("1D", {}).get("h", 0),
                "low_24h": data.get("high_and_low", {}).get("1D", {}).get("l", 0),
            }
    except Exception:
        pass
    return {}


def _extract_funding(data: Dict) -> float:
    fr = data.get("funding_rate")
    if fr is not None:
        try:
            return float(fr)
        except (ValueError, TypeError):
            pass
    return 0.0
