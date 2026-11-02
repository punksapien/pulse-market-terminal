"""Record a fixture proving the 24h price-change method (ADR-0005).

For each symbol, saves Binance's 24h ticker and the 1-min kline at the ticker's
minute-aligned openTime, then checks lastPrice / kline_open - 1 == priceChangePercent.
Output: tests/fixtures/price_change/binance_24h_<SYMBOL>.json (public market data only).
Usage: python tools/probes/record_24h_fixture.py BTCUSDT ETHUSDT DOGEUSDT XAUUSDT
"""
import json
import os
import sys

import net

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tests", "fixtures", "price_change")


def main(symbols):
    os.makedirs(OUT, exist_ok=True)
    s = net.session()
    for sym in symbols:
        t = s.get("https://fapi.binance.com/fapi/v1/ticker/24hr", params={"symbol": sym}, timeout=10).json()
        k = s.get("https://fapi.binance.com/fapi/v1/klines",
                  params={"symbol": sym, "interval": "1m", "startTime": t["openTime"], "limit": 1}, timeout=10).json()[0]
        ours = (float(t["lastPrice"]) / float(k[1]) - 1) * 100
        record = {
            "symbol": sym,
            "ticker": {key: t[key] for key in ("lastPrice", "openPrice", "priceChangePercent", "openTime", "closeTime")},
            "kline_1m_at_openTime": {"openTime": k[0], "open": k[1]},
            "computed_percent": round(ours, 3),
            "matches": round(ours, 3) == float(t["priceChangePercent"]) and float(k[1]) == float(t["openPrice"]),
        }
        with open(os.path.join(OUT, f"binance_24h_{sym}.json"), "w") as fh:
            json.dump(record, fh, indent=1)
        print(sym, "matches" if record["matches"] else "MISMATCH", record["computed_percent"], t["priceChangePercent"])


if __name__ == "__main__":
    main(sys.argv[1:] or ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "XAUUSDT"])
