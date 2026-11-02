"""Probe public REST market-data endpoints on CoinDCX, Binance USDM and Bybit v5.

Read-only GETs, no credentials. Prints status, latency (3 samples), payload size,
rate-limit headers and a short shape summary per endpoint. Results also saved as JSON.
"""
import json
import statistics
import sys
import time

import requests

NOW_S = int(time.time())
import net
S = net.session()
S.headers["User-Agent"] = "pulse-probe/0.1"

DCX = "https://api.coindcx.com"
DCXP = "https://public.coindcx.com"
BN = "https://fapi.binance.com"
BY = "https://api.bybit.com"

ENDPOINTS = [
    # name, url, params
    ("dcx.active_instruments", f"{DCX}/exchange/v1/derivatives/futures/data/active_instruments", {"margin_currency_short_name[]": "USDT"}),
    ("dcx.instrument", f"{DCX}/exchange/v1/derivatives/futures/data/instrument", {"pair": "B-BTC_USDT", "margin_currency_short_name": "USDT"}),
    ("dcx.candles_1h_90d", f"{DCXP}/market_data/candlesticks", {"pair": "B-BTC_USDT", "from": NOW_S - 90 * 86400, "to": NOW_S, "resolution": "60", "pcode": "f"}),
    ("dcx.candles_1m_1d", f"{DCXP}/market_data/candlesticks", {"pair": "B-BTC_USDT", "from": NOW_S - 86400, "to": NOW_S, "resolution": "1", "pcode": "f"}),
    ("dcx.current_prices_rt", f"{DCXP}/market_data/v3/current_prices/futures/rt", None),
    ("dcx.pair_stats", f"{DCX}/api/v1/derivatives/futures/data/stats", {"pair": "B-BTC_USDT"}),
    ("bn.exchangeInfo", f"{BN}/fapi/v1/exchangeInfo", None),
    ("bn.fundingInfo", f"{BN}/fapi/v1/fundingInfo", None),
    ("bn.klines_1h_1500", f"{BN}/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 1500}),
    ("bn.ticker24h_all", f"{BN}/fapi/v1/ticker/24hr", None),
    ("bn.premiumIndex_all", f"{BN}/fapi/v1/premiumIndex", None),
    ("bn.fundingRate", f"{BN}/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 10}),
    ("bn.openInterest", f"{BN}/fapi/v1/openInterest", {"symbol": "BTCUSDT"}),
    ("bn.oiHist_5m_300", f"{BN}/futures/data/openInterestHist", {"symbol": "BTCUSDT", "period": "5m", "limit": 300}),
    ("bn.globalLSAccount_5m", f"{BN}/futures/data/globalLongShortAccountRatio", {"symbol": "BTCUSDT", "period": "5m", "limit": 30}),
    ("bn.topLSPosition_5m", f"{BN}/futures/data/topLongShortPositionRatio", {"symbol": "BTCUSDT", "period": "5m", "limit": 30}),
    ("bn.takerLS_5m", f"{BN}/futures/data/takerlongshortRatio", {"symbol": "BTCUSDT", "period": "5m", "limit": 30}),
    ("by.instruments_linear", f"{BY}/v5/market/instruments-info", {"category": "linear", "limit": 1000}),
    ("by.kline_60_1000", f"{BY}/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "60", "limit": 1000}),
    ("by.tickers_linear_all", f"{BY}/v5/market/tickers", {"category": "linear"}),
    ("by.funding_history", f"{BY}/v5/market/funding/history", {"category": "linear", "symbol": "BTCUSDT", "limit": 10}),
    ("by.open_interest_5min", f"{BY}/v5/market/open-interest", {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min", "limit": 200}),
    ("by.account_ratio_5min", f"{BY}/v5/market/account-ratio", {"category": "linear", "symbol": "BTCUSDT", "period": "5min", "limit": 50}),
]

RL_HEADERS = ("x-mbx-used-weight-1m", "x-mbx-used-weight", "x-bapi-limit", "x-bapi-limit-status",
              "x-bapi-limit-reset-timestamp", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining")


def shape(obj, depth=0):
    if isinstance(obj, list):
        return f"list[{len(obj)}]" + (f" of {shape(obj[0], depth + 1)}" if obj and depth < 2 else "")
    if isinstance(obj, dict):
        keys = list(obj.keys())
        return "{" + ",".join(keys[:14]) + ("…" if len(keys) > 14 else "") + "}"
    return type(obj).__name__


def probe(name, url, params):
    lat, last = [], None
    for _ in range(3):
        t0 = time.perf_counter()
        try:
            r = S.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            return {"name": name, "error": repr(exc)}
        lat.append((time.perf_counter() - t0) * 1000)
        last = r
        time.sleep(0.3)
    try:
        body = last.json()
    except ValueError:
        body = last.text[:300]
    return {
        "name": name, "status": last.status_code,
        "lat_ms_min": round(min(lat)), "lat_ms_med": round(statistics.median(lat)),
        "bytes": len(last.content), "encoding": last.headers.get("content-encoding"),
        "rl": {h: last.headers[h] for h in RL_HEADERS if h in last.headers},
        "shape": shape(body), "body": body,
    }


def main():
    results = []
    for name, url, params in ENDPOINTS:
        res = probe(name, url, params)
        results.append(res)
        if "error" in res:
            print(f"{name:26s} ERROR {res['error']}")
            continue
        print(f"{name:26s} {res['status']} min={res['lat_ms_min']:>4}ms med={res['lat_ms_med']:>4}ms "
              f"{res['bytes'] / 1024:7.1f}KB enc={res['encoding']} rl={res['rl']} {res['shape']}")
    with open(sys.argv[1] if len(sys.argv) > 1 else "rest_results.json", "w") as fh:
        json.dump(results, fh, default=str)


if __name__ == "__main__":
    main()
