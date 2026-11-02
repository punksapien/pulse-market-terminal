"""ADR-0130: synthetic prefix parity on the isolated30MB Preview DB, never pulse-redis."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from preview_redis_access import connect
from pulse_shared_capsule_parity import descriptors, vectors
from pulse.store.metric_capsule import MetricCapsule
from pulse.store.capsule_model import observation
from pulse.store.production_scope import ProductionScope, physical_key


def publication_parity(raw,scoped):
    import brotli
    from pulse.store.artifact_publisher import ArtifactPublisher
    from pulse.store.runtime_document import RuntimeDocument
    from pulse.wire.rows import empty_row,available,build
    from pulse.wire.identity import digest
    from pulse.wire.composer import compose
    from pulse.wire.validation import snapshot
    from pulse.http.wire_reader import response
    universe=[dict(id=0,pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=8)]
    mapping=digest(universe);run_id=uuid.uuid4().hex
    t=raw.time();now=t[0]*1000+t[1]//1000
    replies=[]
    for client in (raw,scoped):
        pub=ArtifactPublisher(client,run_id,mapping);pub.acquire()
        try:
            revision,witness=pub.register_source("scope:synthetic",b'{"synthetic":true}')
            row=empty_row(universe[0],mapping)
            row["price"]=available("price","price_change.v1","100.125",source_ms=now,
                expires_ms=now+60000,input_refs=["scope:synthetic"],now_ms=now)
            inputs={"scope:synthetic":dict(version=witness,source_ms=now,verified_at_ms=now,
                expires_ms=now+60000,definition_version="scope.synthetic.v1")}
            doc=build(universe,[row],inputs,horizon="1H",now_ms=now)
            package=compose(doc,validate=snapshot,brotli_compress=lambda b,q:brotli.compress(b,quality=q),
                input_revisions={"scope:synthetic":revision},now_ms=now)
            pub.publish([package],expected_revisions={package.channel:0})
            def store(name):return RuntimeDocument(client,run_id,name,mapping)
            common=dict(environment="production",production_active=True,store_factory=store,
                artifacts_factory=lambda:pub.artifacts,now_ms=now,accept_encoding="br")
            manifest=response({"horizon":["1H"]},**common)
            if manifest.status!=200:raise RuntimeError("scope_manifest_read_failed")
            descriptor=json.loads(manifest.body)["parts"]["core"]
            artifact=response({"artifact":[descriptor["logical_sha256"]]},**common)
            if artifact.status!=200 or artifact.headers.get("Content-Encoding")!="br":
                raise RuntimeError("scope_artifact_read_failed")
            replies.append((manifest.body,artifact.body))
        finally:pub.release()
    if replies[0]!=replies[1]:raise RuntimeError("scope_publication_parity_failed")
    return dict(run_id=run_id,manifest_sha256=hashlib.sha256(replies[0][0]).hexdigest(),
        compressed_core_sha256=hashlib.sha256(replies[0][1]).hexdigest(),encoding="br")


def run(cli):
    raw=connect(cli)  # Exact disposable resource identity and Preview-only credential gate.
    deadline=time.monotonic()+240
    cases=[]
    try:
        before=raw.info("memory")
        if (type(before.get("used_memory")) is not int or before["used_memory"]+3000000>=21000000
                or before.get("maxmemory_policy")!="noeviction"):
            raise RuntimeError("isolated_preview_capacity_refused")
        scoped=ProductionScope(raw)
        for spec in descriptors():
            if time.monotonic()>=deadline:raise RuntimeError("parity_deadline")
            if raw.info("memory")["used_memory"]+1000000>=21000000:raise RuntimeError("isolated_preview_memory_stop")
            run_id=uuid.uuid4().hex
            left=MetricCapsule(raw,run_id,spec,approved_plan_fallback=True)
            right=MetricCapsule(scoped,run_id,spec,approved_plan_fallback=True)
            left.acquire();right.acquire()
            try:
                left.initialise();right.initialise()
                t=raw.time();now=t[0]*1000+t[1]//1000
                statuses=[];points=vectors(spec,now)
                for point in points:
                    if time.monotonic()>=deadline:raise RuntimeError("parity_deadline")
                    a=left.write(point);b=right.write(point)
                    if a[0]!=b[0] or raw.get(left.frame)!=raw.get(physical_key(right.frame)):
                        raise RuntimeError("scope_frame_parity_failed")
                    if left.read()!=right.read():raise RuntimeError("scope_read_parity_failed")
                    statuses.append(a[0].decode())
                point=points[-1]
                batch=[observation(spec,point.source_ms+i+1,point.sources) for i in range(16)]
                a=left.write_many(batch);b=right.write_many(batch)
                if [r[0] for r in a]!=[r[0] for r in b] or raw.get(left.frame)!=raw.get(physical_key(right.frame)):
                    raise RuntimeError("scope_batch_parity_failed")
                point=batch[-1]
                if left.baseline(point.source_ms,spec.step_ms)!=right.baseline(point.source_ms,spec.step_ms):
                    raise RuntimeError("scope_baseline_parity_failed")
                frame=raw.get(physical_key(right.frame))
                owner=right.owner
                try:
                    right.owner=b"stale-owner"
                    if right.write(point)!=[b"fenced_out"] or raw.get(physical_key(right.frame))!=frame:
                        raise RuntimeError("scope_fencing_failed")
                finally:right.owner=owner
                if raw.ttl(left.frame)!=scoped.ttl(right.frame) or scoped.ttl(right.frame)!=-1:
                    raise RuntimeError("scope_lifecycle_failed")
                cases.append(dict(metric=spec.metric,logical_frame=left.frame,physical_frame=physical_key(right.frame),
                    sha256=hashlib.sha256(frame).hexdigest(),statuses=statuses,batch_samples=len(batch)))
            finally:
                left.release();right.release()
        if time.monotonic()>=deadline:raise RuntimeError("parity_deadline")
        publication=publication_parity(raw,scoped)
        return dict(status="production_prefix_parity_passed_on_isolated_preview_db",production_access=False,publication=publication,
            exchange_calls=0,deletions=0,write_pairs=sum(len(c["statuses"])+c["batch_samples"] for c in cases),cases=cases,used_memory_before=before["used_memory"],
            used_memory_after=raw.info("memory")["used_memory"])
    finally:raw.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    args=parser.parse_args()
    try:print(json.dumps(run(args.vercel_cli)))
    except Exception as exc:
        print(json.dumps(dict(status="parity_failed",error_type=type(exc).__name__,production_access=False)))
        raise SystemExit(1) from None
