"""ADR-0065 shared bounded, response-aware Binance public-data transport."""
import json
import ssl
import time
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

from .one_pair_oi import NoRedirect

HOST = "https://fapi.binance.com"
MAX_RESPONSE = 262144


class AdmissionDenied(RuntimeError):
    def __init__(self, reason):
        super().__init__("oi_source_admission_denied")
        self.admission_reason = reason


def fetch(path, parameters, admission, costs, ca_file, *, max_response=MAX_RESPONSE,
          host=HOST, decimal_numbers=False, timeout=8,reserve=None,deadline=None):
    if host not in (HOST, "https://public.coindcx.com", "https://api.coindcx.com"):
        raise ValueError("source_host_refused")
    if type(max_response) is not int or not 1 <= max_response <= 4*1024*1024:
        raise ValueError("invalid_source_response_limit")
    if type(timeout) not in (int,float) or not 0 < timeout <= 8:
        raise ValueError("invalid_source_timeout")
    if not callable(getattr(admission, "feedback", None)):
        raise RuntimeError("response_aware_admission_required")
    if reserve is not None and not callable(reserve):raise ValueError("invalid_reservation_callback")
    if deadline is not None and (type(deadline) not in (int,float) or not 0<deadline<float("inf")):
        raise ValueError("invalid_dispatch_deadline")
    context = ssl.create_default_context()
    if ca_file is not None:
        context.load_verify_locations(cafile=ca_file)
    opener = build_opener(NoRedirect, HTTPSHandler(context=context))
    # CoinDCX documents literal array brackets; keep them in the canonical query.
    request = Request(host + path + "?" + urlencode(parameters, safe="[]"),
                      headers={"Accept": "application/json", "User-Agent": "CoinDCX-Pulse/0.1"})
    # Preserve the OI transport's errors and charge-before-dispatch contract.
    decision = (reserve or admission.reserve)(costs)
    if not decision or decision[0] != b"reserved":
        reason = decision[0].decode("ascii") if decision else "missing_decision"
        raise AdmissionDenied(reason)
    headers = {}
    started=time.monotonic()
    if deadline is not None:
        remaining=deadline-started
        if remaining<=0:
            admission.feedback(0,{})
            raise RuntimeError("worker_source_budget_exhausted")
        timeout=min(timeout,remaining)
    try:
        with opener.open(request, timeout=timeout) as response:
            headers = response.headers
            if response.status != 200:
                admission.feedback(response.status, headers)
                raise RuntimeError("oi_source_http_" + str(response.status))
            data = response.read(max_response + 1)
            if len(data) > max_response:
                admission.feedback(response.status, headers)
                raise RuntimeError("oi_source_response_too_large")
    except HTTPError as error:
        try:
            admission.feedback(error.code, error.headers or {})
        except RuntimeError:
            # A failed bootstrap must not hide the original provider status.
            pass
        failure = RuntimeError("oi_source_http_" + str(error.code))
        failure.source_status = error.code
        failure.source_path = path
        failure.source_host = host
        failure.response_metadata = {name: error.headers.get(name) for name in
            ("Server", "Via", "Content-Type", "Retry-After", "CF-Ray") if error.headers and error.headers.get(name)}
        raise failure from None
    except (OSError, TimeoutError) as error:
        error.source_endpoint=path.rsplit("/",1)[-1]
        error.source_timeout_ms=int(timeout*1000)
        error.source_elapsed_ms=int((time.monotonic()-started)*1000)
        try:
            admission.feedback(0, headers)
        except RuntimeError:
            # Preserve the transport failure; the admission ledger stays closed.
            pass
        raise
    admission.feedback(response.status, headers)
    return json.loads(data, parse_float=Decimal) if decimal_numbers else json.loads(data)
