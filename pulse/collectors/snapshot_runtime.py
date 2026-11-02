"""ADR-0135 bounded source-free composition, independent of fast collection."""
import json
import os
import time
import re
import threading

from .preview_runtime import connect
from .universe_registry import load
from .preview_compose import prepare
from pulse.store.artifact_publisher import ArtifactPublisher
from pulse.store.runtime_document import RuntimeDocument
from pulse.store.worker_state import WorkerState,measured_io
from pulse.store.published_artifacts import PublishedArtifacts,descriptor_valid
from pulse.wire.publication import byte_digest
from pulse.wire.rows import WINDOWS
from pulse.wire.validation import live
from pulse.wire.source_codec import decode as unpack_source,gunzip


def clock(client):
    seconds,micros=client.time();return seconds*1000+micros//1000


def renew_while_owned(publisher,stop,errors):
    """ADR-0153: never let CPU composition silently exhaust the fenced lease."""
    while not stop.wait(5):
        try:publisher.renew()
        except Exception:
            errors.append("publication_heartbeat_failed")
            return


def shared_inputs(client,run_id,mapping,universe,*,now_ms):
    """Read only hash-pinned source bytes; never redate a retained response."""
    record=RuntimeDocument(client,run_id,"live_funding_v1",mapping).read(now_ms=now_ms)
    if record["status"]!="available":return price_only_inputs(client,run_id,mapping,universe,now_ms=now_ms)
    descriptor=descriptor_valid(json.loads(record["payload"]),mapping_version=mapping)
    artifacts=PublishedArtifacts(client,run_id,mapping)
    variant=descriptor["variants"]["gzip"]
    packed=artifacts.blob(variant["sha256"])
    if byte_digest(packed)!=variant["sha256"] or len(packed)!=variant["bytes"]:
        raise ValueError("snapshot_live_hash_refused")
    body=gunzip(packed,maximum=descriptor["raw_bytes"])
    if byte_digest(body)!=descriptor["logical_sha256"] or len(body)!=descriptor["raw_bytes"]:
        raise ValueError("snapshot_live_hash_refused")
    document=json.loads(body)
    live(document,universe,mapping)
    values=[]
    for name in ("bulk","info","premium"):
        witness=document["inputs"]["funding:live:"+name]
        data=artifacts.blob(witness["version"])
        if byte_digest(data)!=witness["version"]:raise ValueError("snapshot_source_hash_refused")
        value=json.loads(unpack_source(data))
        if witness["source_ms"]>now_ms+5000:
            raise ValueError("snapshot_source_clock_refused")
        if now_ms>=witness["expires_ms"]:
            return price_only_inputs(client,run_id,mapping,universe,now_ms=now_ms)
        values.append(value if name=="bulk" else {"data":value,"source_ms":witness["source_ms"]})
    return tuple(values)


def price_only_inputs(client,run_id,mapping,universe,*,now_ms):
    """ADR-0157: verify a fresh price witness; funding absence is never freshened."""
    record=RuntimeDocument(client,run_id,"live_price_v1",mapping).read(now_ms=now_ms)
    if record["status"]!="available":raise RuntimeError("snapshot_shared_inputs_unavailable")
    descriptor=descriptor_valid(json.loads(record["payload"]),mapping_version=mapping)
    artifacts=PublishedArtifacts(client,run_id,mapping)
    variant=descriptor["variants"]["gzip"];packed=artifacts.blob(variant["sha256"])
    if byte_digest(packed)!=variant["sha256"] or len(packed)!=variant["bytes"]:
        raise ValueError("snapshot_live_hash_refused")
    body=gunzip(packed,maximum=descriptor["raw_bytes"])
    if byte_digest(body)!=descriptor["logical_sha256"] or len(body)!=descriptor["raw_bytes"]:
        raise ValueError("snapshot_live_hash_refused")
    document=json.loads(body)
    if document.get("schema")!="pulse.live_price.v1":raise ValueError("snapshot_price_identity_refused")
    live(document,universe,mapping)
    witness=document["inputs"]["price:batch"];data=artifacts.blob(witness["version"])
    if byte_digest(data)!=witness["version"]:raise ValueError("snapshot_source_hash_refused")
    if now_ms>=witness["expires_ms"] or witness["source_ms"]>now_ms+5000:
        raise ValueError("snapshot_source_clock_refused")
    return json.loads(unpack_source(data)),{"data":{},"source_ms":0},{"data":{},"source_ms":0}


