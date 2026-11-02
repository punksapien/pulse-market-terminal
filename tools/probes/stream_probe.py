"""Concurrently probe public market streams on Binance USDM, Bybit v5 linear and CoinDCX futures.

For each stream: message count/rate, bytes/s, inter-arrival, event->receive latency
(corrected by measured clock offset), field samples. Also records BTC trade/LTP prints
from all venues to estimate CoinDCX lag versus Binance.
Usage: python stream_probe.py <seconds> <clock_offset_ms> <out.json>
"""
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict

import socketio
import websockets

import net

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60
OFFSET_MS = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
OUT = sys.argv[3] if len(sys.argv) > 3 else "stream_results.json"

stats = defaultdict(lambda: {"n": 0, "bytes": 0, "arr": [], "lat": [], "sample": None, "items": 0})
btc_prints = defaultdict(list)  # venue -> [(recv_ms_corrected, event_ms, price)]
errors = []
rt_ls_updates = defaultdict(int)  # CoinDCX rt: per-pair count of last-price updates
rt_tick_age = []                  # CoinDCX rt: receive time - btST (third-party tick send time)
bn_ticker_updates = defaultdict(int)  # Binance !ticker@arr per-symbol updates (USDM only)
by_price_updates = defaultdict(int)   # Bybit tickers deltas carrying lastPrice, per symbol


def now_ms():
    return time.time() * 1000 + OFFSET_MS


def rec(key, raw_len, event_ms=None, sample=None, items=1):
    s = stats[key]
    t = now_ms()
    s["n"] += 1
    s["bytes"] += raw_len
    s["items"] += items
    s["arr"].append(t)
    if event_ms:
        s["lat"].append(t - float(event_ms))
    if s["sample"] is None and sample is not None:
        s["sample"] = sample


# ---------------- Binance market streams ----------------
BN_STREAMS = [
    "!markPrice@arr@1s", "!miniTicker@arr", "!ticker@arr", "!bookTicker",
    "btcusdt@aggTrade", "btcusdt@markPrice@1s", "btcusdt@kline_1m", "btcusdt@bookTicker",
    "btcusdt@depth20@100ms", "!forceOrder@arr",
]


async def binance(base, tag):
    url = f"{base}/stream?streams=" + "/".join(BN_STREAMS)
    try:
        async with websockets.connect(url, max_size=None, ping_interval=None, open_timeout=15, ssl=net.ssl_context()) as ws:
            end = time.time() + DURATION
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                stream, data = msg.get("stream"), msg.get("data")
                if isinstance(data, list):
                    if stream == "!ticker@arr":
                        for d in data:
                            if d.get("st", 1) == 1:
                                bn_ticker_updates[d["s"]] += 1
                    ev = max((d.get("E", 0) for d in data), default=None)
                    rec(f"{tag}:{stream}", len(raw), ev, data[0] if data else None, len(data))
                else:
                    ev = data.get("E") or data.get("T")
                    rec(f"{tag}:{stream}", len(raw), ev, data)
                    if stream == "btcusdt@aggTrade":
                        btc_prints[tag].append((now_ms(), data["T"], float(data["p"])))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{tag}: {exc!r}")


# ---------------- Binance WebSocket API (request/response) ----------------
async def binance_wsapi():
    url = "wss://ws-fapi.binance.com/ws-fapi/v1"
    try:
        async with websockets.connect(url, max_size=None, open_timeout=15, ssl=net.ssl_context()) as ws:
            for i, (method, params) in enumerate([
                ("ticker.price", {"symbol": "BTCUSDT"}),
                ("ticker.book", {"symbol": "BTCUSDT"}),
                ("depth", {"symbol": "BTCUSDT", "limit": 5}),
                ("ticker.price", {}),
            ] * 3):
                t0 = time.perf_counter()
                await ws.send(json.dumps({"id": str(i), "method": method, "params": params}))
                raw = await ws.recv()
                rtt = (time.perf_counter() - t0) * 1000
                msg = json.loads(raw)
                key = f"bn-wsapi:{method}{'(all)' if not params else ''}"
                s = stats[key]
                s["n"] += 1
                s["bytes"] += len(raw)
                s["lat"].append(rtt)  # here: request round-trip
                if s["sample"] is None:
                    res = msg.get("result")
                    s["sample"] = {"status": msg.get("status"), "rateLimits": msg.get("rateLimits"),
                                   "result": res[:2] if isinstance(res, list) else res, "error": msg.get("error")}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bn-wsapi: {exc!r}")


