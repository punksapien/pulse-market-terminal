import requests
params = {
    "pair": "B-BTC_USDT",
    "resolution": "240",
    "from": 1715000000,
    "to": 1715100000,
    "pcode": "f"
}
r = requests.get("https://public.coindcx.com/market_data/candles", params=params)
print(r.status_code)
print(r.json()[:2] if isinstance(r.json(), list) else r.json())
