"""ADR-0050: offline pure metric CPU/elapsed diagnostic; no collector or Redis."""

import json
import math
import platform
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pulse.metrics.long_short import LSObservation, common_bucket_divergence, ratio_metric, share_evolution

NOW = 1792980000000


def summary(times):
    return {"n": len(times), "p50_ms": round(statistics.median(times) / 1e6, 3),
            "sample_p95_ms": round(sorted(times)[math.ceil(.95 * len(times)) - 1] / 1e6, 3)}


def measure(evaluations, history_size):
    # Repeat a single preloaded fixture to isolate function work; not a universe/RSS test.
    global_history = [LSObservation("BTCUSDT", "global", NOW - i * 300000, Decimal("1.5"), Decimal("0.6"), Decimal("0.4")) for i in range(history_size)]
    top_history = [LSObservation("BTCUSDT", "top", NOW - i * 300000, Decimal("2.3333"), Decimal("0.7"), Decimal("0.3")) for i in range(history_size)]
    baseline = LSObservation("BTCUSDT", "global", NOW - 3600000, Decimal("1.5"), Decimal("0.6"), Decimal("0.4"))
    ratio_times, evolution_times, common_times = [], [], []
    for _ in range(3):
        start = time.perf_counter_ns()
        for _ in range(evaluations):
            assert ratio_metric(global_history[0], now_ms=NOW).status == "available"
        ratio_times.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        for _ in range(evaluations):
            assert share_evolution(global_history[0], baseline, horizon="1H", mode="own", now_ms=NOW).value == 0
        evolution_times.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        for _ in range(evaluations):
            assert common_bucket_divergence(global_history, top_history, pair_id="BTCUSDT", now_ms=NOW).value == 10
        common_times.append(time.perf_counter_ns() - start)
    return {"logical_pair_evaluations": evaluations, "history_per_series": history_size,
            "ratio": summary(ratio_times), "evolution": summary(evolution_times), "common_bucket": summary(common_times)}


if __name__ == "__main__":
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                      "synthetic_repeated_fixture": True, "results": [measure(n, s) for n in (50, 503) for s in (4, 320)]}, indent=2))
