"""ADR-0032: validated target selection without HTTP or concrete storage imports."""
import math
import os
import time

from pulse.config import ACTIVE_GENERATION_KEY, LAST_GOOD_GENERATION_KEY, REPORT_MAX_STALE, REPORT_REFRESH_AFTER
from pulse.legacy.generation import _serving_metadata, _validate_generated_report, _report_age_seconds


def _resolve_target_report(
    cache_key, resolution, horizon, allow_stale=False, *,
    storage, candidate_ids, read_report, save_local, attach_states, fallback,
):
    """Resolve a validated, generation-consistent report for a public read."""
    VercelStorage = storage
    _generation_candidate_ids = candidate_ids
    _read_generation_report = read_report
    _save_to_local_cache_layers = save_local
    _attach_signal_states = attach_states
    load_precomputed_report = fallback
    now = time.time()
    limit = REPORT_MAX_STALE if allow_stale else REPORT_REFRESH_AFTER
    diagnostics = []
    redis_available = VercelStorage.is_available()

    if redis_available:
        active_generation = VercelStorage.get(ACTIVE_GENERATION_KEY)
        valid_candidates = []
        for generation_id in _generation_candidate_ids():
            report, provenance = _read_generation_report(
                generation_id,
                cache_key,
                resolution,
                horizon,
                now=now,
            )
            diagnostics.append(provenance)
            if report is not None:
                valid_candidates.append((report, provenance))

        valid_candidates.sort(
            key=lambda item: item[1].get("effective_age_seconds", float("inf"))
        )
        for report, provenance in valid_candidates:
            age = provenance["effective_age_seconds"]
            if age > limit:
                continue
            generation_id = provenance["generation_id"]
            if generation_id != active_generation:
                # Pointer-only recovery is safe because the immutable generation
                # and complete manifest were validated before this write.
                VercelStorage.set_many_atomic({
                    ACTIVE_GENERATION_KEY: (generation_id, None),
                    LAST_GOOD_GENERATION_KEY: (generation_id, None),
                })
                provenance["source"] = "recovered_generation"
            else:
                provenance["source"] = "active_generation"
            _save_to_local_cache_layers(cache_key, report)
            freshness = "fresh" if age <= REPORT_REFRESH_AFTER else "stale_revalidating"
            response = _serving_metadata(report, provenance, freshness)
            return _attach_signal_states(response, resolution, horizon), provenance

    # Local development remains usable without Redis. Production may only use a
    # packaged report while it is genuinely within the same hard freshness bound.
    fallback = load_precomputed_report(resolution)
    valid, fallback_reason = _validate_generated_report(fallback, resolution, horizon)
    fallback_age = _report_age_seconds(fallback, now=now) if valid else float("inf")
    is_local = os.environ.get("VERCEL") != "1"
    if valid and (is_local or fallback_age <= limit):
        provenance = {
            "source": "packaged_fallback",
            "generation_id": fallback.get("meta", {}).get("generation_id"),
            "generation_age_seconds": fallback_age,
            "model_age_seconds": fallback_age,
            "effective_age_seconds": fallback_age,
        }
        freshness = "fresh" if fallback_age <= REPORT_REFRESH_AFTER else "stale_revalidating"
        response = _serving_metadata(fallback, provenance, freshness)
        return _attach_signal_states(response, resolution, horizon), provenance

    return None, {
        "reason": "no_acceptable_generation",
        "redis_available": redis_available,
        "allow_stale": allow_stale,
        "candidates": diagnostics,
        "fallback_reason": fallback_reason,
        "fallback_age_seconds": fallback_age if math.isfinite(fallback_age) else None,
    }

