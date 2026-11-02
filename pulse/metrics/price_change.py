"""ADR-0048: pure event-time reference selection; no implicit price/volume fallback."""

from dataclasses import dataclass
from collections.abc import Mapping
from decimal import Decimal, DecimalException

from pulse.observation import MAX_FUTURE_SKEW_MS, MAX_SOURCE_TIME_MS
from pulse.metrics.price_arithmetic import decimal_fraction

MINUTE_MS = 60000
WINDOW_MS = {"1H": 3600000, "4H": 14400000, "24H": 86400000}
HORIZONS = tuple(WINDOW_MS)
MAX_SLOTS = 8


@dataclass(frozen=True)
class PriceTick:
    pair_id: str
    source_time_ms: int
    price: Decimal


@dataclass(frozen=True)
class CandleOpen:
    pair_id: str
    open_time_ms: int
    price: Decimal


@dataclass(frozen=True)
class ReferenceLadder:
    pair_id: str
    mapping_version: str
    definition_version: str
    generation_id: str
    built_at_ms: int
    start_ms: int
    slots: tuple[tuple[CandleOpen | None, ...], ...]

    @property
    def valid_until_ms(self):
        return self.start_ms + len(self.slots) * MINUTE_MS


@dataclass(frozen=True)
class FallbackReferences:
    tick: PriceTick
    mapping_version: str
    definition_version: str
    generation_id: str
    built_at_ms: int
    references: tuple[CandleOpen | None, ...]


@dataclass(frozen=True)
class PriceChange:
    status: str
    reason: str | None
    value_fraction: Decimal | None
    numerator_asof_ms: int | None
    reference_asof_ms: int | None
    numerator_age_ms: int | None
    delayed: bool | None = None
    pair_id: str | None = None


@dataclass(frozen=True)
class TickDecision:
    status: str
    tick: PriceTick | None
    reason: str | None = None


def _time(value):
    return type(value) is int and 0 <= value <= MAX_SOURCE_TIME_MS


def _name(value):
    return isinstance(value, str) and 0 < len(value) <= 128


def _price(value):
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _clock(now_ms):
    if not _time(now_ms):
        raise ValueError("invalid_observation_clock")


def _valid_tick(tick, now_ms):
    return (isinstance(tick, PriceTick) and _name(tick.pair_id) and _time(tick.source_time_ms)
            and tick.source_time_ms <= now_ms + MAX_FUTURE_SKEW_MS and _price(tick.price))


def _identity(pair_id, mapping_version, definition_version, generation_id):
    if not all(_name(x) for x in (pair_id, mapping_version, definition_version, generation_id)):
        raise ValueError("invalid_identity")


def select_tick(current, incoming, *, pair_id, now_ms):
    _clock(now_ms)
    if not _name(pair_id):
        raise ValueError("invalid_pair")
    if current is not None and (not _valid_tick(current, now_ms) or current.pair_id != pair_id):
        return TickDecision("rejected", None, "invalid_current")
    if not _valid_tick(incoming, now_ms) or incoming.pair_id != pair_id:
        return TickDecision("rejected", current, "invalid_incoming")
    if current is not None and incoming.source_time_ms < current.source_time_ms:
        return TickDecision("ignored_out_of_order", current)
    status = "accepted_equal_time" if current is not None and incoming.source_time_ms == current.source_time_ms else "accepted"
    return TickDecision(status, incoming)


def _reference(opens, pair_id, minute_ms, cutoff_ms):
    candidate = opens.get(minute_ms)
    if (isinstance(candidate, CandleOpen) and candidate.pair_id == pair_id
            and _time(candidate.open_time_ms) and candidate.open_time_ms == minute_ms
            and minute_ms <= cutoff_ms and _price(candidate.price)):
        return candidate
    return None


def build_ladder(opens, *, pair_id, mapping_version, definition_version, generation_id, now_ms, slots=5):
    _clock(now_ms)
    _identity(pair_id, mapping_version, definition_version, generation_id)
    if not isinstance(opens, Mapping):
        raise ValueError("invalid_candle_mapping")
    if type(slots) is not int or not 1 <= slots <= MAX_SLOTS:
        raise ValueError("invalid_slot_count")
    start = now_ms // MINUTE_MS * MINUTE_MS
    if start + slots * MINUTE_MS > MAX_SOURCE_TIME_MS:
        raise ValueError("ladder_time_overflow")
    rows = tuple(tuple(_reference(opens, pair_id, start + i * MINUTE_MS - window, now_ms)
                       for window in WINDOW_MS.values()) for i in range(slots))
    return ReferenceLadder(pair_id, mapping_version, definition_version, generation_id, now_ms, start, rows)


