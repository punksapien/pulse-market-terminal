"""ADR-0171 four fixed historical buckets per pass; no exchange imports or calls."""
import os,time
from .preview_runtime import connect
from .release_universe import load,capsule_run
from .preview_history import prepare_histories
from .snapshot_runtime import clock
from pulse.store.artifact_publisher import ArtifactPublisher
from pulse.store.worker_state import WorkerState
from pulse.store.runtime_document import RuntimeDocument
from pulse.store.production_scope import release_enabled


def run(*,run_id,mapping_version,duration_ms,env=None,deps=None):
    env=os.environ if env is None else env
    if not release_enabled(env):raise ValueError("history_preview_release_required")
    if type(duration_ms) is not int or not 1<=duration_ms<=55000:raise ValueError("history_duration_refused")
    d=dict(connect=connect,load=load,Publisher=ArtifactPublisher,State=WorkerState,
        Document=RuntimeDocument,prepare=prepare_histories,clock=clock,now=time.monotonic);d.update(deps or {})
    started=d["now"]();end=started+duration_ms/1000;client=d["connect"](env)
    pub=d["Publisher"](client,run_id,mapping_version,lane="history")
    try:
        try:pub.acquire()
        except RuntimeError as e:
            if str(e)=="publication_lease_busy":return {"status":"lease_busy"}
            raise
        universe=d["load"](client,run_id,mapping_version,now_ms=d["clock"](client))
        state=d["State"](pub);cursor=state.read("HISTORY_CURSOR")
        index=(cursor or {}).get("index",0)
        if type(index) is not int or not 0<=index<35:raise ValueError("history_cursor_refused")
        published=[];allocation=0
        for offset in range(4):
            if d["now"]()+5>=end:break
            bucket=(index+offset)%35;batch=universe[bucket*10:(bucket+1)*10]
            if not batch:raise ValueError("history_membership_refused")
            pub.renew();records=state.read_many([m["symbol"] for m in batch])
            packages=d["prepare"](client,capsule_run(env,run_id),batch,records,pub,
                now_ms=d["clock"](client),retention_ms=720000,include_identity=False)
            if len(packages)!=1 or packages[0].channel!="history:"+str(bucket):raise ValueError("history_bucket_refused")
            if d["now"]()+3>=end:break
            p=packages[0];doc=d["Document"](client,run_id,p.channel,mapping_version)
            revision=doc.read(now_ms=d["clock"](client))["revision"]
            pub.publish([p],expected_revisions={p.channel:revision})
            state.write("HISTORY_CURSOR",{"index":(bucket+1)%35})
            published.append(bucket);allocation+=p.allocation_bytes
        return dict(status="published" if published else "incomplete",pairs=len(universe),
            history_buckets=published,allocation_bytes=allocation,source_dispatches=0,
            elapsed_ms=int((d["now"]()-started)*1000))
    finally:
        try:pub.release()
        finally:client.close()
