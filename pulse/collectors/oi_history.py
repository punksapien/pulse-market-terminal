"""ADR-0062 bounded native-quantity backfill; no network access in this module."""
from dataclasses import replace
from pulse.observation import MAX_SOURCE_TIME_MS
from pulse.metrics.open_interest import OIObservation, WINDOW_MS, quantity_change
from pulse.store.bucket_ring import BucketRing, BucketCollision, DiscardedBaseline
from pulse.store.capsule_model import CapsuleSpec, observation


def history_spec(pair_id,symbol):
    # ADR-0010: 5-minute history is a distinct cold-start series, never a 60s series.
    return CapsuleSpec(pair_id,symbol,"oi_cold","quantity",300000,320,900000,
                       320*300000,inclusive_expiry=True)


def parse_history(payload,spec,*,now_ms):
    if not isinstance(spec,CapsuleSpec) or spec.metric!="oi_cold":
        raise ValueError("oi_history_descriptor_required")
    if type(now_ms) is not int or not 0<=now_ms<=MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_observation_clock")
    if type(payload) is not list or len(payload)>500:
        raise ValueError("oi_history_response_size")
    rows={}
    oldest=now_ms-spec.max_ingress_age_ms
    for row in payload:
        if type(row) is not dict or row.get("symbol")!=spec.symbol:
            raise ValueError("oi_history_identity_mismatch")
        stamp=row.get("timestamp")
        # Binance documents string timestamps; also accept integer JSON timestamps.
        if isinstance(stamp,str) and stamp.isascii() and stamp.isdigit() and len(stamp)<=16:
            stamp=int(stamp)
        if type(stamp) is not int or not 0<=stamp<=MAX_SOURCE_TIME_MS:
            raise ValueError("oi_history_invalid_timestamp")
        if stamp>now_ms+5000:
            raise ValueError("oi_history_future_timestamp")
        # Do not let a later row hide a conflicting exact-timestamp observation.
        point=observation(spec,stamp,(row.get("sumOpenInterest"),))
        if stamp in rows and rows[stamp].values!=point.values:
            raise ValueError("oi_history_exact_timestamp_conflict")
        rows[stamp]=point
    selected=sorted((point for stamp,point in rows.items() if stamp>=oldest),key=lambda p:p.source_ms)
    # At most capacity buckets are retained, including an allowed future-jitter bucket.
    if selected:
        newest_bucket=selected[-1].source_ms//spec.step_ms
        selected=[p for p in selected if p.source_ms//spec.step_ms>newest_bucket-spec.capacity]
    return tuple(selected)


def build_history_ring(points,spec,*,now_ms):
    """Validate a bounded response off-store before any real Redis write."""
    if not isinstance(spec,CapsuleSpec) or spec.metric!="oi_cold" or len(points)>500:
        raise ValueError("oi_history_descriptor_or_size")
    ring=BucketRing(spec.step_ms,spec.capacity,spec.columns)
    for point in points:
        checked=observation(spec,point.source_ms,point.sources,expires_ms=point.expires_ms)
        if point!=checked:
            raise ValueError("source_value_mismatch")
        ring.write(point.source_ms,point.values,now_ms=now_ms)
    ring.scrub()
    return ring


def cold_start_changes(current,ring,spec,*,now_ms):
    if (not isinstance(spec,CapsuleSpec) or spec.metric!="oi_cold"
            or (ring.step_ms,ring.capacity,ring.columns)!=(spec.step_ms,spec.capacity,spec.columns)
            or not isinstance(current,OIObservation) or current.pair_id!=spec.pair_id):
        raise ValueError("oi_history_identity_or_dimensions")
    results={}
    for horizon,window in WINDOW_MS.items():
        selection_reason=None
        target=current.source_time_ms-window
        if target<0:
            baseline=None
        else:
            try:
                row=ring.baseline(target,300000)
            except (BucketCollision,DiscardedBaseline) as error:
                # Ambiguous selection is not permission to fall back to an older row.
                row=None
                selection_reason=str(error)
            baseline=None if row is None else OIObservation(row[0],row[1],spec.pair_id)
        results[horizon]=quantity_change(current,baseline,horizon=horizon,mode="cold-start",now_ms=now_ms)
        if selection_reason:
            results[horizon]=replace(results[horizon],reason=selection_reason)
    return results
