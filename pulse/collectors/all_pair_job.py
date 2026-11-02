"""ADR-0169: one fenced pair job; the scheduler owns concurrency, not calculations."""
import re
from .preview_pair import collect_oi,collect_instrument,collect_ls_period,refresh_legacy
from .preview_compose import retain_derived


def execute(job,meta,source,prior,*,client,run_id,state,bulk,info,premium,functions,derive=retain_derived,maintenance_only=False):
    kind=job.kind;prior=dict(prior or {})
    prior.setdefault("schedule_first_seen_ms",prior.get("last_collected_ms") or source.clock())
    def failed(exc):
        retry=dict(prior.get("schedule_retry_ms",{}));retry[kind]=source.clock()+60000
        prior["schedule_retry_ms"]=retry;reason=str(exc)
        prior["collection_failure"]={"job":kind,"at_ms":source.clock(),
            "reason":reason if re.fullmatch(r"[a-zA-Z0-9_:]{1,100}",reason) else type(exc).__name__}
        state.write(meta["symbol"],prior)
    try:
        if kind=="bootstrap":
            def checkpoint(value):
                prior.clear();prior.update(value);state.write(meta["symbol"],prior)
            value=functions["collect"](client,run_id,meta,source,bulk,info,premium,prior,
                all_mode=True,checkpoint=checkpoint)
        elif kind=="oi":value=collect_oi(client,run_id,meta,source,prior)
        elif kind=="instrument":value=collect_instrument(meta,source,prior)
        elif kind=="global":value=functions["collect_ls_tip"](client,run_id,meta,source,prior,periods=False,repair=True)
        elif kind.startswith("period_"):value=collect_ls_period(client,run_id,meta,source,prior,window=kind[7:])
        elif kind=="detail":
            options=dict(top_refresh_ms=900000,repair=not maintenance_only)
            if maintenance_only:options["maintenance_only"]=True
            value=functions["collect_detail"](client,run_id,meta,source,prior,**options)
        elif kind=="funding":value=functions["collect_funding"](client,run_id,meta,source,bulk,info,premium,prior,settlement_only=True)
        elif kind=="references":value=functions["collect_references"](meta,source,bulk,prior,force_prefetch=True)
        elif kind=="legacy":value=refresh_legacy(source,meta["pair_id"],meta["symbol"],
            bulk["prices"].get(meta["pair_id"],{}),prior,source.clock(),reuse_oi=True)
        else:raise ValueError("all_pair_job_refused")
    except (ValueError,KeyError,TypeError) as exc:
        failed(exc);raise
    if kind in ("bootstrap","oi","global","detail") or "derived" not in value:
        try:value=derive(client,run_id,meta,value,now_ms=source.clock(),
            update={"oi":"oi","global":"ls"}.get(kind,"all"))
        except (ValueError,KeyError,TypeError) as exc:
            failed(exc);raise
    if kind=="bootstrap":
        value["last_hot_ms"]=value["derived"]["current"].get("source_ms",source.clock())
        if len(value["derived"]["changes"])==3 and all(v.get("status")=="available" for v in value["derived"]["changes"].values()):
            value["detail_refreshed_ms"]=source.clock()
    retry=dict(value.get("schedule_retry_ms",{}));value.pop("collection_failure",None)
    retry[kind]=source.clock()+60000;value["schedule_retry_ms"]=retry
    state.write(meta["symbol"],value)
    return value
