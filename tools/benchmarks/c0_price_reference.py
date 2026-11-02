"""ADR-0048: offline semantic builder timing, not snapshot/CDN/Redis throughput."""

import json
import math
import platform
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pulse.metrics.price_change import CandleOpen, PriceTick, WINDOW_MS, build_fallback, build_ladder, fallback_change, price_change


START = 1792980000000
VERSIONS = dict(mapping_version="synthetic-m1", definition_version="price_change.v1", generation_id="synthetic-g1")


def summary(times):
    ordered = sorted(times)
    return {"n": len(times), "p50_ms": round(statistics.median(times) / 1e6, 3),
            "sample_p95_ms": round(ordered[math.ceil(.95 * len(times)) - 1] / 1e6, 3)}


def measure(count, slots):
    inputs = []
    for i in range(count):
        pair = f"SYNTHETIC{i}USDT"
        opens = {START + offset * 60000 - window: CandleOpen(pair, START + offset * 60000 - window,
                  Decimal(100000000 + i * 12345 + offset * 123) / Decimal(100000000))
                 for offset in range(-1, 8) for window in WINDOW_MS.values()}
        tick = PriceTick(pair, START + 60000, Decimal("1.25"))
        fallback = PriceTick(pair, START - 1, Decimal("1.24"))
        inputs.append((pair, opens, tick, fallback))
    builders, reads = [], []
    for _ in range(10):
        start = time.perf_counter_ns()
        frames = [(build_ladder(opens, pair_id=pair, now_ms=START + 59000, slots=slots, **VERSIONS),
                   build_fallback(opens, old, now_ms=START + 59000, **VERSIONS)) for pair, opens, tick, old in inputs]
        builders.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        for (pair, _, tick, _), (ladder, fallback) in zip(inputs, frames):
            for horizon in WINDOW_MS:
                live = price_change(tick, ladder, horizon=horizon, now_ms=START + 60000,
                                    numerator_expires_at_ms=START + 240000, **VERSIONS)
                old = fallback_change(fallback, pair_id=pair, horizon=horizon, now_ms=START + 60000,
                                      expires_at_ms=START + 240000, **VERSIONS)
                assert live.status == old.status == "available"
        reads.append(time.perf_counter_ns() - start)
    return {"pairs": count, "slots": slots, "ladder_plus_fallback_build": summary(builders),
            "evaluate_all_live_and_fallback_horizons": summary(reads),
            "evaluations_per_round": count * 6}


if __name__ == "__main__":
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                      "synthetic": True, "results": [measure(n, s) for n in (50, 503) for s in (5, 8)]}, indent=2))