def run(*,run_id,mapping_version,duration_ms,env=None,deps=None):
    env=os.environ if env is None else env
    if env.get("VERCEL_ENV")=="production" and env.get("PULSE_RELEASE_TOP350")!="1":
        raise ValueError("production_release350_required")
    if env.get("PULSE_UNIVERSE_MODE")!="all":raise ValueError("snapshot_all_mode_required")
    if type(duration_ms) is not int or not 1<=duration_ms<=55000:raise ValueError("snapshot_duration_refused")
    d=dict(connect=connect,Publisher=ArtifactPublisher,State=WorkerState,load=load,
        inputs=shared_inputs,prepare=prepare,Document=RuntimeDocument,clock=clock,now=time.monotonic)
    from .release_universe import capsule_run
    data_run=capsule_run(env,run_id)
    if env.get("PULSE_RELEASE_TOP350")=="1":
        from .release_universe import load as release_load
        d["load"]=release_load
    d.update(deps or {})
    started=d["now"]();end=started+duration_ms/1000
    client=d["connect"](env)
    publisher=d["Publisher"](client,run_id,mapping_version,lane="snapshot")
    stop_renewal=threading.Event();renewal=None;renewal_errors=[]
    try:
        try:publisher.acquire()
        except RuntimeError as exc:
            if str(exc)=="publication_lease_busy":return {"status":"lease_busy"}
            raise
        renewal=threading.Thread(target=renew_while_owned,args=(publisher,stop_renewal,renewal_errors),
            name="pulse-snapshot-lease",daemon=True)
        renewal.start()
        stamp=d["clock"](client)
        universe=d["load"](client,run_id,mapping_version,now_ms=stamp)
        state=d["State"](publisher)
        records=state.read_many([m["symbol"] for m in universe])
        missing=[s for s,r in records.items() if not (r and r.get("last_collected_ms") and "derived" in r)]
        partial=env.get("PULSE_PER_FIELD_PUBLICATION_APPROVED")=="1"
        if missing and not partial:return {"status":"incomplete","pairs":len(universe),"pending_pairs":len(missing),
            "pending_symbols":missing[:10],"pending_details":pending_details(records,missing),
            "source_dispatches":0,"state_io":measured_io(state)}
        checkpoint=state.read("SNAPSHOT_CURSOR")
        if checkpoint is None and not partial:
            # ADR-0138: cold history may finish before current OI; never publish that gap.
            stale=initial_oi_pending(records,now_ms=stamp,reserve_ms=duration_ms)
            if stale:return {"status":"incomplete","pairs":len(universe),
                "pending_refresh_pairs":len(stale),"pending_refresh_symbols":stale[:10],
                "source_dispatches":0,"state_io":measured_io(state)}
        bulk,info,premium=d["inputs"](client,run_id,mapping_version,universe,now_ms=stamp)
        cursor=(checkpoint or {}).get("index",0)
        if type(cursor) is not int or not 0<=cursor<3:raise ValueError("snapshot_cursor_refused")
        published=[];allocation=[];phases=[]
        horizons=tuple(WINDOWS[(cursor+offset)%3] for offset in range(3))
        # ADR-0170: build shared rows/witnesses once; retain every horizon's validators.
        packages=[];status={}
        if d["now"]()+12<end:
            packages,status=d["prepare"](client,data_run,universe,state,publisher,bulk,info,premium,
                now_ms=d["clock"](client),cached=records,horizons=horizons,compressed_only=True,allow_partial=partial,stream=True)
            packages=packages or []
        try:
            for offset,package in enumerate(packages):
                if offset>=len(horizons) or package.channel!="snapshot:"+horizons[offset].lower()+":manifest":
                    raise ValueError("snapshot_package_horizons_refused")
                horizon=horizons[offset]
                if renewal_errors:raise RuntimeError(renewal_errors[0])
                publisher.renew()
                if d["now"]()+3>=end:break
                expected={package.channel:d["Document"](client,run_id,package.channel,mapping_version).read(
                    now_ms=d["clock"](client))["revision"]}
                publisher.publish([package],expected_revisions=expected)
                published.append(horizon);allocation.append(package.allocation_bytes)
                state.write("SNAPSHOT_CURSOR",{"index":((cursor+offset+1)%3)})
                # ADR-0181: leave time for measured ~15 s composition plus promotion.
                if d["now"]()+18>=end:break
        finally:
            if callable(getattr(packages,"close",None)):packages.close()
        if type(status.get("phase_ms")) is dict:
            phases.append({"horizon":"all",**status["phase_ms"]})
        return {"status":"published" if published else "incomplete","pairs":len(universe),
            "horizons":published,"allocation_bytes":allocation,"source_dispatches":0,
            "pending_pairs":len(missing),"pending_details":pending_details(records,missing),
            "compose_phase_ms":phases,
            "state_io":measured_io(state),
            "elapsed_ms":int((d["now"]()-started)*1000)}
    finally:
        stop_renewal.set()
        if renewal is not None:renewal.join(timeout=6)
        try:publisher.release()
        finally:client.close()


def initial_oi_pending(records,*,now_ms,reserve_ms):
    """Use original observation expiry, not collection/receipt time."""
    pending=[]
    for symbol,record in records.items():
        point=record.get("derived",{}).get("current",{})
        source,expiry=point.get("source_ms"),point.get("expires_ms")
        if (point.get("status")!="available" or type(source) is not int
                or type(expiry) is not int or source>now_ms+5000
                or source<0 or expiry<=now_ms+reserve_ms):
            pending.append(symbol)
    return pending


def pending_details(records,symbols):
    result=[]
    for symbol in symbols[:10]:
        record=records.get(symbol) or {}
        item={"symbol":symbol,"present":[key for key in
            ("instrument","funding_report","opens","legacy","derived") if key in record]}
        failure=record.get("collection_failure",{})
        if type(failure) is dict:
            item["failure"]={k:v for k,v in failure.items() if k in ("job","reason")
                and isinstance(v,str) and re.fullmatch(r"[a-zA-Z0-9_:]{1,100}",v)}
        item["source_clocks"]={k:record[k] for k in
            ("last_hot_ms","cold_source_ms","global_source_ms","top_source_ms","instrument_ms",
             "funding_refreshed_ms","references_refreshed_ms","legacy_source_ms") if type(record.get(k)) is int}
        result.append(item)
    return result
