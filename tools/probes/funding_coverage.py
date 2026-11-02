"""Evidence for ADR-0041: can consecutive funding settlements be verified from Binance history plus
CURRENT interval metadata, without a historical schedule?

Per pair (read-only public GETs, paced under Binance's shared 500 requests / 5 min for fundingRate):
  - fetch 8 days of Binance fundingRate events;
  - classify the window against the current interval h (Binance fundingInfo, default 8):
      complete        every spacing == h +-5 s            -> d_last and 1D/7D verifiable
      gap             spacings are multiples of h         -> missing event OR earlier longer interval (both: unavailable)
      interval_change spacings not a multiple of h        -> detected, unavailable
  - latest settlement + h ~= premiumIndex.nextFundingTime ;
  - CoinDCX fr == Binance latest settled rate ;
  - CoinDCX funding_frequency == Binance interval (all pairs, one call each).
Usage: python funding_coverage.py [--sample N] [--out result.json]
  --sample N   check all non-8H pairs plus N random 8H pairs (default 60); 0 = only the interval-agreement check
"""
import argparse
import json
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import net

S = net.session()
NOW = int(time.time() * 1000)
EIGHT_D = 8 * 86400 * 1000
JITTER_MS = 5000
BN = "https://fapi.binance.com"
DCX = "https://api.coindcx.com"


def get(url, params=None):
    for attempt in range(3):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(2 + 2 * attempt)
    return None


_last = [0.0]


def paced(gap):
    while time.time() - _last[0] < gap:
        time.sleep(0.01)
    _last[0] = time.time()


def classify(times, h):
    H = h * 3600 * 1000
    if len(times) < 2:
        return "single" if times else "empty"
    spacing = [b - a for a, b in zip(times, times[1:])]
    if all(abs(s - H) <= JITTER_MS for s in spacing):
        return "complete"
    if all(abs(s - round(s / H) * H) <= JITTER_MS for s in spacing):
        return "gap"
    return "interval_change"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--out", default="funding_coverage.json")
    args = ap.parse_args()

    pairs = sorted(p for p in get(DCX + "/exchange/v1/derivatives/futures/data/active_instruments",
                                   {"margin_currency_short_name[]": "USDT"}) if p.startswith("B-"))
    info = {x["symbol"]: x for x in get(BN + "/fapi/v1/fundingInfo")}
    prem = {x["symbol"]: x for x in get(BN + "/fapi/v1/premiumIndex")}
    rt = get("https://public.coindcx.com/market_data/v3/current_prices/futures/rt")["prices"]
    sym = {p: p[2:].replace("_", "") for p in pairs}
    interval = lambda p: int(info.get(sym[p], {}).get("fundingIntervalHours", 8))
    print(f"universe {len(pairs)}; Binance intervals {dict(Counter(interval(p) for p in pairs))}")

    def dcx_interval(p):
        paced(0.125)
        r = get(DCX + "/exchange/v1/derivatives/futures/data/instrument",
                {"pair": p, "margin_currency_short_name": "USDT"})
        return p, (r or {}).get("instrument", {}).get("funding_frequency")

    with ThreadPoolExecutor(4) as ex:
        agree = Counter("agree" if (f is not None and float(f) == interval(p)) else "DISAGREE_OR_MISSING"
                        for p, f in ex.map(dcx_interval, pairs))
    print("CoinDCX funding_frequency vs Binance interval:", dict(agree))
    if not args.sample:
        return

    non8 = [p for p in pairs if interval(p) != 8]
    rest = [p for p in pairs if interval(p) == 8]
    random.seed(7)
    sample = non8 + random.sample(rest, min(args.sample, len(rest)))
    result, rows = Counter(), []
    for p in sample:
        paced(0.6)
        ev = get(BN + "/fapi/v1/fundingRate", {"symbol": sym[p], "startTime": NOW - EIGHT_D, "limit": 1000})
        if ev is None:
            result["fetch_failed"] += 1
            continue
        kinds = Counter(e.get("rateType", "Regular") for e in ev)
        regular = sorted({e["fundingTime"]: e for e in ev if e.get("rateType", "Regular") == "Regular"}.items())
        times = [t for t, _ in regular]
        h = interval(p)
        cls = classify(times, h)
        result[cls] += 1
        nxt = prem.get(sym[p], {}).get("nextFundingTime")
        if times and nxt:
            result["latest+h==nextFundingTime" if abs(times[-1] + h * 3600000 - nxt) <= JITTER_MS
                   else "latest+h!=nextFundingTime"] += 1
        fr = rt.get(p, {}).get("fr")
        if regular and fr is not None:
            result["coindcx_fr==binance_latest" if abs(float(regular[-1][1]["fundingRate"]) - float(fr)) < 1e-12
                   else "coindcx_fr!=binance_latest"] += 1
        if kinds.get("Special") or any(k not in ("Regular", "Special") for k in kinds):
            result["special_or_unknown_event_types"] += 1
        rows.append({"pair": p, "interval_h": h, "events": len(times), "class": cls})
    print(json.dumps(dict(result), indent=1))
    print("not complete:", [r for r in rows if r["class"] != "complete"][:12])
    json.dump({"summary": dict(result), "rows": rows}, open(args.out, "w"))


if __name__ == "__main__":
    main()
