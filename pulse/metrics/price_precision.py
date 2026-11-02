"""ADR-0049: offline price ingress gate; artifacts preserve source text."""

import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from .price_arithmetic import decimal_fraction

SOURCE_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE]([+-]?\d+))?$", re.ASCII)
NUMERIC_PARITY_FACTOR = Decimal("1e-12")


@dataclass(frozen=True)
class PreparedPrice:
    source: str | None
    value: Decimal | None
    ring_value: float | None
    reason: str | None

    @property
    def available(self):
        return self.reason is None


def prepare_price(source):
    if not isinstance(source, str) or len(source) > 128:
        return PreparedPrice(None, None, None, "invalid_source_price")
    match = SOURCE_PATTERN.fullmatch(source)
    if not match or abs(int(match.group(1) or "0")) > 400:
        return PreparedPrice(source, None, None, "invalid_source_price")
    value = Decimal(source)
    if not value.is_finite() or value <= 0:
        return PreparedPrice(source, None, None, "nonpositive_price")
    candidate = float(value)
    if not math.isfinite(candidate):
        reason = "binary64_overflow"
    elif candidate == 0:
        reason = "binary64_underflow"
    elif candidate < sys.float_info.min:
        reason = "subnormal_price_unsupported"
    elif Decimal(repr(candidate)) != value:
        reason = "binary64_roundtrip_loss"
    else:
        return PreparedPrice(source, value, candidate, None)
    return PreparedPrice(source, value, None, reason)


def prepare_batch(sources):
    prepared = tuple(prepare_price(source) for source in sources)
    return prepared, dict(Counter(item.reason for item in prepared if not item.available))


def artifact_source(prepared):
    if not isinstance(prepared, PreparedPrice) or not prepared.available:
        raise ValueError("price_unavailable")
    return prepared.source


def ring_artifact_source(value):
    """ADR-0051: numerical source preservation, not original lexical spelling.

    Only use on a ring whose ingress was guarded by prepare_price. This check
    cannot authenticate arbitrary finite payload corruption or unguarded writes.
    """
    if type(value) is not float or not prepare_price(repr(value)).available:
        raise ValueError("price_unavailable")
    return repr(value)


def display_percent(current, reference, digits=2):
    if type(digits) is not int or not 0 <= digits <= 8:
        raise ValueError("invalid_digits")
    exact = (Fraction(current) - Fraction(reference)) / Fraction(reference)
    scaled = abs(exact) * 100 * 10**digits
    rounded, remainder = divmod(scaled.numerator, scaled.denominator)
    twice = 2 * remainder
    if twice > scaled.denominator or (twice == scaled.denominator and rounded % 2):
        rounded += 1
    sign = "" if rounded == 0 else "+" if exact > 0 else "-"
    units, fraction = divmod(rounded, 10**digits)
    return sign + str(units) + (("." + str(fraction).zfill(digits)) if digits else "") + "%"


def parity_case(current_source, reference_source):
    current, reference = prepare_price(current_source), prepare_price(reference_source)
    if not current.available or not reference.available:
        return {"status": "unavailable", "reason": current.reason or reference.reason}
    approximation = (current.ring_value - reference.ring_value) / reference.ring_value
    if not math.isfinite(approximation):
        return {"status": "unavailable", "reason": "binary64_arithmetic_unavailable"}
    expected = decimal_fraction(current.value, reference.value)
    return {"status": "available", "reason": None, "fraction": approximation,
            "reference_fraction": expected, "display": display_percent(current.value, reference.value),
            "current_source": artifact_source(current), "reference_source": artifact_source(reference)}
