"""ADR-0095 adapt existing verified metric results; never recalculate definitions."""
from decimal import Decimal
from .rows import available, unavailable, WINDOWS


def add_oi(row,current,changes,*,input_ref,now_ms):
    if current.get("status")!="available":
        row["oi"]["quantity"]["reason"]=current.get("reason","source_unavailable");return
    source=current["source_ms"];expiry=current["expires_ms"]
    row["oi"]["quantity"]=available("quantity","open_interest.v1",current["sources"][0],
        source_ms=source,expires_ms=expiry,input_refs=[input_ref],now_ms=now_ms)
    for h,metric in changes.items():
        if metric.status!="available":
            row["oi"]["change_"+h.lower()]["reason"]=metric.reason;continue
        row["oi"]["change_"+h.lower()]=available("fraction","open_interest.v1",metric.value_fraction,
            source_ms=metric.asof_ms,expires_ms=expiry,input_refs=[input_ref],now_ms=now_ms,
            baseline_asof_ms=metric.baseline_asof_ms,actual_duration_ms=metric.actual_duration_ms,
            baseline_error_ms=metric.baseline_error_ms,mode=metric.mode,direction=metric.direction)
        field=row["oi"]["change_"+h.lower()]
        if field["status"]!="available":
            # ADR-0149: expired retained values still require the unavailable OI shape.
            field.update(baseline_asof_ms=None,actual_duration_ms=None,baseline_error_ms=None,mode=None)


def add_ls(row,metrics,*,input_ref,now_ms):
    def field(metric):
        if metric.status!="available": return unavailable(metric.unit,"long_short.v1",metric.reason)
        return available(metric.unit,"long_short.v1",metric.value,source_ms=metric.asof_ms,
            expires_ms=metric.expires_at_ms,input_refs=[input_ref],now_ms=now_ms)
    for name,series in metrics["series"].items():
        ratio=series["ratio"];row["ls"][name+"_ratio"]=field(ratio)
        if ratio.status=="available":
            row["ls"][name+"_long_share"]=available("fraction","long_short.v1",series["current"]["sources"][1],
                source_ms=ratio.asof_ms,expires_ms=ratio.expires_at_ms,input_refs=[input_ref],now_ms=now_ms)
        for h,v in series["evolution"].items(): row["ls"][name+"_share_change"][h]=field(v["metric"])
    row["ls"]["divergence"]=field(metrics["divergence"])


def add_funding(row,report,native,*,input_ref,now_ms,metadata=None):
    from pulse.metrics.funding import settled_view,HOUR
    if report is not None: report=settled_view(report,now_ms=now_ms)
    meta=metadata if metadata is not None else report.metadata if report is not None else None
    if meta is None: return
    if meta.coindcx_hours!=row["funding"]["h"]: raise ValueError("funding_dictionary_interval_mismatch")
    row["funding"]["nft"]=meta.next_funding_ms;cycle=row["funding_change"];cycle["nft"]=meta.next_funding_ms
    def field(v,definition="funding.v1",*,until=None):
        if v.status!="available": return unavailable("fraction",definition,v.reason)
        return available("fraction",definition,v.fraction,source_ms=v.asof_ms,
            expires_ms=min(v.expires_at_ms,until) if until is not None else v.expires_at_ms,
            input_refs=[input_ref],now_ms=now_ms,assurance=v.assurance,
            level_basis=str(v.level_basis_bps) if v.level_basis_bps is not None else None,
            level_basis_unit="bps" if v.level_basis_bps is not None else None)
    if native is not None:
        rate=native["rate"];row["funding"]["native_estimate"]=field(rate)
        if rate.status=="available":
            for key in ("cap","floor"):
                value=getattr(meta,key)
                if value is not None:
                    row["funding"][key]=available("fraction","funding.v1",value,source_ms=meta.source_ms,
                        expires_ms=rate.expires_at_ms,input_refs=[input_ref],now_ms=now_ms)
            for key in ("at_cap","at_floor"):
                if native.get(key) is not None:
                    row["funding"][key]=available("boolean","funding.v1",native[key],source_ms=rate.asof_ms,
                        expires_ms=rate.expires_at_ms,input_refs=[input_ref],now_ms=now_ms)
    if report is None: return
    if (report.metadata.coindcx_hours,report.metadata.next_funding_ms)!=(meta.coindcx_hours,meta.next_funding_ms):
        cycle["reason"]="cycle_identity_mismatch";return
    for key in ("cumulative_1d","cumulative_7d"): row["funding"][key]=field(getattr(report,key))
    if report.latest is None or report.verified_at_ms is None: return
    due=report.latest.time_ms+meta.coindcx_hours*HOUR
    if now_ms>=due:
        cycle["reason"]="settlement_pending";return
    cycle.update(status="available",reason=None,settled_rate=str(report.latest.rate),settlement_ms=report.latest.time_ms,
        verified_at_ms=report.verified_at_ms,expires_ms=due+900000)
    for key,name in (("settled_last","last"),("settled_1d","delta_1d"),("settled_7d","delta_7d")):
        cycle[key]=field(getattr(report,name),"funding_change.v1",until=min(due,meta.next_funding_ms))
    last=cycle["settled_last"]
    if last["status"]=="available":
        basis=report.last.level_basis_bps
        cycle["basis_bps"]=available("bps","funding_change.v1",basis,source_ms=last["asof_ms"],
            expires_ms=last["expires_ms"],input_refs=[input_ref],now_ms=now_ms)
