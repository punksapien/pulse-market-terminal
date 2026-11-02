"""Measure reference-ladder payload size from REAL CoinDCX 1-min opens (all active pairs).

For each pair, fetch 1-min candles around T-1h, T-4h, T-24h (narrow ranges), build
5- and 8-slot ladders, and measure compact JSON / gzip(6) / brotli(5) for several encodings.
Paced at ~8 req/s (CoinDCX FAQ limit 16/s). Read-only.
"""
import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import net

try:
    import brotli  # optional
except ImportError:
    brotli = None

PAIRS = json.load(open("rest_results.json"))[0]["body"]
S = net.session()
NOW = int(time.time()) // 60 * 60          # current minute start (s)
T0 = NOW + 60                              # ladder starts at next minute (future slots)
WINDOWS = [3600, 14400, 86400]
MAX_SLOTS = 8
_last = [0.0]


def paced_get(params):
    # crude global pacing: <= ~8 req/s across threads
    while True:
        now = time.time()
        if now - _last[0] >= 0.125:
            _last[0] = now
            break
        time.sleep(0.01)
    for attempt in range(3):
        try:
            r = S.get("https://public.coindcx.com/market_data/candlesticks", params=params, timeout=20)
            if r.status_code == 200:
                return r.json().get("data") or []
        except Exception:
            pass
        time.sleep(1 + attempt)
    return None


def fetch_pair(pair):
    out = []
    for w in WINDOWS:
        frm = T0 - w - 120
        to = T0 - w + MAX_SLOTS * 60 + 60
        data = paced_get({"pair": pair, "from": frm, "to": to, "resolution": "1", "pcode": "f"})
        if data is None:
            out.append([None] * MAX_SLOTS)
            continue
        opens = {c["time"] // 1000: c["open"] for c in data}
        out.append([opens.get(T0 - w + k * 60) for k in range(MAX_SLOTS)])
    return out


def sizes(obj):
    b = json.dumps(obj, separators=(",", ":")).encode()
    g = len(gzip.compress(b, 6))
    br = len(brotli.compress(b, quality=5)) if brotli else None
    return len(b), g, br


def decimals(x):
    s = repr(x)
    return len(s.split(".")[1]) if "." in s and "e" not in s else 0


def main():
    t = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        ladders = list(ex.map(fetch_pair, PAIRS))
    fetch_s = time.time() - t
    nulls = sum(v is None for L in ladders for w in L for v in w)
    print(f"pairs={len(PAIRS)} fetch {fetch_s:.0f}s, null slots {nulls}/{len(PAIRS)*3*MAX_SLOTS}")
    header = {"version": 1, "mapping_version": 1, "definition_version": "price_change.v1",
              "source": "coindcx", "valid_from": T0 * 1000, "step_ms": 60000,
              "windows": [w * 1000 for w in WINDOWS], "pairs": PAIRS}
    for slots in (5, 8):
        rows = [[w[:slots] for w in L] for L in ladders]
        packed = {**header, "rows": rows}
        # delta encoding: per pair-window, first price + integer deltas in that pair's decimal precision
        drows, scales = [], []
        for L in rows:
            dec = max((decimals(v) for w in L for v in w if v is not None), default=0)
            m = 10 ** dec
            scales.append(dec)
            pr = []
            for w in L:
                ints = [round(v * m) if v is not None else None for v in w]
                first = next((i for i in ints if i is not None), None)
                deltas, prev = [], first
                for i in ints:
                    if i is None:
                        deltas.append(None)
                    else:
                        deltas.append(i - prev)
                        prev = i
                pr.append([first] + deltas[1:] if first is not None else [None] * slots)
            drows.append(pr)
        delta = {**header, "decimals": scales, "rows_delta": drows}
        print(f"slots={slots} packed  raw/gzip/brotli = {sizes(packed)}")
        print(f"slots={slots} delta   raw/gzip/brotli = {sizes(delta)}")
    fb = {**header, "fallback_refs": [[w[0] for w in L] for L in ladders]}
    print(f"fallback refs only (3/pair) raw/gzip/brotli = {sizes(fb)}")
    json.dump({"T0": T0, "pairs": PAIRS, "ladders": ladders}, open("ladder_real.json", "w"))


if __name__ == "__main__":
    main()
