"""ADR-0111 exact canonical L2 bases; caller supplies a reviewed policy, none is activated here."""
from bisect import bisect_right
from decimal import Context,Decimal,localcontext

def magnitude(definition,value,*,hours=None,window=None):
    if not isinstance(value,(str,Decimal)) or isinstance(value,bool):raise ValueError("l2_decimal_required")
    number=Decimal(value)
    if not number.is_finite():raise ValueError("l2_nonfinite")
    with localcontext(Context(prec=60)):
        if definition=="long_short.v1":
            if number<=0:raise ValueError("l2_ratio_nonpositive")
            with localcontext(Context(prec=34)):return abs(number.ln()),"log_ratio"
        if definition=="open_interest.v1":return abs(number),"fraction"
        if definition in ("funding.v1","funding_change.v1"):
            if type(hours) is not int or hours not in (1,4,8):raise ValueError("l2_native_interval")
            if window not in (None,"1D","7D"):raise ValueError("l2_funding_window")
            # ADR-0119: cumulative totals use their whole elapsed window, not native h.
            divisor={"1D":24,"7D":168}[window] if definition=="funding.v1" and window else hours
            equivalent=number*8/divisor
            return abs(equivalent-Decimal("0.0001") if definition=="funding.v1" else equivalent)*10000,"bps"
    raise ValueError("l2_definition_unsupported")

def classify_basis(value,cuts):
    """Fixed supplied scale, inclusive lower bounds: ties enter the higher band."""
    if not isinstance(value,Decimal) or not value.is_finite() or value<0:raise ValueError("l2_invalid_basis")
    if not isinstance(cuts,tuple) or len(cuts)!=3:raise ValueError("l2_invalid_cuts")
    parsed=tuple(Decimal(x) for x in cuts if isinstance(x,(str,Decimal)))
    if (len(parsed)!=3 or any(not x.is_finite() or x<=0 for x in parsed)
            or not all(a<b for a,b in zip(parsed,parsed[1:]))):raise ValueError("l2_invalid_cuts")
    return bisect_right(parsed,value)+1
