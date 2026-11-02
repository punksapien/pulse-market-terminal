"""ADR-0034: legacy read/compute dispatch with explicit storage and callbacks."""
import os
import uuid

from pulse.api.http_utils import _first, _as_bool, _split_symbols
from pulse.config import LOCK_KEY_PREFIX, LOCK_TTL
from pulse.legacy.generation import ReportUnavailableError


def _get_cached_or_compute(
    query, *, hydrate, resolve_report, normalize, storage, stale_fallback, try_cache, build,
):
    """Resolve public reads or execute an authorized, lock-protected rebuild."""
    _fast_hydrate = hydrate
    _resolve_target_report = resolve_report
    normalize_instruments_lw = normalize
    VercelStorage = storage
    _serve_stale_or_fallback = stale_fallback
    _try_serve_from_cache = try_cache
    _perform_optimized_pipeline = build

    margin = str(_first(query, "margin_currency", "USDT")).upper()
    resolution = str(_first(query, "timeframe", _first(query, "resolution", "60")))
    _raw_hz = str(_first(query, "horizon", _first(query, "scale_horizon", "1H")))
    horizon = _raw_hz.upper() if _raw_hz[-1:].lower() not in ('m',) else _raw_hz
    use_all = _as_bool(_first(query, "all", "1"), True)
    symbols = _split_symbols(query)

    is_refresh = _as_bool(_first(query, "refresh", "0"), False) or _as_bool(_first(query, "refresh_cache", "0"), False)
    is_rebuild = _as_bool(_first(query, "rebuild", "0"), False)
    is_fast = _as_bool(_first(query, "fast", "0"), False)
    is_cron = _as_bool(_first(query, "_cron", "0"), False)
    is_stage_only = _as_bool(_first(query, "_stage_only", "0"), False)
    is_sync_locked = _as_bool(_first(query, "_sync_locked", "0"), False)
    allow_stale = _as_bool(_first(query, "_allow_stale", "0"), False)

    cache_key = f"{margin}_{resolution}_{horizon}_{use_all}_{','.join(sorted(symbols))}"
    generation_cache_key = f"{margin}_{resolution}_{horizon}_True_"

    if is_fast:
        return _fast_hydrate(
            query,
            generation_cache_key,
            resolution,
            horizon,
        )

    # 1. Public requests read only complete, atomically published generations.
    # Missing or overdue data is surfaced to the HTTP handler so exactly one
    # bounded repair can run under the global distributed lock.
    if not is_cron:
        report, diagnostics = _resolve_target_report(
            generation_cache_key,
            resolution,
            horizon,
            allow_stale=allow_stale,
        )
        if report is None:
            raise ReportUnavailableError(
                "volatility_generation_unavailable",
                diagnostics,
            )

        if symbols:
            requested = set(normalize_instruments_lw(symbols, margin) or [])
            filtered = dict(report)
            filtered["summary"] = [
                row for row in report.get("summary", [])
                if row.get("instrument") in requested
            ]
            filtered["instruments"] = {
                inst: info for inst, info in report.get("instruments", {}).items()
                if inst in requested
            }
            return filtered
        return report

    # 2. Cron requests are allowed to compute and update the cache
    # Try to acquire the distributed lock to avoid thundering herd
    lock_key = f"{LOCK_KEY_PREFIX}{cache_key}"
    lock_acquired = False
    lock_value = uuid.uuid4().hex
    if is_sync_locked:
        lock_acquired = True
    elif VercelStorage.is_available():
        try:
            lock_acquired = VercelStorage.setnx(
                lock_key,
                lock_value,
                ex=LOCK_TTL,
            )
        except Exception as e:
            print(f"[LOCK] setnx failed: {e}")
            lock_acquired = True  # Fallback to local concurrency if KV fails

    if not is_sync_locked and VercelStorage.is_available() and not lock_acquired:
        print(f"[LOCK CONCURRENCY] Lock occupied for {lock_key}.")
        if os.environ.get("VERCEL") == "1":
            raise ReportUnavailableError(
                "generation_lock_occupied",
                {"lock_key": lock_key},
            )
        return _serve_stale_or_fallback(cache_key, resolution, horizon)

    try:
        # Re-check cache inside lock in case someone else computed it while we were waiting
        if not is_refresh and not is_rebuild:
            cached = _try_serve_from_cache(cache_key, resolution, horizon)
            if cached:
                return cached

        report = _perform_optimized_pipeline(
            query,
            cache_key,
            resolution,
            horizon,
            margin,
            use_all,
            symbols,
            is_refresh,
            is_rebuild,
            persist_target=not is_stage_only,
        )
    finally:
        if not is_sync_locked and VercelStorage.is_available() and lock_acquired:
            try:
                VercelStorage.delete_if_value(lock_key, lock_value)
            except Exception as e:
                print(f"[LOCK] Failed to release lock {lock_key}: {e}")

    return report

