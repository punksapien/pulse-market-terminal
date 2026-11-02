"""ADR-0031: legacy generation model/validation and explicit-storage readers."""
import json
import math
import time
from datetime import datetime, timezone

from pulse.config import (GENERATION_KEY_PREFIX, ACTIVE_GENERATION_KEY, LAST_GOOD_GENERATION_KEY,
    PREVIOUS_GENERATION_KEY, REPORT_SCHEMA_VERSION, MODEL_VERSION, UI_COMBINATIONS,
    REPORT_REFRESH_AFTER, REPORT_MAX_STALE, MIN_GENERATED_ROWS, MAX_UNEXPECTED_ERROR_RATIO,
    MIN_PREVIOUS_ROW_RATIO)
from pulse.legacy.scoring import _parse_horizon_to_seconds


class ReportUnavailableError(RuntimeError):
    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def _epoch_seconds(value):
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return None


def _report_model_timestamp(report):
    meta = report.get("meta", {}) if isinstance(report, dict) else {}
    return (
        _epoch_seconds(meta.get("last_rebuilt"))
        or _epoch_seconds(meta.get("created_at_utc"))
    )


def _report_age_seconds(report, now=None):
    timestamp = _report_model_timestamp(report)
    if timestamp is None:
        return float("inf")
    return max(0.0, float(now or time.time()) - timestamp)


def _generation_manifest_key(generation_id):
    return f"{GENERATION_KEY_PREFIX}:{generation_id}:manifest"


def _generation_report_key(generation_id, cache_key):
    return f"{GENERATION_KEY_PREFIX}:{generation_id}:{cache_key}"


def _manifest_combinations(manifest):
    combinations = manifest.get("combinations", []) if isinstance(manifest, dict) else []
    return {
        (str(item.get("resolution")), str(item.get("horizon", "")).upper()): item
        for item in combinations
        if isinstance(item, dict)
    }


def _validate_manifest(manifest, generation_id):
    if not isinstance(manifest, dict):
        return False, "missing_manifest"
    if str(manifest.get("generation_id", "")) != str(generation_id):
        return False, "manifest_generation_mismatch"
    if str(manifest.get("schema_version", "")) != REPORT_SCHEMA_VERSION:
        return False, "manifest_schema_mismatch"
    if str(manifest.get("model_version", "")) != MODEL_VERSION:
        return False, "manifest_model_mismatch"
    if not _epoch_seconds(manifest.get("published_at")):
        return False, "manifest_missing_published_at"
    available = set(_manifest_combinations(manifest))
    if not set(UI_COMBINATIONS).issubset(available):
        return False, "manifest_incomplete"
    return True, "ok"


def _validate_generated_report(report, resolution, horizon):
    if not isinstance(report, dict):
        return False, "report_not_object"
    meta = report.get("meta")
    if not isinstance(meta, dict):
        return False, "missing_meta"
    if str(meta.get("resolution")) != str(resolution):
        return False, "resolution_mismatch"
    if str(meta.get("scale_horizon", "")).upper() != str(horizon).upper():
        return False, "horizon_mismatch"
    if meta.get("target_seconds") != _parse_horizon_to_seconds(horizon):
        return False, "target_seconds_mismatch"
    summary = report.get("summary")
    if not isinstance(summary, list) or not summary:
        return False, "empty_summary"
    instruments = [row.get("instrument") for row in summary if isinstance(row, dict)]
    if not instruments or any(not value for value in instruments):
        return False, "missing_instrument"
    if len(instruments) != len(set(instruments)):
        return False, "duplicate_instrument"
    for row in summary:
        if not isinstance(row, dict):
            return False, "invalid_summary_row"
        try:
            score = float(row.get("volatility_score"))
        except (TypeError, ValueError):
            return False, "missing_volatility_score"
        if not math.isfinite(score) or score < 0:
            return False, "invalid_volatility_score"
    return True, "ok"


def _serving_metadata(report, provenance, freshness):
    response = dict(report)
    response["meta"] = dict(report.get("meta", {}))
    response["meta"].update({
        "cache_status": provenance.get("source", "unknown"),
        "freshness_status": freshness,
        "generation_age_seconds": round(
            float(provenance.get("generation_age_seconds", 0.0)), 3
        ),
        "model_age_seconds": round(
            float(provenance.get("model_age_seconds", 0.0)), 3
        ),
        "freshness_slo_seconds": REPORT_REFRESH_AFTER,
        "hard_stale_limit_seconds": REPORT_MAX_STALE,
    })
    return response


