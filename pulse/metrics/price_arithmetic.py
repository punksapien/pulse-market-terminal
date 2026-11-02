"""ADR-0048/0049: fixed-context source-price return reference."""

from decimal import Context, ROUND_HALF_EVEN, Underflow, localcontext


def decimal_fraction(current, reference):
    arithmetic = Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)
    arithmetic.traps[Underflow] = True
    with localcontext(arithmetic):
        return (current - reference) / reference
