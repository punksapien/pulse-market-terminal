"""ADR-0010/0046: quantity simple return with explicit baseline provenance."""

import math
from dataclasses import dataclass

from pulse.observation import MAX_FUTURE_SKEW_MS, MAX_SOURCE_TIME_MS

WINDOW_MS = {"1H": 3600000, "4H": 14400000, "24H": 86400000}
BASELINE_TOLERANCE_MS = {"own": 60000, "cold-start": 300000}
SOURCE_MAX_AGE_MS = 900000


@dataclass(frozen=True)
class OIObservation:
    source_time_ms: int
    quantity: float
    pair_id: str


@dataclass(frozen=True)
class OIChange:
    status: str
    reason: str | None
    value_fraction: float | None
    direction: str | None
    mode: str
    asof_ms: int | None
    baseline_asof_ms: int | None
    actual_duration_ms: int | None
    baseline_error_ms: int | None
    pair_id: str | None
    classification_status: str = "uncalibrated"


def quantity_change(current, baseline, *, horizon, mode, now_ms):
    """Inputs must be same-contract quantity observations selected by the caller."""
    if horizon not in WINDOW_MS or mode not in BASELINE_TOLERANCE_MS:
        raise ValueError("unsupported_horizon_or_mode")
    if type(now_ms) is not int or not 0 <= now_ms <= MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_observation_clock")
    asof = baseline_asof = duration = error = None

    def unavailable(reason):
        return OIChange("unavailable", reason, None, None, mode, asof, baseline_asof, duration, error, None)

    for name, observation in (("current", current), ("baseline", baseline)):
        if observation is None:
            return unavailable(name + "_missing")
        if not isinstance(observation, OIObservation) or type(observation.source_time_ms) is not int or not 0 <= observation.source_time_ms <= MAX_SOURCE_TIME_MS:
            return unavailable(name + "_malformed")
        if type(observation.quantity) is not float or not math.isfinite(observation.quantity) or observation.quantity < 0:
            return unavailable(name + "_invalid_quantity")
        if not isinstance(observation.pair_id, str) or not observation.pair_id or len(observation.pair_id) > 64:
            return unavailable(name + "_invalid_pair")
    if current.pair_id != baseline.pair_id:
        return unavailable("pair_mismatch")
    asof, baseline_asof = current.source_time_ms, baseline.source_time_ms
    if asof > now_ms + MAX_FUTURE_SKEW_MS:
        return unavailable("current_future")
    if now_ms - asof > SOURCE_MAX_AGE_MS:
        return unavailable("current_stale")
    target = asof - WINDOW_MS[horizon]
    duration = asof - baseline_asof
    error = target - baseline_asof
    if error < 0:
        return unavailable("baseline_after_target")
    if error > BASELINE_TOLERANCE_MS[mode]:
        return unavailable("baseline_outside_tolerance")
    if baseline.quantity == 0:
        return unavailable("zero_baseline")
    value = (current.quantity - baseline.quantity) / baseline.quantity
    if not math.isfinite(value):
        return unavailable("nonfinite_result")
    direction = "increasing" if value > 0 else "decreasing" if value < 0 else "unchanged"
    return OIChange("available", None, value, direction, mode, asof, baseline_asof, duration, error, current.pair_id)
