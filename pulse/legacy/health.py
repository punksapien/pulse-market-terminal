"""ADR-0032: generation health with injected storage and report readers."""
import time

from pulse.config import (ACTIVE_GENERATION_KEY, LAST_GOOD_GENERATION_KEY, PREVIOUS_GENERATION_KEY,
    LAST_REFRESH_STATUS_KEY, REFRESH_BACKOFF_KEY, UI_COMBINATIONS, REPORT_REFRESH_AFTER, REPORT_MAX_STALE)
from pulse.legacy.generation import _generation_manifest_key, _validate_manifest, _epoch_seconds


def _generation_health_snapshot(verify_reports=True, *, storage, load_json, read_report):
    VercelStorage = storage
    _load_json_redis = load_json
    _read_generation_report = read_report
    now = time.time()
    redis_available = VercelStorage.is_available()
    snapshot = {
        "redis_available": redis_available,
        "active_generation": None,
        "selected_generation": None,
        "pointer_source": None,
        "manifest": None,
        "generation_age_seconds": None,
        "max_model_age_seconds": None,
        "freshness_status": "unavailable",
        "healthy": False,
        "complete": False,
        "combinations": [],
        "reasons": [],
        "last_refresh": None,
        "refresh_backoff": None,
    }
    if not redis_available:
        snapshot["reasons"].append("redis_unavailable")
        return snapshot

    pointers = {
        "active": VercelStorage.get(ACTIVE_GENERATION_KEY),
        "last_good": VercelStorage.get(LAST_GOOD_GENERATION_KEY),
        "previous": VercelStorage.get(PREVIOUS_GENERATION_KEY),
    }
    snapshot["active_generation"] = pointers["active"]
    snapshot["last_refresh"] = _load_json_redis(LAST_REFRESH_STATUS_KEY)
    snapshot["refresh_backoff"] = _load_json_redis(REFRESH_BACKOFF_KEY)

    valid_manifests = []
    seen = set()
    for source, generation_id in pointers.items():
        if not generation_id or generation_id in seen:
            continue
        seen.add(generation_id)
        manifest = _load_json_redis(_generation_manifest_key(generation_id))
        valid, reason = _validate_manifest(manifest, generation_id)
        if not valid:
            snapshot["reasons"].append(f"{source}:{reason}")
            continue
        published_at = _epoch_seconds(manifest.get("published_at"))
        valid_manifests.append((published_at, source, generation_id, manifest))

    if not valid_manifests:
        snapshot["reasons"].append("no_valid_generation_manifest")
        return snapshot

    _, source, generation_id, manifest = max(valid_manifests, key=lambda item: item[0])
    generation_age = max(0.0, now - _epoch_seconds(manifest.get("published_at")))
    snapshot.update({
        "selected_generation": generation_id,
        "pointer_source": source,
        "manifest": manifest,
        "generation_age_seconds": round(generation_age, 3),
    })

    report_ages = []
    complete = True
    if verify_reports:
        for tf, hz in UI_COMBINATIONS:
            cache_key = f"USDT_{tf}_{hz}_True_"
            report, provenance = _read_generation_report(
                generation_id,
                cache_key,
                tf,
                hz,
                now=now,
            )
            combination = {
                "resolution": tf,
                "horizon": hz,
                "ok": report is not None,
                "reason": provenance.get("reason"),
                "model_age_seconds": provenance.get("model_age_seconds"),
            }
            snapshot["combinations"].append(combination)
            if report is None:
                complete = False
                snapshot["reasons"].append(f"{tf}/{hz}:{provenance.get('reason')}")
            else:
                report_ages.append(provenance["model_age_seconds"])

    max_model_age = max(report_ages) if report_ages else generation_age
    effective_age = max(generation_age, max_model_age)
    snapshot["complete"] = complete
    snapshot["max_model_age_seconds"] = round(max_model_age, 3)
    if not complete:
        snapshot["freshness_status"] = "unavailable"
    elif effective_age <= REPORT_REFRESH_AFTER:
        snapshot["freshness_status"] = "fresh"
        snapshot["healthy"] = source == "active"
        if source != "active":
            snapshot["reasons"].append("active_pointer_recovery_required")
    elif effective_age <= REPORT_MAX_STALE:
        snapshot["freshness_status"] = "stale_recoverable"
        snapshot["reasons"].append("generation_refresh_overdue")
    else:
        snapshot["freshness_status"] = "expired"
        snapshot["reasons"].append("generation_hard_stale_limit_exceeded")
    return snapshot

