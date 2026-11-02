"""ADR-0065 bounded storage-reader composition; no exchange calls or classification."""
from dataclasses import replace
from decimal import Decimal

from pulse.metrics.long_short import (LSMetric, LSObservation, WINDOW_MS, ratio_metric,
                                      share_evolution, common_bucket_divergence)


def _observation(store, row):
    if row["status"] != "available":
        return None
    # Ingress proved Decimal(repr(value)) == source numerically (ADR-0049/0065).
    sources = row.get("sources") or tuple(repr(value) for value in row["values"])
    return LSObservation(store.spec.pair_id, store.spec.metric[3:], row["source_ms"],
                         *(Decimal(source) for source in sources))


def read_metrics(global_store, top_store, *, now_ms):
    if (global_store.spec.metric != "ls_global" or top_store.spec.metric != "ls_top"
            or (global_store.spec.pair_id, global_store.spec.symbol, global_store.spec.mapping_version)
            != (top_store.spec.pair_id, top_store.spec.symbol, top_store.spec.mapping_version)):
        raise ValueError("ls_capsule_identity_mismatch")
    series = {}
    views = {}
    for store in (global_store, top_store):
        name = store.spec.metric[3:]
        row = store.read()
        views[name] = row
        current = _observation(store, row)
        ratio = ratio_metric(current, now_ms=now_ms)
        if current is None:
            ratio = replace(ratio, reason=row["reason"])
        evolution = {}
        for horizon, window in WINDOW_MS.items():
            baseline = None
            selection = {"status": "unavailable", "reason": row.get("reason")}
            if current is not None and current.bucket_ms >= window:
                selection = store.baseline(current.bucket_ms - window, 300000)
                baseline = _observation(store, selection)
            metric = share_evolution(current, baseline, horizon=horizon, mode="cold-start", now_ms=now_ms)
            if baseline is None and current is not None:
                metric = replace(metric, reason=selection["reason"])
            evolution[horizon] = {"metric": metric, "baseline": selection}
        series[name] = {"ratio": ratio, "evolution": evolution, "current": row}

    # At most four 5m buckets can intersect global's 15m validity and +5s skew.
    start = min((row["source_ms"] for row in views.values() if row["status"] == "available"),
                default=0)
    candidates = {"global": [], "top": []}
    for offset in range(4):
        bucket = start - offset * 300000
        if bucket < 0 or now_ms >= bucket + 900000:
            break
        for store in (global_store, top_store):
            row = store.baseline(bucket, 0)
            if row["status"] == "available":
                candidates[store.spec.metric[3:]].append(_observation(store, row))
            elif row["reason"] != "baseline_missing":
                # A corrupted/ambiguous newest shared candidate is not permission
                # to silently choose an older value.
                return {"series": series, "divergence": LSMetric("unavailable", row["reason"], None, "pp")}
    divergence = common_bucket_divergence(candidates["global"], candidates["top"],
                                         pair_id=global_store.spec.pair_id, now_ms=now_ms)
    return {"series": series, "divergence": divergence}
