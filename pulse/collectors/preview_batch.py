"""ADR-0109 bounded pair concurrency; each pair appears once and keeps its fenced capsule writer."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from .binance_transport import AdmissionDenied


def collect_batch(order,records,*,collect,make_source,check_lease,refresh_ms=45000,workers=8,timestamp_field="last_collected_ms"):
    if timestamp_field not in ("last_collected_ms","last_hot_ms","global_tip_refreshed_ms","ls_windows_refreshed_ms"): raise ValueError("worker_batch_timestamp")
    if not 1<=workers<=16 or len(order)>1024 or len({m["symbol"] for m in order})!=len(order):
        raise ValueError("worker_batch_bound")
    stop=threading.Event()
    result={"records":dict(records),"collected":0,"collected_symbols":[],"skipped":0,"failed":{},"stopped":None,"source_dispatches":0}
    sources=[]
    lock=threading.Lock()

    def one(meta):
        if stop.is_set(): return "stopped",None
        source=make_source()
        with lock: sources.append(source)
        def fenced():
            if stop.is_set(): raise RuntimeError("worker_batch_stopped")
            check_lease()
        source.check_lease=fenced
        prior=records[meta["symbol"]]
        if prior and 0<=source.clock()-prior.get(timestamp_field,0)<refresh_ms:
            return "skipped",None
        fenced()
        try:
            return "collected",collect(meta,source,prior)
        except AdmissionDenied:
            stop.set()
            raise
        except RuntimeError as exc:
            if str(exc) in ("worker_source_budget_exhausted","publication_fenced_out","worker_state_fenced_out"):
                stop.set()
            raise

    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="pulse-pair") as executor:
        tasks={executor.submit(one,meta):meta for meta in order}
        for task in as_completed(tasks):
            meta=tasks[task]
            try:
                status,record=task.result()
                if status=="collected":
                    result["records"][meta["symbol"]]=record
                    result["collected"]+=1
                    result["collected_symbols"].append(meta["symbol"])
                elif status=="skipped": result["skipped"]+=1
            except AdmissionDenied as exc:
                result["stopped"]="admission:"+exc.admission_reason
                if getattr(exc,"denied_source",None) in ("coindcx","binance"):
                    result["denied_source"]=exc.denied_source
            except RuntimeError as exc:
                if str(exc) in ("publication_fenced_out","worker_state_fenced_out"):
                    stop.set()
                    raise
                if str(exc)=="worker_source_budget_exhausted":
                    result["stopped"]=result["stopped"] or "time_budget"
                elif str(exc)!="worker_batch_stopped": result["failed"][meta["symbol"]]=str(exc)[:80]
            except (ValueError,KeyError,TypeError):
                result["failed"][meta["symbol"]]="worker_pair_invalid"
            except OSError as exc:
                stop.set()
                result["stopped"]="source_transport_failed"
                result["transport_error_type"]=type(exc).__name__
                endpoint=getattr(exc,"source_endpoint",None)
                if endpoint in ("openInterest","openInterestHist","globalLongShortAccountRatio",
                                "topLongShortAccountRatio","instrument","fundingRate","candlesticks"):
                    result["transport_endpoint"]=endpoint
                for key in ("source_timeout_ms","source_elapsed_ms"):
                    value=getattr(exc,key,None)
                    if type(value) is int and 0<=value<=300000: result[key]=value
    result["source_dispatches"]=sum(s.calls for s in sources)
    return result
