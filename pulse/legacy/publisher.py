"""ADR-0031: legacy publish/backoff ordering with explicit storage and callbacks."""
import os
import time

from pulse.config import (ACTIVE_GENERATION_KEY, LAST_GOOD_GENERATION_KEY, PREVIOUS_GENERATION_KEY,
    REPORT_SCHEMA_VERSION, MODEL_VERSION, GENERATION_TTL, CACHE_TTL, LAST_REFRESH_STATUS_KEY,
    REFRESH_BACKOFF_KEY, REFRESH_FAILURE_BACKOFF)
from pulse.legacy.generation import _generation_manifest_key, _generation_report_key, _epoch_seconds


def _publish_generation(generation_id, generated_reports, trigger='unknown', started_at=None, *, storage, save_local, reconcile_signals):
    VercelStorage = storage
    _save_to_local_cache_layers = save_local
    _best_effort_reconcile_generation_signal_states = reconcile_signals
    now = time.time()
    started_at = float(started_at or now)
    for _, _, _, report in generated_reports:
        meta = report.setdefault("meta", {})
        meta["generation_id"] = generation_id
        meta["generation_published_at"] = now
        meta["schema_version"] = REPORT_SCHEMA_VERSION
        meta["model_version"] = MODEL_VERSION
    manifest = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generation_id": generation_id,
        "published_at": now,
        "started_at": started_at,
        "build_elapsed_seconds": round(now - started_at, 3),
        "trigger": trigger,
        "deployment_git_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA"),
        "combinations": [
            {
                "resolution": tf,
                "horizon": hz,
                "cache_key": cache_key,
                "summary_rows": len(report.get("summary", [])),
                "error_count": len(report.get("errors", {})),
                "oi_coverage_ratio": report.get("meta", {}).get("oi_coverage_ratio"),
            }
            for tf, hz, cache_key, report in generated_reports
        ],
    }

    redis_available = VercelStorage.is_available()
    if redis_available:
        previous_generation = VercelStorage.get(ACTIVE_GENERATION_KEY)
        entries = {
            ACTIVE_GENERATION_KEY: (generation_id, None),
            LAST_GOOD_GENERATION_KEY: (generation_id, None),
            _generation_manifest_key(generation_id): (
                manifest,
                GENERATION_TTL,
            ),
        }
        if previous_generation and previous_generation != generation_id:
            entries[PREVIOUS_GENERATION_KEY] = (
                previous_generation,
                None,
            )
        for _, _, cache_key, report in generated_reports:
            entries[cache_key] = (report, CACHE_TTL)
            entries[_generation_report_key(generation_id, cache_key)] = (
                report,
                GENERATION_TTL,
            )
        if not VercelStorage.set_many_atomic(entries):
            return False, manifest, "redis_atomic_publish_failed"
    elif os.environ.get("VERCEL") == "1":
        return False, manifest, "redis_unavailable"

    # Update local cache only after the durable Redis transaction succeeds, so
    # concurrent readers see either the previous complete generation or the new one.
    for _, _, cache_key, report in generated_reports:
        _save_to_local_cache_layers(cache_key, report)
    _save_to_local_cache_layers(
        _generation_manifest_key(generation_id),
        manifest,
    )
    if redis_available:
        # This state is deliberately downstream of the complete durable publish
        # and local-cache update. It is an optional UI enhancement and therefore
        # cannot make generation publication or request auto-heal fail.
        _best_effort_reconcile_generation_signal_states(
            generation_id,
            generated_reports,
            published_at=now,
        )
    return True, manifest, "published" if redis_available else "local_only"


def _record_refresh_status(state, trigger, details, *, storage):
    VercelStorage = storage
    payload = {
        "state": state,
        "trigger": trigger,
        "updated_at": time.time(),
        "deployment_git_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA"),
    }
    payload.update(details)
    if VercelStorage.is_available():
        VercelStorage.set(
            LAST_REFRESH_STATUS_KEY,
            payload,
            ex=GENERATION_TTL,
        )
    return payload


def _set_refresh_backoff(reason, trigger, *, storage):
    VercelStorage = storage
    payload = {
        "reason": str(reason)[:500],
        "trigger": trigger,
        "created_at": time.time(),
        "retry_after": time.time() + REFRESH_FAILURE_BACKOFF,
    }
    if VercelStorage.is_available():
        VercelStorage.set(
            REFRESH_BACKOFF_KEY,
            payload,
            ex=REFRESH_FAILURE_BACKOFF,
        )
    return payload


def _refresh_backoff_status(*, storage, load_json):
    VercelStorage = storage
    _load_json_redis = load_json
    if not VercelStorage.is_available():
        return None
    payload = _load_json_redis(REFRESH_BACKOFF_KEY)
    if not isinstance(payload, dict):
        return None
    retry_after = _epoch_seconds(payload.get("retry_after"))
    if not retry_after or retry_after <= time.time():
        return None
    return payload
