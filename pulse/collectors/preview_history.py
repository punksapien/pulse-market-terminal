"""ADR-0126: bounded worker-only retained history, separate from table parts."""
from decimal import Decimal,localcontext,Context
import json

from .preview_pair import stores
from pulse.store.bucket_ring import BucketRing
from pulse.wire.composer import Publication
from pulse.wire.publication import prepare
from pulse.wire.compact import raw
from pulse.wire.identity import digest

METRICS=("open_interest","long_short","funding","funding_change","volatility")


def ring_points(frame,spec,*,now_ms):
    """One GETRANGE is an atomic frame snapshot; historical points keep their times."""
    if not frame:return []
    if (len(frame)!=spec.nbytes or frame[spec.ring_bytes:spec.ring_bytes+9]!=b"PULSEC11R"
            or frame[spec.ring_bytes+16:spec.ring_bytes+32]!=spec.mapping):
        raise ValueError("history_capsule_identity")
    ring=BucketRing.from_bytes(frame[:spec.ring_bytes])
    if (ring.step_ms,ring.capacity,ring.columns)!=(spec.step_ms,spec.capacity,spec.columns):
        raise ValueError("history_ring_dimensions")
    latest,_=ring._check_header()
    result=[]
    for slot in range(ring.capacity):
        if not ring._bit(slot):continue
        point,flags=ring._read_slot(slot)
        stamp,value=point[:2]
        if (flags & ring.CONFLICT or not latest-ring.capacity<stamp//spec.step_ms<=latest
                or not now_ms-86400000<=stamp<=now_ms+5000):continue
        if value<0 or (spec.metric.startswith("ls_") and value<=0):
            raise ValueError("history_invalid_value")
        result.append([stamp,repr(value)])
    return sorted(result)[-288:]


def series(points,unit,label):
    return dict(unit=unit,label=label,points=points,status="available" if len(points)>=2 else "unavailable",
        reason=None if len(points)>=2 else "insufficient_retained_history")


def row_history(meta,record,oi,ls):
    settled=record.get("funding_history",[])
    # Values retained only after settlement verification; history never pairs with a live estimate.
    changes=[]
    with localcontext(Context(prec=60)):
        for a,b in zip(settled,settled[1:]):
            if abs(b[0]-a[0]-meta["funding_hours"]*3600000)<=5000:
                changes.append([b[0],str(Decimal(b[1])-Decimal(a[1]))])
    return dict(id=meta["id"],pair_id=meta["pair_id"],series={
        "open_interest":series(oi,"quantity","Open interest quantity · 5m samples"),
        "long_short":series(ls,"ratio","Global accounts L/S · 5m samples"),
        "funding":series(settled,"fraction","Verified settled funding rate"),
        "funding_change":series(changes,"fraction","Consecutive settled funding change"),
        "volatility":{h:series(record.get("volatility_history",{}).get(h,[]),"fraction",h+" volatility score")
            for h in ("1H","4H","24H")}})


def prepare_histories(client,run_id,universe,records,publisher,*,now_ms,retention_ms=180000,include_identity=True):
    import brotli
    if not 1<=len(universe)<=50:raise ValueError("history_cohort_bound")
    ids=[m["id"] for m in universe]
    if (retention_ms not in (180000,720000) or len(set(ids))!=len(ids)
            or any(type(i) is not int or not 0<=i<350 for i in ids)):
        raise ValueError("history_release_bound")
    capsules=[]
    frames=[]
    pending=[]
    for meta in universe:
        ss=stores(client,run_id,meta)
        for store in (ss[1],ss[2]):
            store._scope()
            pending.append(store)
            capsules.append(store.spec)
            if len(pending)==16:
                pipe=client.pipeline(transaction=False)
                for item in pending:pipe.getrange(item.frame,0,item.spec.nbytes-1)
                frames.extend(pipe.execute())
                pending=[]
    if pending:
        pipe=client.pipeline(transaction=False)
        for item in pending:pipe.getrange(item.frame,0,item.spec.nbytes-1)
        frames.extend(pipe.execute())
    if len(frames)!=len(capsules):raise ValueError("history_pipeline_shape")
    rows=[]
    for i,meta in enumerate(universe):
        points=[]
        for at in (2*i,2*i+1):
            try:points.append(ring_points(frames[at],capsules[at],now_ms=now_ms))
            except ValueError:points.append([])
        rows.append(row_history(meta,records.get(meta["symbol"]) or {},*points))
    packages=[]
    for bucket in sorted({r["id"]//10 for r in rows}):
        doc=dict(schema="pulse.history.v1",mapping_version=publisher.mapping_version,
            bucket=bucket,generated_ms=now_ms,expires_ms=now_ms+retention_ms,rows=[r for r in rows if r["id"]//10==bucket])
        doc["generation"]=digest(doc)
        descriptor,blobs=prepare(doc,brotli_compress=lambda b,q:brotli.compress(b,quality=q),include_identity=include_identity)
        name="history:"+str(bucket)
        # ADR-0177: pin the already-required gzip blob, not a second encoded copy.
        witness=raw(doc) if include_identity else blobs[descriptor["variants"]["gzip"]["sha256"]]
        revision,_=publisher.register_source(name,witness)
        packages.append(Publication(name,publisher.mapping_version,now_ms,now_ms+retention_ms,
            ((name,revision),),raw(descriptor),((descriptor["logical_sha256"],raw(descriptor)),),tuple(blobs.items())))
    return packages
