"""ADR-0041 R1–R9 / ADR-0051. Pure, bounded settlement verification.

Rates are Decimal fractions; bps are a presentation projection. No network/store.
Adapters must establish source identities before constructing these observations.
"""

from dataclasses import dataclass, replace
from decimal import Context, Decimal, localcontext

from pulse.observation import MAX_FUTURE_SKEW_MS, MAX_SOURCE_TIME_MS

HOUR = 3600000
GRACE = 900000
FRESH = 120000
JITTER = 5000
MAX_EVENTS = 512
BPS = Decimal(10000)


@dataclass(frozen=True)
class Settlement:
    pair_id: str
    time_ms: int
    rate: Decimal
    rate_type: str = "Regular"


@dataclass(frozen=True)
class FundingMetadata:
    pair_id: str
    mapping_version: str
    coindcx_hours: int
    binance_hours: int | None  # None means symbol absent from a successfully fetched fundingInfo.
    next_funding_ms: int
    settled_rate: Decimal
    source_ms: int
    cap: Decimal | None = None
    floor: Decimal | None = None


@dataclass(frozen=True)
class FundingEstimate:
    pair_id: str
    mapping_version: str
    hours: int
    next_funding_ms: int
    settled_rate: Decimal
    rate: Decimal
    source_ms: int


@dataclass(frozen=True)
class FundingValue:
    status: str
    reason: str | None = None
    fraction: Decimal | None = None
    asof_ms: int | None = None
    expires_at_ms: int | None = None
    assurance: str | None = None
    level_basis_bps: Decimal | None = None
    classification_status: str = "uncalibrated"

    @property
    def bps(self):
        if self.fraction is None:
            return None
        with localcontext(Context(prec=60)):
            return self.fraction * BPS

    @property
    def direction(self):
        if self.fraction is None:
            return None
        return "increasing" if self.fraction > 0 else "decreasing" if self.fraction < 0 else "unchanged"


@dataclass(frozen=True)
class FundingReport:
    metadata: FundingMetadata
    latest: Settlement | None
    last: FundingValue
    cumulative_1d: FundingValue
    cumulative_7d: FundingValue
    delta_1d: FundingValue
    delta_7d: FundingValue
    excluded_special: int = 0
    excluded_unknown: int = 0
    verified_at_ms: int | None = None


def _clock(value):
    return type(value) is int and 0 <= value <= MAX_SOURCE_TIME_MS


def _rate(value):
    # Bounds on provider precision/resource use, NOT a clamp to economic values.
    return (isinstance(value, Decimal) and value.is_finite()
            and len(value.as_tuple().digits) <= 28 and -28 <= value.as_tuple().exponent <= 0)


def _bad(reason):
    return FundingValue("settlement_pending" if reason == "settlement_pending" else "unavailable", reason)


def _metadata_reason(meta, now_ms):
    if not isinstance(meta, FundingMetadata):
        return "invalid_metadata"
    # ADR-0087: public sha256: mapping tokens are 71 chars; schema bound is128.
    if not isinstance(meta.pair_id, str) or not 0 < len(meta.pair_id) <= 64 or not isinstance(meta.mapping_version, str) or not 0 < len(meta.mapping_version) <= 128:
        return "invalid_identity"
    if type(meta.coindcx_hours) is not int or meta.coindcx_hours not in (1, 4, 8):
        return "interval_unsupported"
    if meta.binance_hours is not None and (type(meta.binance_hours) is not int or meta.binance_hours not in (1, 4, 8)):
        return "interval_unsupported"
    if meta.coindcx_hours != (8 if meta.binance_hours is None else meta.binance_hours):
        return "interval_sources_disagree"
    if not _clock(meta.source_ms) or not _clock(meta.next_funding_ms) or not _rate(meta.settled_rate):
        return "invalid_metadata"
    if meta.source_ms > now_ms + MAX_FUTURE_SKEW_MS:
        return "future_metadata"
    if now_ms - meta.source_ms >= FRESH:
        return "stale_metadata"
    return None


