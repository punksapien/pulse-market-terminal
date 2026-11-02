"""ADR-0173 fast-only bounded live publication; no pair jobs/scorer."""
import os,time
from .preview_runtime import connect,fetch_bulk,fetch_funding_metadata
from .preview_sources import Sources
from .preview_compose import prepare_price,prepare_funding
from .release_universe import load
from .binance_transport import AdmissionDenied
from .binance_transport import HOST
from pulse.store.artifact_publisher import ArtifactPublisher
from pulse.store.worker_state import WorkerState
from pulse.store.runtime_document import RuntimeDocument
from pulse.store.production_scope import release_enabled,dcx12_enabled
from pulse.store.expansion_guard import ExpansionStopped


def run(*,run_id,mapping_version,duration_ms,env=None,deps=None):
    env=os.environ if env is None else env
    if not release_enabled(env) or not dcx12_enabled(env):raise ValueError("live_preview_release_required")
    if type(duration_ms) is not int or not 1<=duration_ms<=55000:raise ValueError("live_duration_refused")
    d=dict(connect=connect,Sources=Sources,Publisher=ArtifactPublisher,State=WorkerState,Document=RuntimeDocument,
        load=load,bulk=fetch_bulk,metadata=fetch_funding_metadata,price=prepare_price,funding=prepare_funding,
        now=time.monotonic,sleep=time.sleep);d.update(deps or {})
    started=d["now"]();end=started+duration_ms/1000;client=d["connect"](env)
    pub=d["Publisher"](client,run_id,mapping_version,lane="live")
    result=dict(status="incomplete",price_publishes=0,funding_publishes=0)
    try:
        try:pub.acquire()
        except RuntimeError as e:
            if str(e)=="publication_lease_busy":return {"status":"lease_busy"}
            raise
        source=d["Sources"](client,scope="vercel-bom1-all-v1",deadline=end-2,preview_dcx12=dcx12_enabled(env))
        source.check_lease=pub.renew
        # Binance header bootstrap is deferred until a Binance dispatch; DCX is independent.
        source.dcx.initialise()
        universe=d["load"](client,run_id,mapping_version,now_ms=source.clock())
        state=d["State"](pub);metadata=None;next_funding=started;next_price=started
        funding_paused=False;last_funding=None;bulk=None;memory_waiting=False;memory_required=0
        def publish(package):
            pub.renew();doc=d["Document"](client,run_id,package.channel,mapping_version)
            rev=doc.read(now_ms=source.clock())["revision"]
            pub.publish([package],expected_revisions={package.channel:rev})
        for _ in range(80):
            at=d["now"]()
            if at+4>=end:break
            pub.renew()
            try:
                if memory_waiting:
                    # ADR-0199: let native artifact expiry reclaim headroom without source retry storms.
                    pub.preflight(memory_required)
                    memory_waiting=False;bulk=None;next_price=at
                    result['memory_recovery']='resumed'
                if at>=next_price:
                    bulk=d["bulk"](source)
                    publish(d["price"](universe,pub,bulk,now_ms=source.clock(),compressed_only=True))
                    result["price_publishes"]+=1;next_price=at+2
                if bulk is not None and not funding_paused and at>=next_funding:
                    try:
                        stamp=source.clock()
                        if metadata is None or any(type(metadata[1]["data"].get(m["symbol"],{}).get("nextFundingTime")) is int
                                and metadata[1]["data"][m["symbol"]]["nextFundingTime"]<=stamp for m in universe):
                            metadata=d["metadata"](source)
                        records=state.read_funding_many([m["symbol"] for m in universe])
                        publish(d["funding"](universe,records,pub,bulk,*metadata,now_ms=source.clock(),compressed_only=True))
                        result["funding_publishes"]+=1
                        if last_funding is not None:
                            result["funding_max_publish_gap_ms"]=max(result.get("funding_max_publish_gap_ms",0),int((d["now"]()-last_funding)*1000))
                        last_funding=d["now"]()
                    except AdmissionDenied as e:
                        if getattr(e,"denied_source",None)=="coindcx":raise
                        funding_paused=True;result["funding_stopped"]="admission:"+e.admission_reason
                    except (ValueError,KeyError,TypeError,OSError) as e:
                        funding_paused=True;result["funding_error_type"]=type(e).__name__
                    except RuntimeError as e:
                        if getattr(e,"source_host",None)!=HOST:raise
                        funding_paused=True;result["funding_error_type"]=type(e).__name__
                    next_funding=at+5
            except ExpansionStopped as e:
                if str(e)!='staging_memory_gate_headroom_denied':raise
                memory_required=e.additional_bytes;memory_waiting=True
                result['memory_pauses']=result.get('memory_pauses',0)+1
                result['memory_recovery']='waiting'
                d['sleep'](max(0,min(1,end-4-d['now']())))
                continue
            except AdmissionDenied as e:
                result["stopped"]="admission:"+e.admission_reason;break
            except RuntimeError as e:
                if str(e)!="worker_source_budget_exhausted":raise
                result["stopped"]="time_budget";break
            due=next_price if funding_paused else min(next_price,next_funding)
            d["sleep"](max(.05,min(end-4-d["now"](),due-d["now"]())))
        result.update(status="published" if result["price_publishes"] else "incomplete",pairs=len(universe),
            source_dispatches=source.calls,elapsed_ms=int((d["now"]()-started)*1000))
        return result
    finally:
        try:pub.release()
        finally:client.close()
