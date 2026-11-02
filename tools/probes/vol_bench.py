"""Benchmark the existing volatility calculation (read-only import of repo code).

Fetches candles for N pairs x 3 public combinations, then times analyse_instrument_lw
(compute only, network excluded) and score_cross_section. Cache goes to scratch.
"""
import os
import statistics
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)
sys.dont_write_bytecode = True

import requests  # noqa: E402

import pulse.legacy.scoring as lw  # noqa: E402
from pulse.legacy.vol_core import VolatilityConfig  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 15
CACHE = os.path.join(__import__("tempfile").gettempdir(), "pulse_vol_cache")  # outside the repo
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-DOGE_USDT", "B-XRP_USDT", "B-PEPE_USDT", "B-BNB_USDT",
         "B-ADA_USDT", "B-LINK_USDT", "B-AVAX_USDT", "B-SUI_USDT", "B-TRX_USDT", "B-LTC_USDT", "B-DOT_USDT",
         "B-NEAR_USDT", "B-APT_USDT", "B-ARB_USDT", "B-OP_USDT", "B-WIF_USDT", "B-TIA_USDT"][:N]
COMBOS = [("60", "1H", 90), ("240", "4H", 120), ("1D", "24H", 365)]
session = requests.Session()

for resolution, horizon, days in COMBOS:
    base = VolatilityConfig(resolution=resolution, cache_dir=CACHE,
                            start_date=time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400)))
    cfg = lw.LWConfig(base=base, scale_horizon=horizon)
    fetch_ms, comp_ms, rows, results = [], [], [], {}
    for pair in PAIRS:
        t0 = time.perf_counter()
        df = lw.fetch_candles_data(pair, base, session=session)
        fetch_ms.append((time.perf_counter() - t0) * 1000)
        rows.append(len(df))
        t0 = time.perf_counter()
        res = lw.analyse_instrument_lw(pair, df, cfg)
        comp_ms.append((time.perf_counter() - t0) * 1000)
        results[pair] = res
    t0 = time.perf_counter()
    lw.score_cross_section(results, cfg)
    xs_ms = (time.perf_counter() - t0) * 1000
    errs = sum(1 for r in results.values() if r.get("error"))
    print(f"{resolution:>3}/{horizon:<3} pairs={len(PAIRS)} bars/pair median={statistics.median(rows):.0f} "
          f"fetch ms p50={statistics.median(fetch_ms):.0f} | analyse ms p50={statistics.median(comp_ms):.1f} "
          f"max={max(comp_ms):.1f} | cross_section ms={xs_ms:.1f} | errors={errs}")
    print(f"        -> extrapolated compute for 500 pairs: {statistics.median(comp_ms) * 500 / 1000:.1f} s single-threaded")
