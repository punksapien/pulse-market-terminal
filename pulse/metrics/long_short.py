"""ADR-0050: pure accounts-ratio/share metrics; never exposure estimates."""

from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, Underflow, localcontext

from pulse.observation import MAX_FUTURE_SKEW_MS, MAX_SOURCE_TIME_MS

WINDOW_MS = {"1H": 3600000, "4H": 14400000, "24H": 86400000}
BASELINE_TOLERANCE_MS = {"own": 60000, "cold-start": 300000}
SERIES_TTL_MS = {"global": 900000, "top": 2100000}
SUM_TOLERANCE = Decimal("0.0002")
RATIO_REL_TOLERANCE = Decimal("0.005")
MAX_HISTORY = 320


def _context():
    context = Context(prec=34, rounding=ROUND_HALF_EVEN)
    context.traps[Underflow] = True
    return context


@dataclass(frozen=True)
class LSObservation:
    pair_id: str
    series: str
    bucket_ms: int
    ratio: Decimal
    long_share: Decimal
    short_share: Decimal


@dataclass(frozen=True)
class LSMetric:
    status: str
    reason: str | None
    value: Decimal | None
    unit: str
    pair_id: str | None = None
    series: str | None = None
    asof_ms: int | None = None
    comparison_asof_ms: int | None = None
    expires_at_ms: int | None = None
    source_age_ms: int | None = None
    actual_duration_ms: int | None = None
    baseline_error_ms: int | None = None
    mode: str | None = None
    direction: str | None = None
    level_basis: Decimal | None = None
    classification_status: str = "uncalibrated"


def _clock(now_ms):
    if type(now_ms) is not int or not 0 <= now_ms <= MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_observation_clock")


def _decimal(value):
    return isinstance(value, Decimal) and value.is_finite()


def _validate(observation, now_ms, *, fresh, positive_ratio=True):
    if observation is None:
        return "missing_observation"
    if not isinstance(observation, LSObservation):
        return "malformed_observation"
    if not isinstance(observation.pair_id, str) or not 0 < len(observation.pair_id) <= 64:
        return "invalid_pair"
    if not isinstance(observation.series, str) or observation.series not in SERIES_TTL_MS:
        return "invalid_series"
    if type(observation.bucket_ms) is not int or not 0 <= observation.bucket_ms <= MAX_SOURCE_TIME_MS or observation.bucket_ms % 300000:
        return "invalid_bucket"
    if observation.bucket_ms > now_ms + MAX_FUTURE_SKEW_MS:
        return "future_bucket"
    if not all(_decimal(value) and 0 <= value <= 1 for value in (observation.long_share, observation.short_share)):
        return "invalid_share"
    if observation.short_share == 0:
        return "zero_short_share"
    if not _decimal(observation.ratio) or observation.ratio < 0 or (positive_ratio and observation.ratio == 0):
        return "invalid_ratio"
    try:
        with localcontext(_context()):
            if abs(observation.long_share + observation.short_share - 1) > SUM_TOLERANCE:
                return "share_sum_mismatch"
            expected = observation.long_share / observation.short_share
            if abs(observation.ratio - expected) > RATIO_REL_TOLERANCE * abs(expected):
                return "ratio_share_mismatch"
    except DecimalException:
        return "arithmetic_unavailable"
    if fresh and now_ms >= observation.bucket_ms + SERIES_TTL_MS[observation.series]:
        return "expired"
    return None


def _unavailable(reason, unit, mode=None):
    return LSMetric("unavailable", reason, None, unit, mode=mode)


def ratio_metric(observation, *, now_ms):
    _clock(now_ms)
    reason = _validate(observation, now_ms, fresh=True)
    if reason:
        return _unavailable(reason, "ratio")
    try:
        with localcontext(_context()) as context:
            basis = abs(observation.ratio.ln(context=context))
    except DecimalException:
        return _unavailable("arithmetic_unavailable", "ratio")
    direction = "long_dominant" if observation.ratio > 1 else "short_dominant" if observation.ratio < 1 else "balanced"
    return LSMetric("available", None, observation.ratio, "ratio", pair_id=observation.pair_id,
                    series=observation.series, asof_ms=observation.bucket_ms,
                    expires_at_ms=observation.bucket_ms + SERIES_TTL_MS[observation.series],
                    source_age_ms=now_ms - observation.bucket_ms, direction=direction, level_basis=basis)


