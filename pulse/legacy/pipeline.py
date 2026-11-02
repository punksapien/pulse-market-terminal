"""ADR-0033: model build orchestration with explicit cache and compute callbacks."""
import os
import tempfile
import time

from pulse.api.http_utils import _as_bool, _first
from pulse.config import CACHE_TTL, REPORT_SCHEMA_VERSION, MODEL_VERSION
from pulse.legacy.vol_core import VolatilityConfig, _serialise
from pulse.legacy.scoring import LWConfig, normalize_instruments_lw
from pulse.legacy.generation import _report_age_seconds


def _perform_optimized_pipeline(
    query, cache_key, tf, hz, margin, use_all, symbols,
    is_refresh, is_rebuild, persist_target=True, *,
    bootstrap, try_cache, start_date, analyse, save, fallback, scale, enrich,
):
    """
    Executes base timeframe candle fetching and calculations, scales YZ/EWMA/BVOL
    volatility metrics in-memory, scores the cross-section, and enriches the results.
    """
    bootstrap_cache = bootstrap
    _try_serve_from_cache = try_cache
    _default_start_date = start_date
    run_lightweight_analysis = analyse
    _save_to_cache_layers = save
    load_precomputed_report = fallback
    scale_and_score_report = scale
    _enrich_with_signals = enrich
    now = time.time()
    bootstrap_cache()

    # Step 1: Resolve the base timeframe report
    base_hz = "24H" if tf == "1D" else ("4H" if tf == "240" else "1H")
    base_cache_key = f"base_{margin}_{tf}_{use_all}_{','.join(sorted(symbols))}"
    base_report = _try_serve_from_cache(base_cache_key, tf, base_hz)
    base_age = _report_age_seconds(base_report, now=now) if base_report else float("inf")
    
    is_vercel = os.environ.get("VERCEL") == "1"
    is_cron = _as_bool(_first(query, "_cron", "0"), False)

    # Rebuild base candles only if stale (1 hour) or requested (rebuild), and (not on Vercel OR is a cron request)
    if (not base_report or base_age > 3600 or is_rebuild) and (not is_vercel or is_cron):
        try:
            instruments = None if use_all else normalize_instruments_lw(symbols, margin)
            
            base_cfg = VolatilityConfig(
                margin_currency=margin,
                resolution=tf,
                start_date=_default_start_date(tf),
                end_date=None,
                min_bars=80,
                cache_dir=os.path.join(tempfile.gettempdir(), "volatility_cache"),
                refresh_cache=True,
                cache_raw=True,
                request_sleep=0.01,
                request_timeout=20,
                retries=2,
                max_chart_points=260,
            )
            cfg = LWConfig(
                base=base_cfg,
                scale_horizon=base_hz,  # Base scale horizon matched to tf
                bvol_window_hours=24.0,
                daily_volume_days=7,
                min_volume_usd=0.0,
            )
            base_report = run_lightweight_analysis(cfg, instruments=instruments, max_instruments=None)
            base_report["meta"]["last_rebuilt"] = time.time()
            
            # Cache base report
            _save_to_cache_layers(base_cache_key, base_report, tf, base_hz, ex=600)
        except Exception as e:
            print(f"[PIPELINE Rebuild Base Failed]: {e}")
            if not base_report:
                if is_vercel and is_cron:
                    raise RuntimeError(
                        f"fresh base rebuild failed for {tf}/{base_hz}"
                    ) from e
                base_report = load_precomputed_report(tf)
    else:
        # On Vercel, or if cached base report is fresh, we do not rebuild.
        # Load fallback if base_report is missing
        if not base_report:
            base_report = load_precomputed_report(tf)

    if not base_report:
        raise RuntimeError(f"no base report available for {tf}/{base_hz}")
    base_age = _report_age_seconds(base_report)
    if is_cron and base_age > 15 * 60:
        raise RuntimeError(
            f"refusing stale base report for {tf}/{base_hz}: {base_age:.1f}s old"
        )

    # Step 2: Scale volatility scores and score the cross-section
    base_cfg = VolatilityConfig(margin_currency=margin, resolution=tf, cache_dir=os.path.join(tempfile.gettempdir(), "volatility_cache"))
    cfg = LWConfig(base=base_cfg, scale_horizon=hz)
    report = scale_and_score_report(base_report, hz, cfg)

    # Step 3: Enrich with shared CoinDCX prices & Binance OI
    report = _enrich_with_signals(report)
    report["meta"]["served_by"] = "vercel_optimized_scale_pipeline"
    report["meta"]["last_hydrated"] = time.time()
    report["meta"]["schema_version"] = REPORT_SCHEMA_VERSION
    report["meta"]["model_version"] = MODEL_VERSION
    generation_id = str(_first(query, "_generation_id", "") or "")
    if generation_id:
        report["meta"]["generation_id"] = generation_id

    serialised = _serialise(report)
    
    # Strip timeseries and duplicate keys to optimize payload size for ultra-low-latency transport
    if isinstance(serialised, dict):
        if "instruments" in serialised:
            for k, v in serialised["instruments"].items():
                if isinstance(v, dict):
                    v.pop("timeseries", None)
                    v.pop("oi_history", None)
                    
        if "summary" in serialised:
            for item in serialised["summary"]:
                if isinstance(item, dict):
                    item.pop("timeseries", None)
                    item.pop("oi_history", None)
                    
        # Remove common duplicate keys from instruments to avoid repeating data
        if "instruments" in serialised and "summary" in serialised and len(serialised["summary"]) > 0:
            sample_summary = serialised["summary"][0]
            if isinstance(sample_summary, dict):
                sum_keys = set(sample_summary.keys())
                keys_to_remove = sum_keys - {"instrument"}
                for k, v in serialised["instruments"].items():
                    if isinstance(v, dict):
                        for key in keys_to_remove:
                            v.pop(key, None)
    
    # Scheduled synchronization stages target reports and publishes them together.
    if persist_target:
        _save_to_cache_layers(cache_key, serialised, tf, hz, ex=CACHE_TTL)
    return serialised

