"""ADR-0169 completion-driven pool, durable outcomes and one active writer per pair."""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from threading import Event,Lock,local
import heapq
import re
from .all_pair_schedule import due_jobs,DueJob
from .all_pair_job import execute
from .job_deadlines import priority,providers
from .binance_transport import AdmissionDenied
from .preview_compose import retain_derived
from pulse.store.staging_policy import MeasuredCapacity

PROVIDER_SLOTS={'binance':16,'coindcx':20}


def maintenance_allowed(kind,record):
    """ADR-0183: pressure pauses backfill, not existing bounded live maintenance."""
    record=record or {}
    if kind=='bootstrap':return False
    if kind=='references':return bool(record.get('opens'))
    if kind=='detail':return type(record.get('top_source_ms')) is int and record['top_source_ms']>0
    return True


def run_due(client,run_id,universe,records,state,publisher,sources,new_sources,
            bulk,info,premium,mapping,*,now,deadline,functions,history_paused=False):
    by_symbol={m["symbol"]:{**m,"mapping_version":mapping} for m in universe}
    records=dict(records);cycles={s:r.get("nextFundingTime") for s,r in premium["data"].items()}
    attempted=set();active=set();pending={};counts={};failures={};calls=0;stopped=None;transport={}
    timings={};lock=Lock();halt=Event();binance_paused=False;worker_local=local()
    pressure=(state.read("SCHEDULE_PRESSURE") or {}).get("state","normal")
    bootstrap_first=state.read("SNAPSHOT_CURSOR") is None

    def work(job,prior,maintenance_only):
        nonlocal calls
        started=now();source=None;before=0
        def fenced():
            if halt.is_set():raise RuntimeError("worker_batch_stopped")
            publisher.renew()
        try:
            source=getattr(worker_local,"source",None)
            if source is None:
                source=new_sources(deadline);worker_local.source=source
            before=source.calls
            source.check_lease=fenced
            fenced()
            return execute(job,by_symbol[job.symbol],source,prior,client=client,run_id=run_id,
                state=state,bulk=bulk,info=info,premium=premium,functions=functions,derive=retain_derived,
                maintenance_only=maintenance_only)
        except AdmissionDenied as exc:
            if not (getattr(exc,"denied_source",None)=="binance" and exc.admission_reason in
                    ("header_refresh_required","budget_exhausted:1")):halt.set()
            raise
        except RuntimeError as exc:
            if str(exc) in ("worker_source_budget_exhausted","publication_fenced_out","worker_state_fenced_out"):halt.set()
            raise
        except OSError:
            halt.set();raise
        finally:
            elapsed=max(0,int((now()-started)*1000))
            with lock:
                if source is not None:calls+=source.calls-before
                m=timings.setdefault(job.kind,{"attempts":0,"sum_ms":0,"max_ms":0})
                m["attempts"]+=1;m["sum_ms"]+=elapsed;m["max_ms"]=max(m["max_ms"],elapsed)

    # ADR-0172: overlap bounded I/O, not32 simultaneous heavy candle decodes.
    with ThreadPoolExecutor(max_workers=32,thread_name_prefix="pulse-due") as pool:
        while True:
            if now()+4<deadline and not halt.is_set() and len(pending)<32:
                current=MeasuredCapacity.from_info(client.info("memory"),provider_usable_bytes=125000000,
                    approved_plan_fallback=True).pressure(previous=pressure)
                if current!=pressure and current!="hard_stop":state.write("SCHEDULE_PRESSURE",{"state":current})
                pressure=current
                if current=="hard_stop":stopped="memory_allocation_stop";halt.set()
                else:
                    heap=due_jobs(universe,records,now_ms=sources.clock(),attempted=attempted,next_funding=cycles)
                    eligible=[j for j in heap if j[1] not in active
                        and (not history_paused or j[2] in ("oi","instrument","references"))
                        and (not binance_paused or j[2] in ("instrument","references"))
                        and (pressure!="bootstrap_repair_paused" or maintenance_allowed(j[2],records.get(j[1])))]
                    if bootstrap_first and any(j[2]=="bootstrap" for j in eligible):
                        eligible=[j for j in eligible if j[2]=="bootstrap"]
                    eligible=[priority(item,records.get(item[1])) for item in eligible]
                    heapq.heapify(eligible)
                    heavy=sum(j.kind in ("legacy","bootstrap") for j in pending.values())
                    lanes={p:sum(p in providers(j.kind) for j in pending.values()) for p in ('binance','coindcx')}
                    while eligible and len(pending)<32 and not halt.is_set() and now()+4<deadline:
                        _,due,symbol,kind=heapq.heappop(eligible)
                        if symbol in active:continue
                        if kind in ("legacy","bootstrap") and heavy>=6:continue
                        if any(lanes[p]>=PROVIDER_SLOTS[p] for p in providers(kind)):continue
                        job=DueJob(symbol,kind,due)
                        attempted.add((symbol,kind));active.add(symbol)
                        pending[pool.submit(work,job,records.get(symbol),pressure=="bootstrap_repair_paused")]=job
                        for p in providers(kind):lanes[p]+=1
                        if kind in ("legacy","bootstrap"):heavy+=1
            if not pending:break
            done,_=wait(pending,timeout=.1,return_when=FIRST_COMPLETED)
            for future in done:
                job=pending.pop(future);active.remove(job.symbol)
                try:
                    records[job.symbol]=future.result()
                    counts[job.kind]=counts.get(job.kind,0)+1
                except AdmissionDenied as exc:
                    reason=exc.admission_reason
                    if getattr(exc,"denied_source",None)=="binance" and reason=="header_refresh_required":binance_paused=True
                    elif getattr(exc,"denied_source",None)=="binance" and reason=="budget_exhausted:1":history_paused=True
                    else:stopped=stopped or "admission:"+reason;halt.set()
                except RuntimeError as exc:
                    reason=str(exc)
                    if reason in ("publication_fenced_out","worker_state_fenced_out"):halt.set();raise
                    if reason=="worker_source_budget_exhausted":stopped=stopped or "time_budget";halt.set()
                    elif reason!="worker_batch_stopped":
                        failures[job.symbol]=reason if re.fullmatch(r"[a-zA-Z0-9_:]{1,100}",reason) else type(exc).__name__
                        records[job.symbol]=state.read(job.symbol)
                except (ValueError,KeyError,TypeError):
                    failures[job.symbol]="worker_pair_invalid";records[job.symbol]=state.read(job.symbol)
                except OSError as exc:
                    stopped="source_transport_failed";halt.set();transport["transport_error_type"]=type(exc).__name__
                    endpoint=getattr(exc,"source_endpoint",None)
                    if endpoint in ("openInterest","openInterestHist","globalLongShortAccountRatio",
                            "topLongShortAccountRatio","instrument","fundingRate","candlesticks"):transport["transport_endpoint"]=endpoint
                    for key in ("source_timeout_ms","source_elapsed_ms"):
                        value=getattr(exc,key,None)
                        if type(value) is int and 0<=value<=300000:transport[key]=value
                except Exception:
                    halt.set();raise
    stamp=sources.clock();remaining=due_jobs(universe,records,now_ms=stamp,next_funding=cycles)
    return records,dict(scheduled_jobs=counts,schedule_attempted=len(attempted),job_time_ms=timings,
        history_budget_paused=history_paused,binance_header_paused=binance_paused,
        schedule_due=len(remaining),schedule_overdue_max_ms=max((stamp-t for t,_,_ in remaining),default=0),
        metric_source_dispatches=calls,failed=failures,stopped=(stopped or "time_budget") if now()+4>=deadline else stopped,
        memory_pressure=pressure,bootstrap_first=bootstrap_first,
        bootstrap_completed=sum(bool(r and r.get("last_collected_ms")) for r in records.values()),**transport)