# ---------------- Bybit public linear ----------------
async def bybit(symbols):
    url = "wss://stream.bybit.com/v5/public/linear"
    try:
        async with websockets.connect(url, max_size=None, ping_interval=None, open_timeout=15,
                                      ssl=net.ssl_context()) as ws:
            topics = ["kline.1.BTCUSDT", "publicTrade.BTCUSDT", "orderbook.1.BTCUSDT", "allLiquidation.BTCUSDT"]
            topics += [f"tickers.{s}" for s in symbols]
            for i in range(0, len(topics), 200):
                await ws.send(json.dumps({"op": "subscribe", "req_id": str(i), "args": topics[i:i + 200]}))
            end = time.time() + DURATION
            last_ping = time.time()
            while time.time() < end:
                if time.time() - last_ping > 18:
                    await ws.send(json.dumps({"op": "ping"}))
                    last_ping = time.time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, min(5, end - time.time())))
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                topic = msg.get("topic")
                if not topic:
                    if msg.get("op") == "subscribe" and not msg.get("success"):
                        errors.append(f"bybit subscribe: {msg}")
                    continue
                kind = topic.split(".")[0]
                key = f"by:{kind}" + (".BTCUSDT" if topic.endswith("BTCUSDT") and kind == "tickers" else "")
                rec(key, len(raw), msg.get("ts"), msg.get("data"))
                if kind == "tickers" and "lastPrice" in (msg.get("data") or {}):
                    by_price_updates[topic.split(".", 1)[1]] += 1
                if topic == "publicTrade.BTCUSDT":
                    for tr in msg["data"]:
                        btc_prints["by"].append((now_ms(), tr["T"], float(tr["p"])))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bybit: {exc!r}")


# ---------------- CoinDCX socket.io v2 ----------------
DCX_CHANNELS = [
    "currentPrices@futures@rt",
    "B-BTC_USDT@prices-futures",
    "B-BTC_USDT@trades-futures",
    "B-BTC_USDT_1m-futures",
    "B-BTC_USDT@orderbook@20-futures",
]


async def coindcx():
    sio = socketio.AsyncClient(reconnection=False, ssl_verify=True)
    events_seen = defaultdict(int)

    def make_handler(event):
        async def handler(payload):
            events_seen[event] += 1
            raw = json.dumps(payload)
            data = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    pass
            ev = None
            if isinstance(data, dict):
                ev = data.get("T") or data.get("ts") or data.get("pST") or data.get("Ets") or data.get("E")
            rec(f"dcx:{event}", len(raw), ev, data)
            if event == "price-change" and isinstance(data, dict):
                btc_prints["dcx-ltp"].append((now_ms(), data.get("T"), float(data["p"])))
            if event == "new-trade" and isinstance(data, dict) and data.get("s") == "B-BTC_USDT":
                btc_prints["dcx-trade"].append((now_ms(), data.get("T"), float(data["p"])))
            if event == "currentPrices@futures#update" and isinstance(data, dict):
                p = (data.get("prices") or {}).get("B-BTC_USDT", {})
                if "ls" in p:
                    btc_prints["dcx-rt"].append((now_ms(), p.get("btST"), float(p["ls"])))
                stats["dcx:rt-pairs-per-msg"]["arr"].append(len(data.get("prices") or {}))
                t = now_ms()
                for pair, fields in (data.get("prices") or {}).items():
                    if "ls" in fields:
                        rt_ls_updates[pair] += 1
                        if fields.get("btST"):
                            rt_tick_age.append(t - float(fields["btST"]))
        return handler

    for ev in ("currentPrices@futures#update", "price-change", "new-trade", "candlestick",
               "depth-snapshot", "depth-update"):
        sio.on(ev, make_handler(ev))

    @sio.on("connect")
    async def on_connect():
        for ch in DCX_CHANNELS:
            await sio.emit("join", {"channelName": ch})

    try:
        await sio.connect("wss://stream.coindcx.com", transports=["websocket"])
        end = time.time() + DURATION
        while time.time() < end:
            await asyncio.sleep(min(25, max(0.1, end - time.time())))
            if time.time() < end:
                await sio.emit("ping", {"data": "Ping message"})
        await sio.disconnect()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"coindcx: {exc!r}")
    stats["dcx:events_seen"]["sample"] = dict(events_seen)


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * q))], 1) if xs else None


