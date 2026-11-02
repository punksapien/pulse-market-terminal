"""Validate content of saved REST probe results: fields, units, freshness, universe overlap."""
import json
import statistics
import time

R = {r["name"]: r for f in ("rest_results.json", "rest_results_bybit.json") for r in json.load(open(f))}
now_ms = time.time() * 1000


def body(n):
    return R[n]["body"]


def age_s(ms):
    return round((now_ms - float(ms)) / 1000, 1)


print("=== Universe")
dcx = set(body("dcx.active_instruments"))
bn_syms = {s["symbol"]: s for s in body("bn.exchangeInfo")["symbols"]}
bn_perp = {k for k, s in bn_syms.items() if s["contractType"] in ("PERPETUAL", "TRADIFI_PERPETUAL") and s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}
by_list = body("by.instruments_linear")["result"]["list"]
by_perp = {i["symbol"] for i in by_list if i.get("contractType") == "LinearPerpetual" and i["status"] == "Trading" and i["quoteCoin"] == "USDT"}
print("bybit instruments returned", len(by_list), "nextPageCursor:", bool(body("by.instruments_linear")["result"].get("nextPageCursor")))
dcx_mkt = {p: p[2:].replace("_", "") for p in dcx}
print(f"CoinDCX USDT active: {len(dcx)} | Binance USDT perps trading: {len(bn_perp)} | Bybit USDT perps trading: {len(by_perp)}")
on_bn = {p for p, m in dcx_mkt.items() if m in bn_perp}
on_by = {p for p, m in dcx_mkt.items() if m in by_perp}
print(f"CoinDCX pairs with same-symbol Binance perp: {len(on_bn)} | Bybit: {len(on_by)} | neither: {len(dcx - on_bn - on_by)}")
print("  sample missing on Binance:", sorted(dcx - on_bn)[:12])
# CoinDCX rt feed "mkt" field mapping
prices = body("dcx.current_prices_rt")["prices"]
mk_mismatch = [(p, v.get("mkt")) for p, v in prices.items() if p in dcx and v.get("mkt") and v["mkt"] != dcx_mkt[p]]
print(f"rt prices entries: {len(prices)} | active pairs missing from rt: {len(dcx - set(prices))} | mkt != naive mapping: {len(mk_mismatch)} e.g. {mk_mismatch[:6]}")

print("\n=== CoinDCX current_prices rt (B-BTC_USDT)")
btc = prices["B-BTC_USDT"]
print(json.dumps(btc))
rt = body("dcx.current_prices_rt")
print("top-level ts age s:", age_s(rt["ts"]), "vs:", rt.get("vs"))
for k in ("btST", "bmST", "ctRT", "cmRT"):
    if k in btc:
        print(f"  {k} age s: {age_s(btc[k])}")
ages = [age_s(v["btST"]) for v in prices.values() if v.get("btST")]
print(f"  btST age across pairs: p50={statistics.median(ages)}s p95={sorted(ages)[int(len(ages)*.95)]}s max={max(ages)}s n={len(ages)}")
missing = {k: sum(1 for v in prices.values() if k not in v) for k in ("ls", "pc", "v", "efr", "fr", "mp", "btST", "bmST")}
print("  pairs missing field:", missing)

print("\n=== CoinDCX instrument B-BTC_USDT (selected)")
inst = body("dcx.instrument")["instrument"]
print({k: inst.get(k) for k in ("pair", "kind", "status", "funding_frequency", "unit_contract_value", "quote_currency_short_name", "settle_currency_short_name", "underlying_currency_short_name")})

print("\n=== CoinDCX candles")
for n in ("dcx.candles_1h_90d", "dcx.candles_1m_1d"):
    d = body(n)["data"]
    ts = [c["time"] for c in d]
    order = "desc" if ts[0] > ts[-1] else "asc"
    srt = sorted(ts)
    gaps = [b - a for a, b in zip(srt, srt[1:])]
    print(f"{n}: rows={len(d)} order={order} first={d[0]} span_h={(srt[-1]-srt[0])/3.6e6:.1f} latest_open_age_s={age_s(srt[-1])} step_ms_mode={statistics.mode(gaps)} max_gap_ms={max(gaps)} dups={len(ts)-len(set(ts))}")

