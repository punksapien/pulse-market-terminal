"""ADR-0035: legacy composition, compatibility exports and thin HTTP entry point.

Imported names remain available for existing API callers and call-time patches.
Canonical implementations live in focused legacy modules and pulse.api.handlers.
"""
import json
import os
import re
import sys
import tempfile
import time
import threading
import hashlib
import hmac
import math
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pulse.legacy.vol_core import VolatilityConfig, _now_utc, _resolution_seconds, _serialise, _finite  # noqa: E402
from pulse.legacy.scoring import (  # noqa: E402
    LWConfig,
    normalize_instruments_lw,
    run_lightweight_analysis,
    _parse_horizon_to_seconds,
    vol_signal_template,
    score_cross_section,
)
from frontend.render import render_volatility_document
from pulse.legacy.signal_oi import (  # noqa: E402
    fetch_oi_metrics,
    get_oi_provider_order,
    unavailable_oi_metrics,
)
from pulse.legacy.signal_engine import enrich_token, compute_market_context  # noqa: E402

from pulse.config import (
    UI_COMBINATIONS,
    ALLOWED_COMBINATIONS,
    REPORT_SCHEMA_VERSION,
    MODEL_VERSION,
    GENERATION_KEY_PREFIX,
    ACTIVE_GENERATION_KEY,
    PREVIOUS_GENERATION_KEY,
    LAST_GOOD_GENERATION_KEY,
    LAST_REFRESH_STATUS_KEY,
    REFRESH_BACKOFF_KEY,
    SYNC_LOCK_KEY,
    SYNC_LOCK_TTL,
    GENERATION_TTL,
    SIGNAL_STATE_KEY_PREFIX,
    SIGNAL_STATE_TTL,
    TRACKED_TRADE_SIGNALS,
    DISPLAYABLE_SIGNAL_STATES,
    GENERATION_REFRESH_AFTER,
    REPORT_REFRESH_AFTER,
    REPORT_MAX_STALE,
    REFRESH_FAILURE_BACKOFF,
    MIN_GENERATED_ROWS,
    MAX_UNEXPECTED_ERROR_RATIO,
    MIN_PREVIOUS_ROW_RATIO,
    CACHE_TTL,
    OI_CACHE_TTL,
    LOCK_KEY_PREFIX,
    LOCK_TTL,
)
from pulse.store.redis_store import VercelStorage
from pulse.api.http_utils import sanitize_utm_string, _first, _as_bool, _split_symbols
from pulse.api.auth import _bearer_token, _authorise_secret
from pulse.api import handlers as http_handlers

# ── In-memory + /tmp file cache for Vercel cold-start optimization ──
_CACHE = {"store": {}, "lock": threading.Lock()}

# ── OI Cache to prevent hitting Binance API 430+ times on concurrent/frequent refreshes ──
_OI_CACHE = {"store": {}, "ts": 0, "lock": threading.Lock()}


from pulse.legacy import cache as legacy_cache, generation as legacy_generation, publisher as legacy_publisher
from pulse.legacy import signal_state as legacy_signal_state, reader as legacy_reader, health as legacy_health
from pulse.legacy import scaling as legacy_scaling, enrichment as legacy_enrichment, pipeline as legacy_pipeline, fast_hydrate as legacy_fast_hydrate
from pulse.legacy import dispatch as legacy_dispatch, refresh as legacy_refresh
from pulse.legacy.signal_state import (
    _signal_state_key,
    _positive_finite_number,
    _matching_signal_state_fields,
    _signal_observations,
)
from pulse.legacy.cache import (
    _tmp_cache_path,
)
from pulse.legacy.generation import (
    ReportUnavailableError,
    _epoch_seconds,
    _report_model_timestamp,
    _report_age_seconds,
    _generation_manifest_key,
    _generation_report_key,
    _manifest_combinations,
    _validate_manifest,
    _validate_generated_report,
    _serving_metadata,
    _validate_generation_set,
)


def _default_start_date(resolution):
    seconds = _resolution_seconds(resolution)
    if seconds <= 60:
        days = 7
    elif seconds <= 5 * 60:
        days = 30
    elif seconds <= 60 * 60:
        days = 90
    elif seconds <= 4 * 60 * 60:
        days = 120
    else:
        days = 365
    return (_now_utc().date() - timedelta(days=days)).isoformat()


def _vol_signal_shell_template():
    """Shared shell; does not read reports or invoke collectors."""
    return render_volatility_document()


def scale_and_score_report(base_report, target_horizon, cfg):
    """
    In-memory re-scaling of standard deviations, sparklines, and timeseries charts,
    followed by cross-sectional scoring. Executed in <5ms.
    """
    return legacy_scaling.scale_and_score_report(
        base_report,
        target_horizon,
        cfg,
        score=score_cross_section,
    )