def lag_vs_binance(venue, t_tol_ms=50):
    """Same trade event on both venues (equal price, trade times within tolerance):
    lag = venue receive time - Binance receive time."""
    bn = btc_prints.get("bn-market")
    out = []
    if not bn:
        return None
    for recv, t, price in btc_prints.get(venue, []):
        if not t:
            continue
        cands = [r for r, bt, p in bn if p == price and abs(float(bt) - float(t)) <= t_tol_ms]
        if cands:
            out.append(recv - min(cands))
    return {"matched": len(out), "of": len(btc_prints.get(venue, [])),
            "p50_ms": pct(out, .5), "p95_ms": pct(out, .95)} if out else {"matched": 0, "of": len(btc_prints.get(venue, []))}


async def main():
    symbols = json.load(open("bybit_overlap_symbols.json"))
    await asyncio.gather(
        binance("wss://fstream.binance.com", "bn"),
        binance("wss://fstream.binance.com/public", "bn-public"),
        binance("wss://fstream.binance.com/market", "bn-market"),
        binance_wsapi(),
        bybit(symbols),
        coindcx(),
    )
    report = {}
    for key, s in sorted(stats.items()):
        arr = s["arr"]
        gaps = [b - a for a, b in zip(arr, arr[1:])] if len(arr) > 1 and not key.endswith("per-msg") else []
        report[key] = {
            "msgs": s["n"], "msg_per_s": round(s["n"] / DURATION, 2), "items": s["items"],
            "kB_per_s": round(s["bytes"] / DURATION / 1024, 2),
            "gap_p50_ms": pct(gaps, .5), "gap_p95_ms": pct(gaps, .95),
            "lat_p50_ms": pct(s["lat"], .5), "lat_p95_ms": pct(s["lat"], .95),
            "sample": s["sample"] if not key.endswith("per-msg") else {"pairs_per_msg_p50": pct(arr, .5), "max": max(arr) if arr else None},
        }
    report["_lag_vs_binance_btc"] = {v: lag_vs_binance(v) for v in ("by", "dcx-ltp", "dcx-trade", "dcx-rt")}
    ups = sorted(rt_ls_updates.values())
    report["_dcx_rt_universe"] = {
        "pairs_with_ls_updates": len(ups),
        "updates_per_pair_per_min_p10_p50_p90": [round(pct(ups, q) * 60 / DURATION, 1) for q in (.1, .5, .9)] if ups else None,
        "tick_send_to_receive_ms_p50_p95": [pct(rt_tick_age, .5), pct(rt_tick_age, .95)],
    }
    def per_min(counter, universe=None):
        vals = sorted(v for k, v in counter.items() if universe is None or k in universe)
        return {"symbols": len(vals), "p10_p50_p90": [round(pct(vals, q) * 60 / DURATION, 1) for q in (.1, .5, .9)] if vals else None}
    overlap = set(json.load(open("bybit_overlap_symbols.json")))
    report["_price_updates_per_symbol_per_min"] = {
        "binance_!ticker@arr(all usdm)": per_min(bn_ticker_updates),
        "binance_!ticker@arr(bybit-overlap)": per_min(bn_ticker_updates, overlap),
        "bybit_tickers_lastPrice(overlap)": per_min(by_price_updates),
    }
    json.dump({k: v for k, v in btc_prints.items()}, open("btc_prints.json", "w"))
    report["_errors"] = errors
    json.dump(report, open(OUT, "w"), default=str, indent=1)
    for key, r in report.items():
        if key.startswith("_"):
            print(key, json.dumps(r))
            continue
        print(f"{key:38s} msgs={r['msgs']:>6} {r['msg_per_s']:>7}/s items={r['items']:>7} {r['kB_per_s']:>8}kB/s "
              f"gap p50/p95={r['gap_p50_ms']}/{r['gap_p95_ms']}ms lat p50/p95={r['lat_p50_ms']}/{r['lat_p95_ms']}ms")


if __name__ == "__main__":
    asyncio.run(main())