print("\n=== CoinDCX pair stats B-BTC_USDT (unauthenticated GET)")
print(json.dumps(body("dcx.pair_stats")))

print("\n=== Binance")
pi = {x["symbol"]: x for x in body("bn.premiumIndex_all")}["BTCUSDT"]
print("premiumIndex BTC:", pi, "| time age s", age_s(pi["time"]))
fi = {x["symbol"]: x for x in body("bn.fundingInfo")}
print("fundingInfo entries (only adjusted-interval symbols):", len(fi), "BTC present:", "BTCUSDT" in fi)
fr = body("bn.fundingRate")
print("fundingRate last 3:", fr[-3:])
oi = body("bn.openInterest"); print("openInterest:", oi, "age s", age_s(oi["time"]))
oh = body("bn.oiHist_5m_300")
print("oiHist rows", len(oh), "first", oh[0]["timestamp"], "last", oh[-1], "last age s", age_s(oh[-1]["timestamp"]), "span_h", (oh[-1]["timestamp"]-oh[0]["timestamp"])/3.6e6)
for n in ("bn.globalLSAccount_5m", "bn.topLSPosition_5m", "bn.takerLS_5m"):
    b = body(n); print(n, b[-1], "age s", age_s(b[-1]["timestamp"]))
k = body("bn.klines_1h_1500"); print("klines rows", len(k), "last open age s", age_s(k[-1][0]), "last close_time in future?", k[-1][6] > now_ms)
t = {x["symbol"]: x for x in body("bn.ticker24h_all")}["BTCUSDT"]
print("ticker24h BTC lastPrice", t["lastPrice"], "pct", t["priceChangePercent"], "quoteVol", t["quoteVolume"], "closeTime age s", age_s(t["closeTime"]))

print("\n=== Bybit")
bt = {x["symbol"]: x for x in body("by.tickers_linear_all")["result"]["list"]}
print("tickers count (no symbol param):", len(bt))
b = bt["BTCUSDT"]
print({k: b.get(k) for k in ("lastPrice", "price24hPcnt", "turnover24h", "volume24h", "openInterest", "openInterestValue", "fundingRate", "nextFundingTime", "fundingIntervalHour", "markPrice")})
print("bybit response time field age s:", age_s(body("by.tickers_linear_all")["time"]))
oil = body("by.open_interest_5min")["result"]["list"]; print("open-interest rows", len(oil), "newest", oil[0], "age s", age_s(oil[0]["timestamp"]))
ar = body("by.account_ratio_5min")["result"]["list"]; print("account-ratio newest", ar[0], "age s", age_s(ar[0]["timestamp"]))
fh = body("by.funding_history")["result"]["list"]; print("funding history newest", fh[0])
kl = body("by.kline_60_1000")["result"]["list"]; print("kline rows", len(kl), "newest", kl[0], "age s", age_s(kl[0][0]))

print("\n=== Cross-venue BTC agreement")
print("price: dcx ls", btc.get("ls"), "| bn", t["lastPrice"], "| by", b["lastPrice"])
print("24h %: dcx pc", btc.get("pc"), "| bn", t["priceChangePercent"], "| by", float(b["price24hPcnt"]) * 100)
print("funding est: dcx efr", btc.get("efr"), "| bn lastFundingRate", pi["lastFundingRate"], "| by", b["fundingRate"])
print("last settled: dcx fr", btc.get("fr"), "| bn", fr[-1]["fundingRate"], "| by", fh[0]["fundingRate"])
print("24h vol (USDT): dcx v", btc.get("v"), "| bn quoteVolume", t["quoteVolume"], "| by turnover24h", b["turnover24h"])