def settled_report(events, metadata, *, now_ms, audit_mismatch=False):
    """A per-pair bounded event slice; retrieval completeness is an adapter gate."""
    if not _clock(now_ms):
        raise ValueError("invalid_observation_clock")
    if type(audit_mismatch) is not bool:
        raise ValueError("invalid_audit_status")

    def failed(reason, special=0, unknown=0):
        return FundingReport(metadata, None, *(_bad(reason) for _ in range(5)), special, unknown)

    reason = _metadata_reason(metadata, now_ms)
    if reason:
        return failed(reason)
    if not isinstance(events, (tuple, list)) or len(events) > MAX_EVENTS:
        return failed("history_bound_exceeded")
    regular, unknown_times, seen = [], [], {}
    special = unknown = 0
    for event in events:
        if not isinstance(event, Settlement) or event.pair_id != metadata.pair_id:
            return failed("identity_mismatch")
        if not _clock(event.time_ms) or event.time_ms > now_ms + MAX_FUTURE_SKEW_MS or not _rate(event.rate) or not isinstance(event.rate_type, str):
            return failed("invalid_event")
        identity = (event.time_ms, event.rate_type)
        if identity in seen:
            if seen[identity] != event:
                return failed("conflicting_duplicate")
            continue
        seen[identity] = event
        if event.rate_type == "Regular":
            regular.append(event)
        elif event.rate_type == "Special":
            special += 1
        else:
            unknown += 1
            unknown_times.append(event.time_ms)
    regular.sort(key=lambda event: event.time_ms)
    if not regular:
        return failed("insufficient_history", special, unknown)
    latest = regular[-1]
    if any(stamp >= latest.time_ms for stamp in unknown_times):
        return failed("unknown_rate_type", special, unknown)
    step = metadata.coindcx_hours * HOUR
    due, expiry = latest.time_ms + step, latest.time_ms + step + GRACE
    if now_ms >= expiry:
        return failed("expired", special, unknown)
    # A newly due settlement must never pair an old settled operand with new efr.
    if now_ms >= due:
        return failed("settlement_pending", special, unknown)
    if abs(due - metadata.next_funding_ms) > JITTER or metadata.settled_rate != latest.rate:
        return failed("latest_unverified", special, unknown)

    def field(count, *, cumulative=False, level=False):
        if cumulative and audit_mismatch:
            return _bad("audit_mismatch")
        if len(regular) < count:
            return _bad("insufficient_history")
        selected = regular[-count:]
        if any(selected[0].time_ms <= stamp <= latest.time_ms for stamp in unknown_times):
            return _bad("unknown_rate_type")
        if any(abs(b.time_ms - a.time_ms - step) > JITTER for a, b in zip(selected, selected[1:])):
            return _bad("incomplete_or_interval_changed_in_window")
        with localcontext(Context(prec=60)):
            value = sum((event.rate for event in selected), Decimal(0)) if cumulative else latest.rate - selected[0].rate
            value = Decimal(0) if value == 0 else value
            basis = abs(value * BPS * 8 / metadata.coindcx_hours) if level else None
        return FundingValue("available", fraction=value, asof_ms=latest.time_ms, expires_at_ms=expiry,
                            assurance="window_consistent_provisional" if cumulative else "consecutive_verified",
                            level_basis_bps=basis)

    n1, n7 = 24 // metadata.coindcx_hours, 168 // metadata.coindcx_hours
    return FundingReport(metadata, latest, field(2, level=True), field(n1, cumulative=True),
                         field(n7, cumulative=True), field(n1 + 1), field(n7 + 1), special, unknown, now_ms)


def _verified_report_reason(report, now_ms):
    # ADR-0055: freshness gates verification, not the lifetime of its cached result.
    if not isinstance(report, FundingReport) or report.latest is None or not _clock(report.verified_at_ms):
        return "settlement_unverified"
    if report.verified_at_ms > now_ms + MAX_FUTURE_SKEW_MS:
        return "future_verification"
    reason = _metadata_reason(report.metadata, report.verified_at_ms)
    if reason:
        return reason
    latest, meta = report.latest, report.metadata
    if (not isinstance(latest, Settlement) or latest.pair_id != meta.pair_id
        or latest.rate_type != "Regular" or not _clock(latest.time_ms) or not _rate(latest.rate)
        or latest.time_ms > report.verified_at_ms + MAX_FUTURE_SKEW_MS):
        return "settlement_unverified"
    due = latest.time_ms + meta.coindcx_hours * HOUR
    if (abs(due - meta.next_funding_ms) > JITTER or latest.rate != meta.settled_rate
        or report.verified_at_ms >= due):
        return "settlement_unverified"
    if now_ms >= due + GRACE:
        return "expired"
    if now_ms >= due:
        return "settlement_pending"
    return None


