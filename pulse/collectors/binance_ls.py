"""ADR-0065 account-series adapter, never positions or execution endpoints."""
from decimal import Decimal

from pulse.metrics.long_short import LSObservation, MAX_HISTORY, SERIES_TTL_MS, ratio_metric
from pulse.observation import MAX_SOURCE_TIME_MS
from pulse.store.capsule_model import CapsuleSpec, observation
from .binance_transport import fetch

PATHS = {"global": "/futures/data/globalLongShortAccountRatio",
         "top": "/futures/data/topLongShortAccountRatio"}


def history_spec(pair_id, symbol, series):
    if series not in PATHS:
        raise ValueError("invalid_ls_series")
    return CapsuleSpec(pair_id, symbol, "ls_" + series, "ratio_shares", 300000,
                       MAX_HISTORY, SERIES_TTL_MS[series], MAX_HISTORY * 300000)


def validate_spec(spec):
    if (not isinstance(spec, CapsuleSpec) or spec.metric not in ("ls_global", "ls_top")
            or spec != history_spec(spec.pair_id, spec.symbol, spec.metric[3:])):
        raise ValueError("ls_history_descriptor_required")
    return spec.metric[3:]


def as_observation(spec, stamp, sources):
    series = validate_spec(spec)
    # The shared ingress guard rejects lossy source conversions before storage.
    point = observation(spec, stamp, sources)
    item = LSObservation(spec.pair_id, series, stamp, *(Decimal(s) for s in point.sources))
    # Historical rows are checked at their bucket, not expired as if they were live.
    metric = ratio_metric(item, now_ms=stamp)
    if metric.status != "available":
        raise ValueError("ls_payload_" + metric.reason)
    return item


def parse_history(payload, spec, *, now_ms):
    validate_spec(spec)
    if type(now_ms) is not int or not 0 <= now_ms <= MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_observation_clock")
    if type(payload) is not list or len(payload) > MAX_HISTORY:
        raise ValueError("ls_history_response_size")
    rows = {}
    for row in payload:
        if type(row) is not dict or row.get("symbol") != spec.symbol:
            raise ValueError("ls_history_identity_mismatch")
        stamp = row.get("timestamp")
        if isinstance(stamp, str) and stamp.isascii() and stamp.isdigit() and len(stamp) <= 16:
            stamp = int(stamp)
        if type(stamp) is not int or not 0 <= stamp <= MAX_SOURCE_TIME_MS or stamp % 300000:
            raise ValueError("ls_history_invalid_timestamp")
        if stamp > now_ms + 5000:
            raise ValueError("ls_history_future_timestamp")
        sources = tuple(row.get(name) for name in ("longShortRatio", "longAccount", "shortAccount"))
        as_observation(spec, stamp, sources)
        point = observation(spec, stamp, sources)
        if stamp in rows and rows[stamp].values != point.values:
            raise ValueError("ls_history_exact_timestamp_conflict")
        rows[stamp] = point
    selected = sorted((p for p in rows.values() if p.source_ms >= now_ms - spec.max_ingress_age_ms),
                      key=lambda p: p.source_ms)
    if selected:
        newest = selected[-1].source_ms // spec.step_ms
        selected = [p for p in selected if p.source_ms // spec.step_ms > newest - spec.capacity]
    return tuple(selected)


def fetch_history(spec, admission, *, clock, ca_file=None, evidence_sink=None, limit=MAX_HISTORY):
    series = validate_spec(spec)
    if not callable(clock):
        raise ValueError("invalid_observation_clock")
    if type(limit) is not int or not 1 <= limit <= MAX_HISTORY:
        raise ValueError("ls_history_invalid_limit")
    payload = fetch(PATHS[series], {"symbol": spec.symbol, "period": "5m", "limit": limit},
                    admission, {"history": 1, "weight": 0}, ca_file)
    if evidence_sink is not None:
        evidence_sink(payload)
    if type(payload) is list and len(payload) > limit:
        raise ValueError("ls_history_response_size")
    return parse_history(payload, spec, now_ms=clock())
