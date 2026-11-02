"""ADR-0034: refresh coordination independent of HTTP handler instances."""
import os
import time
import uuid

from pulse.config import (UI_COMBINATIONS, GENERATION_REFRESH_AFTER, ACTIVE_GENERATION_KEY,
    LAST_GOOD_GENERATION_KEY, SYNC_LOCK_KEY, SYNC_LOCK_TTL, REFRESH_BACKOFF_KEY)
from pulse.legacy.generation import ReportUnavailableError


def warm_all_combinations(
    trigger='cron', force=False, *, storage, health_snapshot, set_backoff, record_status, compute, validate_report, validate_set, publish,
):
    """
        Build the three UI-supported combinations and publish them as one
        generation so readers never observe a partially refreshed set.
        """
    VercelStorage = storage
    _generation_health_snapshot = health_snapshot
    _set_refresh_backoff = set_backoff
    _record_refresh_status = record_status
    _get_cached_or_compute = compute
    _validate_generated_report = validate_report
    _validate_generation_set = validate_set
    _publish_generation = publish
    started_at = time.time()
    generation_id = f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
    lock_value = uuid.uuid4().hex
    redis_available = VercelStorage.is_available()

    if os.environ.get("VERCEL") == "1" and not redis_available:
        return {
            "status": "redis_unavailable",
            "generation_id": generation_id,
            "published": False,
            "trigger": trigger,
            "combinations": [],
        }

    before = _generation_health_snapshot(verify_reports=True)
    before_age = max(
        float(before["generation_age_seconds"])
        if before.get("generation_age_seconds") is not None else float("inf"),
        float(before["max_model_age_seconds"])
        if before.get("max_model_age_seconds") is not None else float("inf"),
    )
    if not force and before.get("complete") and before_age < GENERATION_REFRESH_AFTER:
        selected = before.get("selected_generation")
        if selected and before.get("pointer_source") != "active":
            VercelStorage.set_many_atomic({
                ACTIVE_GENERATION_KEY: (selected, None),
                LAST_GOOD_GENERATION_KEY: (selected, None),
            })
        return {
            "status": "fresh_noop",
            "generation_id": selected,
            "published": False,
            "skipped": True,
            "trigger": trigger,
            "generation_age_seconds": round(before_age, 3),
            "combinations": before.get("combinations", []),
        }

    lock_acquired = not redis_available

    if redis_available:
        lock_acquired = VercelStorage.setnx(
            SYNC_LOCK_KEY,
            lock_value,
            ex=SYNC_LOCK_TTL,
        )
    if not lock_acquired:
        return {
            "status": "locked",
            "generation_id": generation_id,
            "published": False,
            "trigger": trigger,
            "combinations": [],
        }

    print(
        f"[WARMER] Building generation {generation_id} "
        f"for {len(UI_COMBINATIONS)} UI combinations (trigger={trigger})..."
    )
    generated_reports = []
    results = []

    def failure(status, reason):
        _set_refresh_backoff(reason, trigger)
        _record_refresh_status(
            "failed",
            trigger,
            generation_id=generation_id,
            reason=str(reason)[:500],
            status=status,
            elapsed_seconds=round(time.time() - started_at, 3),
            combinations=results,
        )
        return {
            "status": status,
            "generation_id": generation_id,
            "published": False,
            "trigger": trigger,
            "reason": str(reason),
            "combinations": results,
        }

    try:
        # Close the race between the first freshness check and lock acquisition.
        after_lock = _generation_health_snapshot(verify_reports=True)
        after_lock_age = max(
            float(after_lock["generation_age_seconds"])
            if after_lock.get("generation_age_seconds") is not None else float("inf"),
            float(after_lock["max_model_age_seconds"])
            if after_lock.get("max_model_age_seconds") is not None else float("inf"),
        )
        if (
            not force
            and after_lock.get("complete")
            and after_lock_age < GENERATION_REFRESH_AFTER
        ):
            return {
                "status": "fresh_noop",
                "generation_id": after_lock.get("selected_generation"),
                "published": False,
                "skipped": True,
                "trigger": trigger,
                "generation_age_seconds": round(after_lock_age, 3),
                "combinations": after_lock.get("combinations", []),
            }

        previous_manifest = after_lock.get("manifest") or before.get("manifest")
        _record_refresh_status(
            "running",
            trigger,
            generation_id=generation_id,
            started_at=started_at,
        )
        for tf, hz in UI_COMBINATIONS:
            cache_key = f"USDT_{tf}_{hz}_True_"
            query = {
                "timeframe": [tf],
                "horizon": [hz],
                "all": ["1"],
                "margin_currency": ["USDT"],
                "refresh": ["1"],
                "_cron": ["1"],
                "_stage_only": ["1"],
                "_sync_locked": ["1"],
                "_generation_id": [generation_id],
            }
            started = time.time()
            try:
                report = _get_cached_or_compute(query)
                valid, reason = _validate_generated_report(report, tf, hz)
                result = {
                    "resolution": tf,
                    "horizon": hz,
                    "ok": valid,
                    "reason": reason,
                    "elapsed_seconds": round(time.time() - started, 3),
                    "summary_rows": len(report.get("summary", []))
                    if isinstance(report, dict) else 0,
                    "error_count": len(report.get("errors", {}))
                    if isinstance(report, dict) else 0,
                }
                results.append(result)
                if not valid:
                    return failure("validation_failed", f"{tf}/{hz}:{reason}")
                generated_reports.append((tf, hz, cache_key, report))
            except Exception as exc:
                results.append({
                    "resolution": tf,
                    "horizon": hz,
                    "ok": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": round(time.time() - started, 3),
                    "summary_rows": 0,
                    "error_count": 0,
                })
                return failure(
                    "generation_failed",
                    f"{type(exc).__name__}: {exc}",
                )

        valid_set, validation_reason = _validate_generation_set(
            generated_reports,
            previous_manifest=previous_manifest,
        )
        if not valid_set:
            return failure("generation_validation_failed", validation_reason)

        published, manifest, reason = _publish_generation(
            generation_id,
            generated_reports,
            trigger=trigger,
            started_at=started_at,
        )
        if not published:
            return failure("publish_failed", reason)

        if redis_available:
            VercelStorage.delete(REFRESH_BACKOFF_KEY)
        _record_refresh_status(
            "succeeded",
            trigger,
            generation_id=generation_id,
            published_at=manifest.get("published_at"),
            elapsed_seconds=round(time.time() - started_at, 3),
            combinations=results,
        )
        return {
            "status": "ok" if published else "publish_failed",
            "generation_id": generation_id,
            "published": published,
            "trigger": trigger,
            "publish_reason": reason,
            "manifest": manifest,
            "combinations": results,
        }
    except Exception as exc:
        return failure(
            "warmer_unhandled_failure",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        if redis_available:
            VercelStorage.delete_if_value(SYNC_LOCK_KEY, lock_value)


def _get_report_with_auto_heal(
    query, *, compute, backoff_status, warm,
):
    _get_cached_or_compute = compute
    _refresh_backoff_status = backoff_status
    _warm = warm
    try:
        report = _get_cached_or_compute(query)
        return report, None
    except ReportUnavailableError as initial_error:
        backoff = _refresh_backoff_status()
        heal_result = None
        if backoff is None:
            heal_result = _warm(
                trigger="request_autoheal",
                force=True,
            )
            if (
                heal_result.get("published")
                or heal_result.get("status") == "fresh_noop"
            ):
                report = _get_cached_or_compute(query)
                report["meta"] = dict(report.get("meta", {}))
                report["meta"]["auto_heal"] = {
                    "status": "recovered",
                    "trigger": "request_autoheal",
                    "generation_id": heal_result.get("generation_id"),
                }
                return report, heal_result

        # While another repair owns the lock, or during a short failure
        # backoff, permit only data inside the explicit hard stale limit.
        stale_query = {key: list(value) for key, value in query.items()}
        stale_query["_allow_stale"] = ["1"]
        try:
            report = _get_cached_or_compute(stale_query)
            report["meta"] = dict(report.get("meta", {}))
            report["meta"]["auto_heal"] = {
                "status": (
                    heal_result.get("status")
                    if isinstance(heal_result, dict)
                    else "backoff"
                ),
                "trigger": "request_autoheal",
                "retry_after": backoff.get("retry_after") if backoff else None,
            }
            return report, heal_result or {"status": "backoff"}
        except ReportUnavailableError as stale_error:
            raise ReportUnavailableError(
                "volatility_generation_repair_failed",
                {
                    "initial": initial_error.details,
                    "stale": stale_error.details,
                    "auto_heal": heal_result,
                    "backoff": backoff,
                },
            ) from stale_error