def _validate_generation_set(generated_reports, previous_manifest=None, now=None):
    now = float(now or time.time())
    expected = set(UI_COMBINATIONS)
    actual = {(tf, hz) for tf, hz, _, _ in generated_reports}
    if actual != expected:
        return False, "combination_set_mismatch"

    previous = _manifest_combinations(previous_manifest)
    for tf, hz, _, report in generated_reports:
        valid, reason = _validate_generated_report(report, tf, hz)
        if not valid:
            return False, f"{tf}/{hz}:{reason}"
        rows = report.get("summary", [])
        errors = report.get("errors", {})
        if len(rows) < MIN_GENERATED_ROWS:
            return False, f"{tf}/{hz}:insufficient_rows:{len(rows)}"
        requested = len(rows) + len(errors)
        unexpected_errors = [
            error for error in errors.values()
            if "insufficient bars" not in str(error).lower()
        ]
        unexpected_error_ratio = (
            len(unexpected_errors) / requested if requested else 1.0
        )
        if unexpected_error_ratio > MAX_UNEXPECTED_ERROR_RATIO:
            return False, (
                f"{tf}/{hz}:unexpected_error_ratio:"
                f"{unexpected_error_ratio:.4f}"
            )
        instruments = {row.get("instrument") for row in rows}
        if not {"B-BTC_USDT", "B-ETH_USDT"}.issubset(instruments):
            return False, f"{tf}/{hz}:missing_reference_markets"
        model_age = _report_age_seconds(report, now=now)
        model_timestamp = _report_model_timestamp(report)
        if model_timestamp is None or model_timestamp > now + 5 * 60:
            return False, f"{tf}/{hz}:model_timestamp_invalid"
        if model_age > 15 * 60:
            return False, f"{tf}/{hz}:model_age:{model_age:.1f}"
        previous_rows = int(previous.get((tf, hz), {}).get("summary_rows", 0) or 0)
        if previous_rows and len(rows) < previous_rows * MIN_PREVIOUS_ROW_RATIO:
            return False, (
                f"{tf}/{hz}:row_count_regression:{len(rows)}/{previous_rows}"
            )
    return True, "ok"


def _load_json_redis(key, *, storage):
    VercelStorage = storage
    value = VercelStorage.get(key)
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception as exc:
        print(f"[REDIS] Invalid JSON at {key}: {exc}")
        return None


def _generation_candidate_ids(*, storage):
    VercelStorage = storage
    candidates = []
    for pointer_key in (
        ACTIVE_GENERATION_KEY,
        LAST_GOOD_GENERATION_KEY,
        PREVIOUS_GENERATION_KEY,
    ):
        value = VercelStorage.get(pointer_key)
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _read_generation_report(generation_id, cache_key, resolution, horizon, now=None, *, load_json):
    _load_json_redis = load_json
    now = float(now or time.time())
    manifest = _load_json_redis(_generation_manifest_key(generation_id))
    valid, reason = _validate_manifest(manifest, generation_id)
    if not valid:
        return None, {"generation_id": generation_id, "reason": reason}

    report = _load_json_redis(_generation_report_key(generation_id, cache_key))
    valid, reason = _validate_generated_report(report, resolution, horizon)
    if not valid:
        return None, {"generation_id": generation_id, "reason": reason}
    report_generation = str(report.get("meta", {}).get("generation_id", ""))
    if report_generation != str(generation_id):
        return None, {
            "generation_id": generation_id,
            "reason": "report_generation_mismatch",
        }

    published_at = _epoch_seconds(manifest.get("published_at"))
    model_timestamp = _report_model_timestamp(report)
    if published_at > now + 5 * 60:
        return None, {
            "generation_id": generation_id,
            "reason": "manifest_timestamp_in_future",
        }
    if model_timestamp is None or model_timestamp > now + 5 * 60:
        return None, {
            "generation_id": generation_id,
            "reason": "model_timestamp_invalid",
        }
    generation_age = max(0.0, now - published_at)
    model_age = max(0.0, now - model_timestamp)
    return report, {
        "generation_id": generation_id,
        "reason": "ok",
        "manifest": manifest,
        "generation_age_seconds": generation_age,
        "model_age_seconds": model_age,
        "effective_age_seconds": max(generation_age, model_age),
    }
