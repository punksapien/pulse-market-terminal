"""ADR-0062 admitted, bounded USDⓈ-M current/history OI transport.

Pure validation and calculations live elsewhere. No import-time network access.
The caller supplies shared admission for the actual collector egress scope.
"""
from .binance_transport import fetch as _fetch, HOST, MAX_RESPONSE
from .oi_history import parse_history
from pulse.store.capsule_model import CapsuleSpec, observation

def fetch_current(spec,admission,*,clock,ca_file=None,evidence_sink=None):
    if not isinstance(spec,CapsuleSpec) or spec.metric!="oi":
        raise ValueError("oi_current_descriptor_required")
    if not callable(clock):
        raise ValueError("invalid_observation_clock")
    payload=_fetch("/fapi/v1/openInterest",{"symbol":spec.symbol},admission,
                   {"history":0,"weight":1},ca_file)
    if evidence_sink is not None:
        evidence_sink(payload)
    # Sample trusted time after response: an 8s HTTP request must not look future-dated.
    now_ms=clock()
    return parse_current(spec,payload,now_ms=now_ms)


def parse_current(spec,payload,*,now_ms):
    if not isinstance(spec,CapsuleSpec) or spec.metric!="oi":
        raise ValueError("oi_current_descriptor_required")
    from pulse.observation import MAX_SOURCE_TIME_MS
    if type(now_ms) is not int or not 0<=now_ms<=MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_observation_clock")
    if type(payload) is not dict or payload.get("symbol")!=spec.symbol:
        raise ValueError("oi_identity_mismatch")
    point=observation(spec,payload.get("time"),(payload.get("openInterest"),))
    if point.source_ms>now_ms+5000 or now_ms-point.source_ms>spec.max_ingress_age_ms:
        raise ValueError("oi_current_stale_or_future")
    return point


def fetch_history(spec,admission,*,clock,ca_file=None,evidence_sink=None):
    if not isinstance(spec,CapsuleSpec) or spec.metric!="oi_cold":
        raise ValueError("oi_history_descriptor_required")
    if not callable(clock):
        raise ValueError("invalid_observation_clock")
    payload=_fetch("/futures/data/openInterestHist",{"symbol":spec.symbol,"period":"5m","limit":320},
                   admission,{"history":1,"weight":0},ca_file)
    if evidence_sink is not None:
        evidence_sink(payload)
    return parse_history(payload,spec,now_ms=clock())
