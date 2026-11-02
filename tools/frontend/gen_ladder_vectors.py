"""Generate shared ladder vectors from the Python reference (pulse.metrics.price_change).
Usage: PULSE_REPO=/path/with/pulse python tools/frontend/gen_ladder_vectors.py > tests/fixtures/pulse_price/ladder_vectors.json
The Python module is the oracle; the JS adapter must reproduce statuses, reasons and fractions."""
import json, os, sys
from decimal import Decimal
sys.path.insert(0, os.environ["PULSE_REPO"])
from pulse.metrics import price_change as pc

PAIR, MAP, DEF, GEN = "B-BTC_USDT", "m1", "price_change.v1", "g1"
MIN, W = 60000, {"1H": 3600000, "4H": 14400000, "24H": 86400000}
T0 = 1_800_000_000_000 // MIN * MIN
now = T0 + 30_000


def opens(missing=(), bad=()):
    out = {}
    for i in range(-24 * 60 - 10, 10):
        minute = T0 + i * MIN
        if minute in missing:
            continue
        price = Decimal(0) if minute in bad else Decimal("100") + Decimal(i) / Decimal(100)
        out[minute] = pc.CandleOpen(PAIR, minute, price)
    return out


def wire_ladder(ladder):
    def point(c):
        if c is None:
            return {"status": "unavailable", "reason": "reference_missing", "reference_minute_ms": None, "open_price": None}
        return {"status": "available", "reason": None, "reference_minute_ms": c.open_time_ms, "open_price": format(c.price, "f")}
    return {"status": "available", "reason": None, "mapping_version": ladder.mapping_version,
            "definition_version": ladder.definition_version, "generation_id": ladder.generation_id,
            "ladder": {"built_at_ms": ladder.built_at_ms, "start_ms": ladder.start_ms, "valid_until_ms": ladder.valid_until_ms,
                       "windows": ["1H", "4H", "24H"], "slots": [[point(c) for c in row] for row in ladder.slots]}}


def wire_fallback(frame, wire):
    refs = {}
    for h, c in zip(("1H", "4H", "24H"), frame.references):
        refs[h] = ({"status": "unavailable", "reason": "reference_missing", "reference_minute_ms": None, "open_price": None} if c is None else
                   {"status": "available", "reason": None, "reference_minute_ms": c.open_time_ms, "open_price": format(c.price, "f")})
    wire["fallback"] = {"source_ms": frame.tick.source_time_ms, "price": format(frame.tick.price, "f"), "expires_ms": None, "references": refs}
    return wire


def run(case_id, ladder_opens, tick_ms, price, now_ms, horizon, *, numerator_expires=None, gen=GEN):
    # a tick cannot be more than 5 s ahead of the clock; keep the clock inside the ladder unless the case says otherwise
    if now_ms is None:
        now_ms = min(max(tick_ms + 1000, T0 + 30_000), T0 + 5 * MIN - 1)
    ladder = pc.build_ladder(ladder_opens, pair_id=PAIR, mapping_version=MAP, definition_version=DEF, generation_id=GEN, now_ms=T0 + 30_000)
    refs = wire_ladder(ladder)
    tick = pc.PriceTick(PAIR, tick_ms, Decimal(price))
    expires = numerator_expires if numerator_expires is not None else tick_ms + 120_000
    result = pc.price_change(tick, ladder, horizon=horizon, mapping_version=MAP, definition_version=DEF, generation_id=gen,
                             numerator_expires_at_ms=expires, now_ms=now_ms)
    return {"id": case_id, "horizon": horizon, "tick": {"price": price, "source_ms": tick_ms, "expires_ms": expires}, "now_ms": now_ms,
            "generation_id": gen, "refs": refs,
            "expected": {"status": result.status, "reason": result.reason,
                         "fraction": None if result.value_fraction is None else format(result.value_fraction, "f"),
                         "delayed": result.delayed}}


cases = []
base = opens()
for h in W:
    for label, tick_ms in (("slot0_start", T0), ("slot0_end", T0 + MIN - 1), ("slot1_start", T0 + MIN), ("slot4_end", T0 + 5 * MIN - 1500),
                           ("before_ladder", T0 - 1), ("after_ladder", T0 + 5 * MIN)):
        cases.append(run(f"{label}_{h}", base, tick_ms, "101.25", T0 + 5 * MIN + 1 if label == "after_ladder" else None, h))
    cases.append(run(f"ladder_expired_{h}", base, T0 + 100, "101.25", T0 + 5 * MIN, h))
    cases.append(run(f"numerator_expired_{h}", base, T0 + 100, "101.25", None, h, numerator_expires=T0 + 30_000))
    cases.append(run(f"future_tick_{h}", base, now + 5001, "101.25", now, h))
    cases.append(run(f"tick_at_skew_limit_{h}", base, now + 5000, "101.25", now, h))
    cases.append(run(f"nonpositive_tick_{h}", base, T0 + 100, "0", None, h))
    cases.append(run(f"version_mismatch_{h}", base, T0 + 100, "101.25", None, h, gen="g2"))
    miss = T0 + 2 * MIN - W[h]
    cases.append(run(f"missing_minute_{h}", opens(missing=(miss,)), T0 + 2 * MIN + 10, "101.25", None, h))
    cases.append(run(f"other_slot_unaffected_{h}", opens(missing=(miss,)), T0 + 3 * MIN + 10, "101.25", None, h))
    cases.append(run(f"zero_reference_{h}", opens(bad=(miss,)), T0 + 2 * MIN + 10, "101.25", None, h))
    cases.append(run(f"rollover_{h}", base, T0 + MIN, "100.00", None, h))
json.dump({"contract": "ladder-price-change.v1", "bound_factor": "1e-12", "t0": T0, "cases": cases}, sys.stdout, indent=1)