def _get_cached_oi(inst, market_symbol="", price_hint=None, session=None):
    return legacy_enrichment._get_cached_oi(
        inst,
        market_symbol,
        price_hint,
        session,
        cache_state=_OI_CACHE,
        fetch=fetch_oi_metrics,
        unavailable=unavailable_oi_metrics,
    )


def _enrich_with_signals(report, session=None):
    """Run signal engine over lightweight report: OI, regime, direction, signals."""
    return legacy_enrichment._enrich_with_signals(
        report,
        session,
        http=requests,
        get_oi=_get_cached_oi,
        classify=enrich_token,
        market_context=compute_market_context,
        provider_order=get_oi_provider_order,
        unavailable=unavailable_oi_metrics,
    )


def bootstrap_cache():
    """Copy packaged volatility_cache directory to /tmp to prevent cold start timeouts on serverless functions."""
    return legacy_cache.bootstrap_cache(
        root=ROOT,
    )


def load_precomputed_report(resolution):
    """Safely resolve and load the packaged precomputed report JSON for the given resolution."""
    return legacy_cache.load_precomputed_report(
        resolution,
        root=ROOT,
    )


def _try_serve_from_cache(cache_key, resolution, horizon):
    """Retrieve from L1 (memory) -> L2 (KV) -> L3 (/tmp). Returns report or None."""
    return legacy_cache._try_serve_from_cache(
        cache_key,
        resolution,
        horizon,
        cache_state=_CACHE,
        storage=VercelStorage,
        cache_ttl=CACHE_TTL,
        cache_path=_tmp_cache_path,
    )


def _serve_stale_or_fallback(cache_key, resolution, horizon):
    """Serve any stale cached value or fallback to the precomputed JSON."""
    return legacy_cache._serve_stale_or_fallback(
        cache_key,
        resolution,
        horizon,
        cache_state=_CACHE,
        storage=VercelStorage,
        cache_path=_tmp_cache_path,
        fallback=load_precomputed_report,
    )


def _save_to_local_cache_layers(cache_key, report):
    """Persist generated report into process memory and the instance-local /tmp."""
    return legacy_cache._save_to_local_cache_layers(
        cache_key,
        report,
        cache_state=_CACHE,
        cache_path=_tmp_cache_path,
    )


def _save_to_cache_layers(cache_key, report, resolution, horizon, ex=180):
    """Persist generated report across L1, L2, and L3 caches."""
    return legacy_cache._save_to_cache_layers(
        cache_key,
        report,
        resolution,
        horizon,
        ex,
        save_local=_save_to_local_cache_layers,
        storage=VercelStorage,
    )


def _load_json_redis(key):
    return legacy_generation._load_json_redis(
        key,
        storage=VercelStorage,
    )


def _generation_candidate_ids():
    return legacy_generation._generation_candidate_ids(
        storage=VercelStorage,
    )


def _read_generation_report(generation_id, cache_key, resolution, horizon, now=None):
    return legacy_generation._read_generation_report(
        generation_id,
        cache_key,
        resolution,
        horizon,
        now,
        load_json=_load_json_redis,
    )


def _reconcile_report_signal_states(
    report,
    resolution,
    horizon,
    source_at,
    generation_id,
    prune=False,
):
    return legacy_signal_state._reconcile_report_signal_states(
        report, resolution, horizon, source_at, generation_id, prune,
        storage=VercelStorage,
    )


def _best_effort_reconcile_generation_signal_states(
    generation_id,
    generated_reports,
    published_at,
):
    """Update auxiliary transition state without affecting report publication."""
    return legacy_signal_state._best_effort_reconcile_generation_signal_states(
        generation_id, generated_reports, published_at,
        reconcile_report=_reconcile_report_signal_states,
    )


def _attach_signal_states(report, resolution, horizon):
    """
    Add transition metadata to response rows without mutating immutable reports.

    A mismatched or malformed state is omitted instead of being guessed. This
    matters during the brief handoff from a newly published model generation to
    the first live-price reconciliation.
    """
    return legacy_signal_state._attach_signal_states(
        report, resolution, horizon,
        storage=VercelStorage,
    )


def _resolve_target_report(cache_key, resolution, horizon, allow_stale=False):
    """Resolve a validated, generation-consistent report for a public read."""
    return legacy_reader._resolve_target_report(
        cache_key, resolution, horizon, allow_stale,
        storage=VercelStorage,
        candidate_ids=_generation_candidate_ids,
        read_report=_read_generation_report,
        save_local=_save_to_local_cache_layers,
        attach_states=_attach_signal_states,
        fallback=load_precomputed_report,
    )


