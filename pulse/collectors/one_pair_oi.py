"""ADR-0056 bounded single-request BTC OI collector. Never imported by read paths."""
import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError

from pulse.observation import MAX_SOURCE_TIME_MS

URL="https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
PAIR="BTCUSDT"
MAX_RESPONSE=4096


@dataclass(frozen=True)
class CurrentOI:
    symbol: str
    source_ms: int
    source_quantity: str
    quantity: float


def parse_current_oi(payload):
    if type(payload) is not dict or payload.get("symbol")!=PAIR:
        raise ValueError("oi_identity_mismatch")
    source_ms,source=payload.get("time"),payload.get("openInterest")
    if type(source_ms) is not int or not 0<=source_ms<=MAX_SOURCE_TIME_MS:
        raise ValueError("oi_invalid_timestamp")
    if not isinstance(source,str) or not 0<len(source)<=63 or any(char not in "0123456789.eE+-" for char in source):
        raise ValueError("oi_invalid_quantity")
    try:
        value=Decimal(source)
        number=float(value) if value else 0.0
    except (InvalidOperation,ValueError,OverflowError):
        raise ValueError("oi_invalid_quantity") from None
    if not value.is_finite() or value<0 or not math.isfinite(number) or Decimal(repr(number))!=value or (number and number<2**-1022):
        raise ValueError("oi_precision_unavailable")
    return CurrentOI(PAIR,source_ms,source,number)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def fetch_current_oi(*,reserve,ca_file=None):
    """reserve persists the one-request admission BEFORE any network dispatch."""
    if not reserve():
        raise RuntimeError("oi_source_budget_denied")
    import ssl
    from urllib.request import HTTPSHandler
    context=ssl.create_default_context(cafile=ca_file)
    opener=build_opener(NoRedirect,HTTPSHandler(context=context))
    request=Request(URL,headers={"Accept":"application/json"})
    try:
        with opener.open(request,timeout=8) as response:
            if response.status!=200:
                raise RuntimeError("oi_source_http_"+str(response.status))
            body=response.read(MAX_RESPONSE+1)
            if len(body)>MAX_RESPONSE:
                raise RuntimeError("oi_source_response_too_large")
            data=json.loads(body)
    except HTTPError as error:
        raise RuntimeError("oi_source_http_"+str(error.code)) from None
    return parse_current_oi(data)
