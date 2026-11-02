"""Poll CoinDCX pair stats every 30 s for 15 min to find the upstream L/S update cadence."""
import json, time
import net
s = net.session()
pairs = ["B-BTC_USDT", "B-ETH_USDT", "B-DOGE_USDT", "B-SOL_USDT", "B-PEPE_USDT"]
last, changes = {}, {p: [] for p in pairs}
end = time.time() + 900
while time.time() < end:
    for p in pairs:
        try:
            pos = json.dumps(s.get("https://api.coindcx.com/api/v1/derivatives/futures/data/stats", params={"pair": p}, timeout=10).json()["position"])
        except Exception as exc:
            pos = f"err {exc!r}"
        if p in last and pos != last[p]:
            changes[p].append(time.strftime("%H:%M:%S"))
        last[p] = pos
    time.sleep(30)
json.dump(changes, open("ls_cadence.json", "w"), indent=1)
print(json.dumps(changes, indent=1))