def _publish_generation(generation_id, generated_reports, trigger="unknown", started_at=None):
    return legacy_publisher._publish_generation(
        generation_id,
        generated_reports,
        trigger,
        started_at,
        storage=VercelStorage,
        save_local=_save_to_local_cache_layers,
        reconcile_signals=_best_effort_reconcile_generation_signal_states,
    )


def _record_refresh_status(state, trigger, **details):
    return legacy_publisher._record_refresh_status(
        state,
        trigger,
        details,
        storage=VercelStorage,
    )


def _set_refresh_backoff(reason, trigger):
    return legacy_publisher._set_refresh_backoff(
        reason,
        trigger,
        storage=VercelStorage,
    )


def _refresh_backoff_status():
    return legacy_publisher._refresh_backoff_status(
        storage=VercelStorage,
        load_json=_load_json_redis,
    )


def _generation_health_snapshot(verify_reports=True):
    return legacy_health._generation_health_snapshot(
        verify_reports,
        storage=VercelStorage,
        load_json=_load_json_redis,
        read_report=_read_generation_report,
    )


def _perform_optimized_pipeline(
    query,
    cache_key,
    tf,
    hz,
    margin,
    use_all,
    symbols,
    is_refresh,
    is_rebuild,
    persist_target=True,
):
    """
    Executes base timeframe candle fetching and calculations, scales YZ/EWMA/BVOL
    volatility metrics in-memory, scores the cross-section, and enriches the results.
    """
    return legacy_pipeline._perform_optimized_pipeline(
        query,
        cache_key,
        tf,
        hz,
        margin,
        use_all,
        symbols,
        is_refresh,
        is_rebuild,
        persist_target,
        bootstrap=bootstrap_cache,
        try_cache=_try_serve_from_cache,
        start_date=_default_start_date,
        analyse=run_lightweight_analysis,
        save=_save_to_cache_layers,
        fallback=load_precomputed_report,
        scale=scale_and_score_report,
        enrich=_enrich_with_signals,
    )


def _fast_hydrate(query, cache_key, resolution, horizon):
    """HOT refresh: V3 prices only -> signal reclassification -> tiny delta.
    Takes ~0.5-1.5s. No OI fetching. No candle fetching.
    """
    return legacy_fast_hydrate._fast_hydrate(
        query,
        cache_key,
        resolution,
        horizon,
        resolve_report=_resolve_target_report,
        http=requests,
        storage=VercelStorage,
    )


def _get_cached_or_compute(query):
    """Resolve public reads or execute an authorized, lock-protected rebuild."""
    return legacy_dispatch._get_cached_or_compute(
        query,
        hydrate=_fast_hydrate,
        resolve_report=_resolve_target_report,
        normalize=normalize_instruments_lw,
        storage=VercelStorage,
        stale_fallback=_serve_stale_or_fallback,
        try_cache=_try_serve_from_cache,
        build=_perform_optimized_pipeline,
    )


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload, headers=None):
        return http_handlers._send_json(
            self,
            status,
            payload,
            headers,
            serialise=_serialise,
        )

    def do_OPTIONS(self):
        return http_handlers.do_OPTIONS(
            self,
        )

    def warm_all_combinations(self, trigger="cron", force=False):
        """
        Build the three UI-supported combinations and publish them as one
        generation so readers never observe a partially refreshed set.
        """
        return legacy_refresh.warm_all_combinations(
            trigger,
            force,
            storage=VercelStorage,
            health_snapshot=_generation_health_snapshot,
            set_backoff=_set_refresh_backoff,
            record_status=_record_refresh_status,
            compute=_get_cached_or_compute,
            validate_report=_validate_generated_report,
            validate_set=_validate_generation_set,
            publish=_publish_generation,
        )

    def _get_report_with_auto_heal(self, query):
        return legacy_refresh._get_report_with_auto_heal(
            query,
            compute=_get_cached_or_compute,
            backoff_status=_refresh_backoff_status,
            warm=self.warm_all_combinations,
        )

    @staticmethod
    def _report_headers(report, heal_result=None):
        return http_handlers._report_headers(
            report,
            heal_result,
        )

    def do_GET(self):
        return http_handlers.do_GET(
            self,
            health_snapshot=_generation_health_snapshot,
            authorise=_authorise_secret,
            storage=VercelStorage,
            cache_state=_CACHE,
            shell=_vol_signal_shell_template,
            render=vol_signal_template,
        )

    def do_POST(self):
        return http_handlers.do_POST(
            self,
            sanitize=sanitize_utm_string,
            now_utc=_now_utc,
            storage=VercelStorage,
        )
