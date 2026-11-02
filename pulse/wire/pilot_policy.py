"""ADR-0116: pilot L2 cut-offs approved by project owner on 27 Oct 2026. Informational only; replaced by calibration (ADR-0015)."""
from decimal import Context, Decimal, localcontext

from pulse.metrics.levels import classify_basis, magnitude
from .decimal_text import decimal_text

PILOT_VERSION = "pilot1"


def _log_cuts(*ratios):
    # ratio cut-offs are the reviewable numbers; the basis is |ln(ratio)|, so the same cut applies to 3x and 1/3x
    with localcontext(Context(prec=34)):
        return tuple(str(Decimal(r).ln()) for r in ratios)


# shortcut: one global scale per metric, no per-pair adaptation, upgrade when the ADR-0015 calibration is accepted
CUTS = {
    "funding.v1": ("2", "4", "9"),  # bps from the +0.01%/8h house anchor
    "funding_change.v1": ("1", "5", "10"),  # bps, settled d_last
    "long_short.v1": _log_cuts("1.25", "1.8", "3.0"),
    "open_interest.v1": {"1H": ("0.01", "0.03", "0.06"), "4H": ("0.02", "0.06", "0.12"), "24H": ("0.05", "0.15", "0.30")},
}


def funding_level(efr, hours):
    """Level and policy version for one live native estimate (ADR-0116); None when the estimate cannot be classified."""
    basis, _ = magnitude("funding.v1", efr, hours=hours)
    return classify_basis(basis, CUTS["funding.v1"]), "funding.v1.l2." + PILOT_VERSION


def apply(row):
    """Classify the L2-eligible fields of one row in place. Only available fields; unavailable ones stay unclassified."""
    targets = [
        (row["funding"]["native_estimate"], "funding.v1", row["funding"]["h"], None),
        (row["funding_change"]["settled_last"], "funding_change.v1", row["funding_change"]["h"], None),
        (row["ls"]["global_ratio"], "long_short.v1", None, None),
    ] + [(row["oi"]["change_" + w.lower()], "open_interest.v1", None, w) for w in ("1H", "4H", "24H")]
    for window,key in (("1D","1d"),("7D","7d")):
        targets.extend(((row["funding"]["cumulative_"+key],"funding.v1",row["funding"]["h"],window),
            (row["funding_change"]["settled_"+key],"funding_change.v1",row["funding_change"]["h"],window)))
    targets.extend((field,"long_short.v1",None,window) for window,field in row["ls"].get("period_ratios",{}).items())
    for field, definition, hours, window in targets:
        if field["status"] != "available":
            continue
        basis, unit = magnitude(definition, field["value"], hours=hours,window=window if window in ("1D","7D") else None)
        cuts = CUTS[definition][window] if definition=="open_interest.v1" else CUTS[definition]
        field.update(
            classification_status="classified",
            level=classify_basis(basis, cuts),
            level_basis=decimal_text(basis),
            level_basis_unit=unit,
            classification_version=definition + ".l2." + (window + "." if window else "") + PILOT_VERSION,
        )
