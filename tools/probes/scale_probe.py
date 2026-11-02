"""Scale and upstream-cadence probes (read-only, polite request rates).

A. Binance /market: aggTrade + kline_1m for every CoinDCX symbol on ONE connection.
B. CoinDCX socket: join @prices-futures for N pairs on one socket.
C. Upstream cadence: poll CoinDCX pair stats (L/S) and Binance openInterest every 5 s.
D. CoinDCX candle resolutions (240 / 4h) and max range per request.
Usage: python scale_probe.py <seconds> <clock_offset_ms>
"""
import asyncio
import json
import sys
import time
from collections import defaultdict

import socketio
import websockets

import net

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 60
OFF = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
now_ms = lambda: time.time() * 1000 + OFF  # noqa: E731
pairs = sorted(json.load(open("rest_results.json"))[0]["body"])  # dcx active list
symbols = [p[2:].replace("_", "").lower() for p in pairs]
out = {}


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * q))], 1) if xs else None


async def binance_all():
    url = "wss://fstream.binance.com/market/stream"
    streams = [f"{s}@aggTrade" for s in symbols] + [f"{s}@kline_1m" for s in symbols]
    counts, lat, nbytes, sub_err = defaultdict(int), defaultdict(list), 0, []
    per_sym = defaultdict(int)
    async with websockets.connect(url, max_size=None, ssl=net.ssl_context(), open_timeout=15) as ws:
        for i in range(0, len(streams), 200):  # stay well under 10 incoming msgs/s
            await ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams[i:i + 200], "id": i}))
            await asyncio.sleep(0.25)
        end = time.time() + DUR
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if "result" in m or "error" in m:
                if m.get("error"):
                    sub_err.append(m)
                continue
            nbytes += len(raw)
            d = m["data"]
            kind = d.get("e")
            counts[kind] += 1
            if kind == "aggTrade":
                lat[kind].append(now_ms() - d["T"])
                per_sym[d["s"]] += 1
            elif kind == "kline":
                lat[kind].append(now_ms() - d["E"])
    out["A_binance_one_conn"] = {
        "streams": len(streams), "subscribe_errors": sub_err[:3],
        "msgs_per_s": {k: round(v / DUR, 1) for k, v in counts.items()},
        "kB_per_s": round(nbytes / DUR / 1024, 1),
        "aggTrade_trade_to_recv_ms_p50_p95_p99": [pct(lat["aggTrade"], q) for q in (.5, .95, .99)],
        "kline_event_to_recv_ms_p50_p95": [pct(lat["kline"], q) for q in (.5, .95)],
        "symbols_with_trades": len(per_sym),
        "trades_msgs_per_symbol_per_min_p10_p50_p90": [round(pct(list(per_sym.values()), q) * 60 / DUR, 1) for q in (.1, .5, .9)],
    }


async def coindcx_many(n):
    sio = socketio.AsyncClient(reconnection=False)
    seen, lat, nbytes = defaultdict(int), [], [0]

    @sio.on("price-change")
    async def pc(payload):
        nbytes[0] += len(json.dumps(payload))
        d = json.loads(payload["data"]) if isinstance(payload.get("data"), str) else payload.get("data")
        seen["msgs"] += 1
        lat.append(now_ms() - float(d["T"]))

    @sio.on("connect")
    async def c():
        for p in pairs[:n]:
            await sio.emit("join", {"channelName": f"{p}@prices-futures"})

    await sio.connect("wss://stream.coindcx.com", transports=["websocket"])
    t0 = time.time()
    while time.time() - t0 < DUR:
        await asyncio.sleep(min(20, DUR - (time.time() - t0)))
        await sio.emit("ping", {"data": "Ping message"})
    await sio.disconnect()
    out[f"B_coindcx_{n}_ltp_channels"] = {
        "msgs_per_s": round(seen["msgs"] / DUR, 1), "kB_per_s": round(nbytes[0] / DUR / 1024, 2),
        "trade_to_recv_ms_p50_p95_p99": [pct(lat, q) for q in (.5, .95, .99)],
    }


async def cadence():
    s = net.session()
    loop = asyncio.get_running_loop()
    targets = {
        "dcx_stats_BTC": ("https://api.coindcx.com/api/v1/derivatives/futures/data/stats", {"pair": "B-BTC_USDT"}),
        "dcx_stats_DOGE": ("https://api.coindcx.com/api/v1/derivatives/futures/data/stats", {"pair": "B-DOGE_USDT"}),
        "bn_oi_BTC": ("https://fapi.binance.com/fapi/v1/openInterest", {"symbol": "BTCUSDT"}),
        "bn_oi_DOGE": ("https://fapi.binance.com/fapi/v1/openInterest", {"symbol": "DOGEUSDT"}),
    }
    hist = defaultdict(list)
    end = time.time() + DUR
    while time.time() < end:
        for name, (url, params) in targets.items():
            j = await loop.run_in_executor(None, lambda u=url, p=params: s.get(u, params=p, timeout=10).json())
            if name.startswith("dcx"):
                hist[name].append((round(now_ms()), json.dumps(j["position"])))
            else:
                hist[name].append((round(now_ms()), j["openInterest"], j["time"]))
        await asyncio.sleep(5)
    res = {}
    for name, rows in hist.items():
        vals = [r[1] for r in rows]
        changes = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
        entry = {"polls": len(rows), "value_changes": changes}
        if name.startswith("bn"):
            entry["source_time_age_ms_p50"] = pct([r[0] - r[2] for r in rows], .5)
            entry["distinct_source_times"] = len({r[2] for r in rows})
        res[name] = entry
    out["C_upstream_cadence_5s_polls"] = res


def candles():
    s = net.session()
    now = int(time.time())
    res = {}
    for label, resol, days in [("240_30d", "240", 30), ("4h_30d", "4h", 30), ("60_365d", "60", 365),
                               ("1_7d", "1", 7), ("1D_1500d", "1D", 1500), ("5_30d", "5", 30)]:
        r = s.get("https://public.coindcx.com/market_data/candlesticks",
                  params={"pair": "B-BTC_USDT", "from": now - days * 86400, "to": now, "resolution": resol, "pcode": "f"}, timeout=20)
        try:
            d = r.json()
            rows = d.get("data") or []
            span = (rows[-1]["time"] - rows[0]["time"]) / 3.6e6 if len(rows) > 1 else 0
            step = (rows[1]["time"] - rows[0]["time"]) / 60000 if len(rows) > 1 else None
            res[label] = {"status": r.status_code, "s": d.get("s"), "rows": len(rows), "span_h": round(span, 1), "step_min": step}
        except ValueError:
            res[label] = {"status": r.status_code, "body": r.text[:120]}
    out["D_coindcx_candles"] = res


async def main():
    await asyncio.gather(binance_all(), coindcx_many(100), cadence(), asyncio.to_thread(candles))
    json.dump(out, open("scale_results.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


asyncio.run(main())