def settled_view(report, *, now_ms):
    """Read retained verification without extending its source or R6 expiry."""
    if not _clock(now_ms):
        raise ValueError("invalid_observation_clock")
    if not isinstance(report, FundingReport):
        raise ValueError("invalid_report")
    reason = _verified_report_reason(report, now_ms)
    if reason is None:
        return report
    values = {}
    for field_name in ("last", "cumulative_1d", "cumulative_7d", "delta_1d", "delta_7d"):
        value = getattr(report, field_name)
        if value.status == "available":
            values[field_name] = replace(value, status="settlement_pending" if reason == "settlement_pending" else "unavailable",
                                         reason=reason, fraction=None, level_basis_bps=None)
    return replace(report, **values)


def current_delta(estimate, report, *, now_ms):
    """Unclassified; both documents must describe the same native funding cycle."""
    if not _clock(now_ms):
        raise ValueError("invalid_observation_clock")
    reason = _verified_report_reason(report, now_ms)
    if reason:
        return _bad(reason)
    meta = report.metadata
    if not isinstance(estimate, FundingEstimate) or not _clock(estimate.source_ms) or not _clock(estimate.next_funding_ms) or type(estimate.hours) is not int or not _rate(estimate.rate) or not _rate(estimate.settled_rate):
        return _bad("invalid_estimate")
    if (estimate.pair_id, estimate.mapping_version, estimate.hours, estimate.next_funding_ms, estimate.settled_rate) != (meta.pair_id, meta.mapping_version, meta.coindcx_hours, meta.next_funding_ms, report.latest.rate):
        return _bad("cycle_identity_mismatch")
    if estimate.source_ms > now_ms + MAX_FUTURE_SKEW_MS:
        return _bad("future_estimate")
    if now_ms - estimate.source_ms >= FRESH:
        return _bad("stale_estimate")
    due = report.latest.time_ms + meta.coindcx_hours * HOUR
    if now_ms >= due + GRACE:
        return _bad("expired")
    if now_ms >= due or estimate.source_ms < report.latest.time_ms or estimate.source_ms >= due:
        return _bad("cycle_identity_mismatch")
    with localcontext(Context(prec=60)):
        value = estimate.rate - report.latest.rate
    return FundingValue("available", fraction=Decimal(0) if value == 0 else value,
                        asof_ms=estimate.source_ms, expires_at_ms=min(due, estimate.source_ms + FRESH),
                        assurance="estimate_minus_verified_settlement", classification_status="unclassified")


def native_estimate(estimate, metadata, *, now_ms):
    """History-independent native rate/anchor/APR; flags independently unsupported."""
    if not _clock(now_ms):
        raise ValueError("invalid_observation_clock")
    reason = _metadata_reason(metadata, now_ms)
    if reason:
        return {"rate": _bad(reason), "at_cap": None, "at_floor": None}
    if not isinstance(estimate, FundingEstimate) or not _rate(estimate.rate) or not _clock(estimate.source_ms) or not _clock(estimate.next_funding_ms) or type(estimate.hours) is not int:
        return {"rate": _bad("invalid_estimate"), "at_cap": None, "at_floor": None}
    if (estimate.pair_id, estimate.mapping_version, estimate.hours, estimate.next_funding_ms) != (metadata.pair_id, metadata.mapping_version, metadata.coindcx_hours, metadata.next_funding_ms):
        return {"rate": _bad("cycle_identity_mismatch"), "at_cap": None, "at_floor": None}
    if estimate.source_ms > now_ms + MAX_FUTURE_SKEW_MS or now_ms - estimate.source_ms >= FRESH or estimate.source_ms >= estimate.next_funding_ms or now_ms >= estimate.next_funding_ms:
        return {"rate": _bad("estimate_expired_or_future"), "at_cap": None, "at_floor": None}
    with localcontext(Context(prec=60)):
        equivalent = estimate.rate * 8 / estimate.hours
        return {"rate": FundingValue("available", fraction=estimate.rate, asof_ms=estimate.source_ms,
                                      expires_at_ms=min(estimate.source_ms + FRESH, metadata.source_ms + FRESH, estimate.next_funding_ms)),
                "anchor_distance_fraction": abs(equivalent - Decimal("0.0001")),
                "apr_simple_fraction": estimate.rate * (24 // estimate.hours) * 365,
                "direction": "longs_pay" if estimate.rate > 0 else "shorts_pay" if estimate.rate < 0 else "zero",
                "at_cap": estimate.rate >= metadata.cap if _rate(metadata.cap) else None,
                "at_floor": estimate.rate <= metadata.floor if _rate(metadata.floor) else None}
