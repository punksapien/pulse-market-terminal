"""ADR-0095 real retained-record to logical snapshot composition."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import json
import time

from .preview_pair import stores,exact_json
from .funding_inputs import inputs
from pulse.metrics.open_interest import OIObservation,OIChange,WINDOW_MS
from pulse.metrics.long_short import LSMetric
from pulse.metrics.oi_selection import change_with_baselines
from pulse.metrics.funding import native_estimate
from pulse.metrics.price_change import PriceTick,CandleOpen
from pulse.metrics.price_precision import prepare_price as prepare_source_price
from pulse.store.ls_reader import read_metrics
from pulse.store.funding_cache import decode
from pulse.wire.rows import empty_row,available,build,WINDOWS
from pulse.wire.metric_rows import add_oi,add_ls,add_funding
from pulse.wire.pilot_policy import apply as pilot_levels,funding_level
from pulse.wire.reference_rows import attach as attach_refs,attach_ladder
from pulse.wire.legacy_rows import attach as attach_legacy
from pulse.wire.live_price import compose_live_price, VOLUME_DEFINITION, VOLUME_VALIDITY_MS
from pulse.wire.validation import live
from pulse.wire.composer import compose
from pulse.store.artifact_publisher import LIVE_STAGE_MS
from pulse.wire.source_codec import encode as pack_source


# the shared live-envelope bound (pulse/wire/semantics.py validate_live): generated < expires <= generated + 15 s
LIVE_ENVELOPE_MS=15000
PAIR_WITNESS_GROUP_SIZE=16


def pair_witness_groups(universe, records):
    """ADR-0132: bounded shared witnesses; field clocks remain per observation."""
    if not 1 <= len(universe) <= 1024 or len(records) != len(universe):
        raise ValueError("pair_witness_universe_refused")
    for start in range(0,len(universe),PAIR_WITNESS_GROUP_SIZE):
        yield "pairs:"+str(start//PAIR_WITNESS_GROUP_SIZE), list(zip(
            universe[start:start+PAIR_WITNESS_GROUP_SIZE],
            records[start:start+PAIR_WITNESS_GROUP_SIZE]))


def funding_values(meta,record,bulk,info,premium,mapping,*,now_ms):
    metadata=estimate=native=None
    try:
        metadata,estimate=inputs(meta["pair_id"],meta["symbol"],record["instrument"],bulk,info["data"],premium["data"][meta["symbol"]],
            instrument_ms=record["instrument_ms"],info_ms=info["source_ms"],mapping=mapping)
        native=native_estimate(estimate,metadata,now_ms=now_ms)
    except (ValueError,KeyError): pass
    return metadata,estimate,native


def funding_live_row(meta,metadata,estimate,native,refs):
    row=dict(id=meta["id"],status="unavailable",reason="source_unavailable",h=meta["funding_hours"],
        nft=metadata.next_funding_ms if metadata else None,fr=str(estimate.settled_rate) if estimate else None,
        efr=None,source_ms=None,expires_ms=None,input_refs=[])
    if native and native["rate"].status=="available":
        value=native["rate"]
        row.update(status="available",reason=None,efr=str(value.fraction),source_ms=value.asof_ms,
            expires_ms=value.expires_at_ms,input_refs=list(refs))
        # ADR-0116: a changed live rate needs its own band; the browser never recomputes thresholds
        row["level"],row["classification_version"]=funding_level(row["efr"],row["h"])
    return row


def oi_values(stores_,pair_id,*,now_ms):
    own,cold=stores_[:2];current=own.read();changes={};witness={}
    if current["status"]!="available": return current,changes,witness
    def observation(value):
        return OIObservation(value["source_ms"],value["values"][0],pair_id) if value["status"]=="available" else None
    for h,window in WINDOW_MS.items():
        target=current["source_ms"]-window
        a=own.baseline(target,60000);b=cold.baseline(target,300000)
        # Never turn integrity/ambiguous selection into permission to fall back.
        allowed=("baseline_missing","discarded_baseline_candidate","exact_timestamp_conflict","quarantined")
        if any(v["status"]!="available" and v.get("reason") not in allowed for v in (a,b)):
            raise ValueError("oi_baseline_integrity_refused")
        changes[h]=change_with_baselines(observation(current),observation(a),observation(b),horizon=h,now_ms=now_ms)
        witness[h]={"own":a,"cold":b}
    return current,changes,witness


def retain_derived(client,run_id,meta,record,*,now_ms,update="all"):
    """ADR-0133: calculate only changed pairs, retaining original field clocks."""
    if update not in ("all","oi","ls"):raise ValueError("derived_update_refused")
    previous=record.get("derived")
    if previous is not None and (previous["mapping_version"]!=meta["mapping_version"]
            or previous["pair_id"]!=meta["pair_id"]):raise ValueError("derived_identity_refused")
    selected=dict(previous or {})
    ss=stores(client,run_id,meta)
    if previous is None or update in ("all","oi"):
        current,changes,baselines=oi_values(ss,meta["pair_id"],now_ms=now_ms)
        selected.update(current=current,changes={h:asdict(v) for h,v in changes.items()},baselines=baselines)
    if previous is None or update in ("all","ls"):
        ls=read_metrics(*ss[2:],now_ms=now_ms)
        selected["ls"]=exact_json(asdict_ls(ls))
    result=dict(record)
    result["derived"]={**selected,"mapping_version":meta["mapping_version"],"pair_id":meta["pair_id"]}
    return result


def retained_metrics(record,meta,mapping):
    """Rehydrate trusted retained calculations; display adapters recheck expiry."""
    value=record["derived"]
    if (value["mapping_version"]!=mapping or value["pair_id"]!=meta["pair_id"]):
        raise ValueError("derived_identity_refused")
    def metric(data):
        data=dict(data)
        for name in ("value","level_basis"):
            if data.get(name) is not None:data[name]=Decimal(data[name])
        return LSMetric(**data)
    ls=value["ls"]
    restored={"series":{name:{"ratio":metric(s["ratio"]),"current":s["current"],
        "evolution":{h:{"metric":metric(v["metric"]),"baseline":v["baseline"]}
            for h,v in s["evolution"].items()}} for name,s in ls["series"].items()},
        "divergence":metric(ls["divergence"])}
    return value["current"],{h:OIChange(**v) for h,v in value["changes"].items()},value["baselines"],restored


def prepare(client,run_id,universe,state,publisher,bulk,info,premium,*,now_ms,cached=None,horizons=WINDOWS,compressed_only=False,allow_partial=False,stream=False):
    """No exchange I/O; ADR-0149 permits unavailable fields, never missing members."""
    import brotli
    started=time.perf_counter()
    if (not isinstance(horizons,(list,tuple)) or not 1<=len(horizons)<=3
            or len(set(horizons))!=len(horizons) or any(h not in WINDOWS for h in horizons)):
        raise ValueError("compose_horizons_refused")
    mapping=publisher.mapping_version
    records=state.read_many([meta["symbol"] for meta in universe]) if cached is None else cached
    cached=[records[meta["symbol"]] for meta in universe]
    missing=[meta["symbol"] for meta,record in zip(universe,cached) if record is None or type(record.get("last_collected_ms")) is not int]
    if missing and not allow_partial: return None,{"status":"incomplete","pending_pairs":missing}
    cached=[record or {} for record in cached]
    vector={};inputs_={};rows={h:[] for h in horizons}
    def register(name,value,source_ms,expiry,definition):
        body=json.dumps(exact_json(value),sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        if compressed_only:body=pack_source(body)
        rev,witness=publisher.register_source(name,body);vector[name]=rev
        inputs_[name]=dict(version=witness,source_ms=source_ms,verified_at_ms=now_ms,
                           expires_ms=expiry,definition_version=definition)
        return witness
    bulk_witness=register("bulk",bulk,bulk["ts"],bulk["ts"]+VOLUME_VALIDITY_MS,"prices.bulk.v1")
    register("info",info,info["source_ms"],info["source_ms"]+120000,"funding.metadata.v1")
    register("premium",premium,premium["source_ms"],premium["source_ms"]+120000,"funding.metadata.v1")
    sources_done=time.perf_counter()
    grouped=list(pair_witness_groups(universe,cached))
    refs={meta["symbol"]:ref for ref,group in grouped for meta,_ in group}
    witnesses={ref:{} for ref,_ in grouped}
    for meta,record in zip(universe,cached):
        publisher.renew()
        pair,symbol=meta["pair_id"],meta["symbol"]
        if "derived" in record:
            current,changes,baseline_witness,ls=retained_metrics(record,meta,mapping)
        else:
            ss=stores(client,run_id,meta)
            current,changes,baseline_witness=oi_values(ss,pair,now_ms=now_ms)
            ls=read_metrics(*ss[2:],now_ms=now_ms)
        ref=refs[symbol]
        witnesses[ref][symbol]={"retained":record,"oi":current,"baselines":baseline_witness,"ls":asdict_ls(ls)}
        rr={h:empty_row(meta,mapping) for h in horizons}
        rt=bulk["prices"].get(pair,{})
        metadata,estimate,native=funding_values(meta,record,bulk,info,premium,mapping,now_ms=now_ms)
        report=decode(record["funding_report"].encode()) if record.get("funding_report") else None
        for row in rr.values():
            add_oi(row,current,changes,input_ref=ref,now_ms=now_ms)
            add_ls(row,ls,input_ref=ref,now_ms=now_ms)
            add_funding(row,report,native,input_ref=ref,now_ms=now_ms,metadata=metadata)
            if report is None and record.get("funding_refresh_reason"):
                row["funding_change"]["reason"]=record["funding_refresh_reason"]
            for window,point in record.get("ls_periods",{}).items():
                if window in WINDOWS:
                    row["ls"]["period_ratios"][window]=available("ratio","long_short.v1",point["value"],
                        source_ms=point["source_ms"],expires_ms=point["expires_ms"],input_refs=[ref],now_ms=now_ms)
            for name in ("native_estimate","cap","floor","at_cap","at_floor"):
                field=row["funding"][name]
                if field["status"]=="available": field["input_refs"]=[ref,"bulk","info","premium"]
            pilot_levels(row)  # ADR-0116
            opens={stamp:CandleOpen(pair,stamp,Decimal(value)) for stamp,value in record.get("opens",[])}
            attach_ladder(row,opens,pair_id=pair,mapping_version=mapping,now_ms=now_ms)
            # ADR-0118: volume has no dependency on a valid price/trade clock.
            try:
                volume=Decimal(str(rt["v"]))
                if rt.get("mkt")!=symbol or not volume.is_finite() or volume<0: raise ValueError("volume_invalid")
                row["volume"]["quote_24h"]=available("USDT",VOLUME_DEFINITION,volume,source_ms=bulk["ts"],
                    expires_ms=bulk["ts"]+VOLUME_VALIDITY_MS,input_refs=["bulk"],now_ms=now_ms)
            except (ValueError,ArithmeticError,KeyError,TypeError): pass
            try:
                p=prepare_source_price(str(rt.get("ls")))
                if not p.available or rt.get("mkt")!=symbol: raise ValueError("price_unavailable")
                stamp=rt["btST"]
                row["price"]=available("price","price_change.v1",str(rt["ls"]),source_ms=stamp,
                    expires_ms=stamp+60000,input_refs=["bulk"],now_ms=now_ms)
                attach_refs(row,PriceTick(pair,stamp,p.value),opens,mapping_version=mapping,
                    input_ref=ref,price_expires_ms=stamp+60000,now_ms=now_ms)
                for field in row["changes"].values():
                    if field["status"]=="available": field["input_refs"].append("bulk")
            except (ValueError,KeyError,TypeError): pass
        if record.get("legacy"):
            attach_legacy(rr,record["legacy"],input_ref=ref,source_ms=record["legacy_source_ms"],
                          expires_ms=record["legacy_source_ms"]+3600000,now_ms=now_ms)
        for h,row in rr.items(): rows[h].append(row)
    rows_done=time.perf_counter()
    for ref,group in grouped:
        stamps=[witness_clock(record,bulk["ts"]) for _,record in group]
        register(ref,witnesses[ref],min(stamps),min(stamps)+8*86400000,"pair.derived.group.v1")
    witnesses_done=time.perf_counter()
    # ADR-0156: compose below always performs the full mandatory validation before encoding.
    documents=[build(universe,rows[h],inputs_,horizon=h,now_ms=now_ms,validate_now=False) for h in horizons]
    validation_done=time.perf_counter()
    from pulse.wire.validation import snapshot
    compressor=lambda body,q:brotli.compress(body,quality=q)
    # ADR-0107: live documents belong to the fast lane, never this slow atomic snapshot batch.
    points=[started,sources_done,rows_done,witnesses_done,validation_done]
    status={"status":"prepared","pairs":len(universe),"prepared_bytes":0,
        "phase_ms":{name:int((end-start)*1000) for name,start,end in zip(
            ("sources","rows","witnesses","assembly"),points,points[1:])}}
    status["phase_ms"]["validated_wire"]=0
    def packages():
        # ADR-0181: expose each fully validated horizon before computing the next.
        for doc in documents:
            before=time.perf_counter()
            package=compose(doc,validate=snapshot,brotli_compress=compressor,input_revisions=vector,now_ms=now_ms,
                include_identity=not compressed_only)
            status["prepared_bytes"]+=package.allocation_bytes
            status["phase_ms"]["validated_wire"]+=int((time.perf_counter()-before)*1000)
            yield package
    iterator=packages()
    return (iterator if stream else list(iterator)),status


def witness_clock(record,observed_ms):
    """Envelope clock only; available fields retain their own validated clocks."""
    if type(record.get("last_collected_ms")) is int:return record["last_collected_ms"]
    clocks=[record[k] for k in ("last_hot_ms","cold_source_ms","global_source_ms",
        "top_source_ms","instrument_ms","funding_refreshed_ms","references_refreshed_ms",
        "legacy_source_ms") if type(record.get(k)) is int]
    # An empty witness records observed absence, not an available measurement.
    return min(clocks) if clocks else observed_ms


def prepare_funding(universe,records,publisher,bulk,info,premium,*,now_ms,compressed_only=False):
    """ADR-0107: publish the native live estimate without rings or slow snapshot compression."""
    import brotli
    mapping=publisher.mapping_version
    symbols={m["symbol"] for m in universe}
    subset={"ts":bulk["ts"],"prices":{m["pair_id"]:bulk["prices"][m["pair_id"]] for m in universe if m["pair_id"] in bulk["prices"]}}
    retained={s:{"instrument":r["instrument"],"instrument_ms":r["instrument_ms"]} for s,r in records.items()
        if r is not None and type(r.get("instrument")) is dict and type(r.get("instrument_ms")) is int}
    vector={};witnesses={}
    def register(name,value,stamp,expiry,definition):
        body=json.dumps(exact_json(value),sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        if compressed_only:body=pack_source(body)
        revision,witness=publisher.register_source(name,body,ttl_ms=LIVE_STAGE_MS)
        vector[name]=revision
        witnesses[name]=dict(version=witness,source_ms=stamp,verified_at_ms=now_ms,
            expires_ms=expiry,definition_version=definition)
    names=["funding:live:"+n for n in ("bulk","info","premium","records")]
    register(names[0],subset,bulk["ts"],bulk["ts"]+120000,"prices.bulk.v1")
    register(names[1],{s:r for s,r in info["data"].items() if s in symbols},info["source_ms"],info["source_ms"]+120000,"funding.metadata.v1")
    register(names[2],{s:r for s,r in premium["data"].items() if s in symbols},premium["source_ms"],premium["source_ms"]+120000,"funding.metadata.v1")
    stamps=[r["instrument_ms"] for r in retained.values()]
    register(names[3],retained,min(stamps) if stamps else bulk["ts"],
             (max(stamps) if stamps else bulk["ts"])+8*86400000,"funding.instrument.retained.v1")
    rows=[]
    for meta in universe:
        values=funding_values(meta,retained.get(meta["symbol"],{}),subset,info,premium,mapping,now_ms=now_ms)
        rows.append(funding_live_row(meta,*values,names))
    doc=dict(schema="pulse.live_funding.v1",mapping_version=mapping,sequence=now_ms,generated_ms=now_ms,
        expires_ms=now_ms+LIVE_ENVELOPE_MS,rows=rows,inputs=witnesses)
    return compose(doc,validate=lambda value:live(value,universe,mapping),
        brotli_compress=lambda b,q:brotli.compress(b,quality=q),input_revisions=vector,now_ms=now_ms,
        include_identity=not compressed_only)


def prepare_price(universe,publisher,bulk,*,now_ms,compressed_only=False):
    """Price-only package for the fast live path: no capsule reads, only the cohort's slice of the bulk batch is
    witnessed (about 15 KB, not the whole 150 KB response), so a tick every couple of seconds stays cheap."""
    import brotli
    mapping=publisher.mapping_version
    prices={m["pair_id"]:bulk["prices"][m["pair_id"]] for m in universe if m["pair_id"] in bulk["prices"]}
    subset={"ts":bulk["ts"],"prices":prices}
    body=json.dumps(exact_json(subset),sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    if compressed_only:body=pack_source(body)
    revision,witness=publisher.register_source("price:live",body,ttl_ms=LIVE_STAGE_MS)
    gate=lambda value:live(value,universe,mapping)
    doc=compose_live_price(universe,subset,mapping_version=mapping,batch_version=witness,sequence=now_ms,
        now_ms=now_ms,price_max_age_ms=60000,validate=gate)
    return compose(doc,validate=gate,brotli_compress=lambda b,q:brotli.compress(b,quality=q),
        input_revisions={"price:live":revision},now_ms=now_ms,include_identity=not compressed_only)


def asdict_ls(metrics):
    return {"series":{name:{"ratio":asdict(s["ratio"]),"current":s["current"],
        "evolution":{h:{"metric":asdict(v["metric"]),"baseline":v["baseline"]} for h,v in s["evolution"].items()}}
        for name,s in metrics["series"].items()},"divergence":asdict(metrics["divergence"])}