def share_evolution(current, baseline, *, horizon, mode, now_ms):
    _clock(now_ms)
    if horizon not in WINDOW_MS or mode not in BASELINE_TOLERANCE_MS:
        raise ValueError("invalid_horizon_or_mode")
    for item, fresh in ((current, True), (baseline, False)):
        reason = _validate(item, now_ms, fresh=fresh, positive_ratio=False)
        if reason:
            return _unavailable(reason, "pp", mode)
    if (current.pair_id, current.series) != (baseline.pair_id, baseline.series):
        return _unavailable("identity_mismatch", "pp", mode)
    duration = current.bucket_ms - baseline.bucket_ms
    error = duration - WINDOW_MS[horizon]
    if error < 0 or error > BASELINE_TOLERANCE_MS[mode]:
        return _unavailable("baseline_outside_tolerance", "pp", mode)
    with localcontext(_context()):
        value = 100 * (current.long_share - baseline.long_share)
    return LSMetric("available", None, value, "pp", pair_id=current.pair_id, series=current.series,
                    asof_ms=current.bucket_ms, comparison_asof_ms=baseline.bucket_ms,
                    expires_at_ms=current.bucket_ms + SERIES_TTL_MS[current.series], source_age_ms=now_ms - current.bucket_ms,
                    actual_duration_ms=duration, baseline_error_ms=error, mode=mode,
                    direction="increasing" if value > 0 else "decreasing" if value < 0 else "unchanged")


def recent_divergence_slice(history, *, now_ms):
    """ADR-0051: retain candidate buckets and their duplicates, not old payloads."""
    _clock(now_ms)
    if not isinstance(history, (tuple, list)) or len(history) > MAX_HISTORY:
        raise ValueError("history_bound_exceeded")
    return tuple(row for row in history if isinstance(row, LSObservation)
                 and type(row.bucket_ms) is int
                 and now_ms - 900000 < row.bucket_ms <= now_ms + MAX_FUTURE_SKEW_MS)


def common_bucket_divergence(global_history, top_history, *, pair_id, now_ms):
    _clock(now_ms)
    if not isinstance(pair_id, str) or not pair_id or len(pair_id) > 64:
        raise ValueError("invalid_pair")
    indexes = []
    for history, series in ((global_history, "global"), (top_history, "top")):
        if not isinstance(history, (tuple, list)) or len(history) > MAX_HISTORY:
            return _unavailable("history_bound_exceeded", "pp")
        index = {}
        for observation in history:
            if not isinstance(observation, LSObservation) or type(observation.bucket_ms) is not int:
                return _unavailable("malformed_observation", "pp")
            index.setdefault(observation.bucket_ms, []).append(observation)
        indexes.append(index)
    shared = indexes[0].keys() & indexes[1].keys()
    if not shared:
        return _unavailable("no_common_bucket", "pp")
    eligible = [bucket for bucket in shared if now_ms < bucket + min(SERIES_TTL_MS.values())]
    if not eligible:
        return _unavailable("no_unexpired_common_bucket", "pp")
    bucket = max(eligible)
    points = []
    for index, series in zip(indexes, ("global", "top")):
        rows = index[bucket]
        if any(row != rows[0] for row in rows[1:]):
            return _unavailable("conflicting_duplicate", "pp")
        point = rows[0]
        reason = _validate(point, now_ms, fresh=True, positive_ratio=False)
        if reason:
            return _unavailable(reason, "pp")
        if (point.pair_id, point.series) != (pair_id, series):
            return _unavailable("identity_mismatch", "pp")
        points.append(point)
    global_point, top_point = points
    with localcontext(_context()):
        difference = 100 * (top_point.long_share - global_point.long_share)
    return LSMetric("available", None, difference, "pp", pair_id=pair_id, series="top-minus-global",
                    asof_ms=bucket, comparison_asof_ms=bucket,
                    expires_at_ms=bucket + min(SERIES_TTL_MS.values()), source_age_ms=now_ms - bucket,
                    direction="top_more_long" if difference > 0 else "top_more_short" if difference < 0 else "equal")