def build_fallback(opens, tick, *, mapping_version, definition_version, generation_id, now_ms):
    _clock(now_ms)
    if not _valid_tick(tick, now_ms):
        raise ValueError("invalid_fallback_tick")
    _identity(tick.pair_id, mapping_version, definition_version, generation_id)
    if not isinstance(opens, Mapping):
        raise ValueError("invalid_candle_mapping")
    refs = tuple(_reference(opens, tick.pair_id, (tick.source_time_ms - window) // MINUTE_MS * MINUTE_MS, now_ms)
                 for window in WINDOW_MS.values())
    return FallbackReferences(tick, mapping_version, definition_version, generation_id, now_ms, refs)


def _unavailable(reason, tick=None, reference=None, now_ms=None, delayed=None):
    source_time = tick.source_time_ms if isinstance(tick, PriceTick) and _time(tick.source_time_ms) else None
    ref_time = reference.open_time_ms if isinstance(reference, CandleOpen) and _time(reference.open_time_ms) else None
    age = now_ms - source_time if source_time is not None and now_ms is not None else None
    pair_id = tick.pair_id if isinstance(tick, PriceTick) and _name(tick.pair_id) else None
    return PriceChange("unavailable", reason, None, source_time, ref_time, age, delayed, pair_id)


def _evaluate(tick, reference, *, horizon, now_ms, cutoff_ms, delayed=None):
    expected = (tick.source_time_ms - WINDOW_MS[horizon]) // MINUTE_MS * MINUTE_MS
    if not (isinstance(reference, CandleOpen) and reference.pair_id == tick.pair_id
            and _time(reference.open_time_ms) and reference.open_time_ms == expected
            and reference.open_time_ms <= cutoff_ms and _price(reference.price)):
        return _unavailable("reference_missing_or_invalid", tick, reference, now_ms, delayed)
    try:
        fraction = decimal_fraction(tick.price, reference.price)
    except DecimalException:
        return _unavailable("arithmetic_unavailable", tick, reference, now_ms, delayed)
    if not fraction.is_finite():
        return _unavailable("arithmetic_unavailable", tick, reference, now_ms, delayed)
    return PriceChange("available", None, fraction, tick.source_time_ms, reference.open_time_ms, now_ms - tick.source_time_ms, delayed, tick.pair_id)


def _versions(frame, mapping_version, definition_version, generation_id):
    actual = (frame.mapping_version, frame.definition_version, frame.generation_id)
    expected = (mapping_version, definition_version, generation_id)
    return all(_name(x) for x in actual + expected) and actual == expected


def price_change(tick, ladder, *, horizon, mapping_version, definition_version, generation_id, numerator_expires_at_ms, now_ms):
    _clock(now_ms)
    if horizon not in WINDOW_MS or not _time(numerator_expires_at_ms):
        raise ValueError("invalid_horizon_or_expiry")
    if not _valid_tick(tick, now_ms):
        return _unavailable("invalid_tick")
    if numerator_expires_at_ms <= tick.source_time_ms:
        return _unavailable("invalid_numerator_expiry", tick, now_ms=now_ms)
    if not isinstance(ladder, ReferenceLadder) or ladder.pair_id != tick.pair_id:
        return _unavailable("pair_or_ladder_mismatch", tick, now_ms=now_ms)
    if not _versions(ladder, mapping_version, definition_version, generation_id):
        return _unavailable("version_mismatch", tick, now_ms=now_ms)
    if (not _time(ladder.built_at_ms) or ladder.built_at_ms > now_ms + MAX_FUTURE_SKEW_MS
            or type(ladder.start_ms) is not int or ladder.start_ms != ladder.built_at_ms // MINUTE_MS * MINUTE_MS
            or type(ladder.slots) is not tuple or not 1 <= len(ladder.slots) <= MAX_SLOTS
            or any(type(row) is not tuple or len(row) != 3 for row in ladder.slots)
            or ladder.valid_until_ms > MAX_SOURCE_TIME_MS):
        return _unavailable("malformed_ladder", tick, now_ms=now_ms)
    if now_ms >= numerator_expires_at_ms:
        return _unavailable("numerator_expired", tick, now_ms=now_ms)
    if now_ms >= ladder.valid_until_ms:
        return _unavailable("ladder_expired", tick, now_ms=now_ms)
    if not ladder.start_ms <= tick.source_time_ms < ladder.valid_until_ms:
        return _unavailable("outside_ladder", tick, now_ms=now_ms)
    row = (tick.source_time_ms - ladder.start_ms) // MINUTE_MS
    return _evaluate(tick, ladder.slots[row][HORIZONS.index(horizon)], horizon=horizon, now_ms=now_ms, cutoff_ms=ladder.built_at_ms)


def fallback_change(frame, *, pair_id, horizon, mapping_version, definition_version, generation_id, expires_at_ms, now_ms):
    _clock(now_ms)
    if horizon not in WINDOW_MS or not _time(expires_at_ms):
        raise ValueError("invalid_horizon_or_expiry")
    if not isinstance(frame, FallbackReferences) or not _valid_tick(frame.tick, now_ms):
        return _unavailable("invalid_fallback")
    tick = frame.tick
    delayed = now_ms - tick.source_time_ms > 120000
    if tick.pair_id != pair_id:
        return _unavailable("pair_mismatch", tick, now_ms=now_ms, delayed=delayed)
    if expires_at_ms <= tick.source_time_ms:
        return _unavailable("invalid_fallback_expiry", tick, now_ms=now_ms, delayed=delayed)
    if not _versions(frame, mapping_version, definition_version, generation_id):
        return _unavailable("version_mismatch", tick, now_ms=now_ms, delayed=delayed)
    if (not _time(frame.built_at_ms) or frame.built_at_ms > now_ms + MAX_FUTURE_SKEW_MS
            or type(frame.references) is not tuple or len(frame.references) != 3):
        return _unavailable("malformed_fallback", tick, now_ms=now_ms, delayed=delayed)
    if now_ms >= expires_at_ms:
        return _unavailable("fallback_expired", tick, now_ms=now_ms, delayed=delayed)
    return _evaluate(tick, frame.references[HORIZONS.index(horizon)], horizon=horizon, now_ms=now_ms, cutoff_ms=frame.built_at_ms, delayed=delayed)
