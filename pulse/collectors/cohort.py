"""ADR-0068 deterministic, coverage-checked cohort; pure source selection."""
import hashlib
import json
import random

CANARIES = ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "1000PEPEUSDT", "XAUUSDT")
CHANGED = ("ARKUSDT", "BMTUSDT", "IOSTUSDT", "IRYSUSDT", "ORDERUSDT", "TUSDT", "DOODUSDT")
COMMODITIES = ("CLUSDT", "COPPERUSDT", "NATGASUSDT", "XAGUSDT", "BZUSDT")


def select(active, ticker, exchange, *, now_ms, seed=20261026):
    if (type(active) is not list or type(ticker) is not dict
            or type(exchange) is not dict or type(exchange.get("symbols")) is not list):
        raise ValueError("cohort_source_shape")
    if len(active) != len(set(active)):
        raise ValueError("cohort_active_duplicates")
    contracts = {}
    for row in exchange["symbols"]:
        if (row.get("contractType") in ("PERPETUAL", "TRADIFI_PERPETUAL")
                and row.get("quoteAsset") == row.get("marginAsset") == "USDT"
                and row.get("status") == "TRADING"):
            symbol = row.get("symbol")
            if symbol in contracts:
                raise ValueError("cohort_exchange_duplicate")
            contracts[symbol] = row
    prices = ticker.get("prices", ticker)
    eligible = {}
    for pair in active:
        if not isinstance(pair, str) or not pair.startswith("B-") or not pair.endswith("_USDT"):
            continue
        symbol = pair[2:-5]+"USDT"
        rt = prices.get(pair)
        if symbol not in contracts or type(rt) is not dict:
            continue
        if rt.get("mkt") != symbol:
            raise ValueError("cohort_rt_mapping_mismatch")
        eligible[symbol] = {"symbol": symbol, "pair_id": pair,
                            "contract_type": contracts[symbol]["contractType"],
                            "onboard_ms": contracts[symbol].get("onboardDate")}
    required = list(CANARIES)+list(CHANGED)
    if any(symbol not in eligible for symbol in required):
        raise ValueError("cohort_required_pair_missing")
    commodities = [s for s in COMMODITIES if s in eligible]
    if len(commodities) < 2:
        raise ValueError("cohort_commodity_coverage_missing")
    required += commodities[:2]
    # Include the youngest eligible listing, without calling it <30d if it is not.
    youngest = max(eligible, key=lambda s: eligible[s]["onboard_ms"] or 0)
    if youngest not in required:
        required.append(youngest)
    remainder = sorted(set(eligible)-set(required))
    random.Random(seed).shuffle(remainder)
    chosen = required+remainder[:50-len(required)]
    if len(chosen) != 50 or len(set(chosen)) != 50:
        raise ValueError("cohort_universe_too_small")
    rows = [eligible[symbol] for symbol in chosen]
    body = {"definition": "cohort.v1", "seed": seed, "source_ms": now_ms,
            "eligible_count": len(eligible), "pairs": rows,
            "coverage": {"canaries": list(CANARIES), "changed": list(CHANGED),
                         "commodities": ["XAUUSDT"]+commodities[:2],
                         "youngest": youngest,
                         "youngest_age_ms": now_ms-(eligible[youngest]["onboard_ms"] or 0)}}
    body["digest"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body
