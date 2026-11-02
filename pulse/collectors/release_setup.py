"""ADR-0144: operator-only, bounded Preview membership seal and history handover."""
import json
import os
import time

from .preview_runtime import connect,fetch_bulk
from .preview_sources import Sources
from .universe_registry import load as load_source
from .release_universe import freeze_top_volume,load,capsule_run,IDENTITY
from pulse.store.runtime_document import RuntimeDocument,document
from pulse.store.artifact_publisher import ArtifactPublisher
from pulse.store.worker_state import WorkerState

RETIRE="""for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return 0 end end
local current=redis.call('GET',KEYS[1])
if current and current~=ARGV[1] then return 0 end
if not current then redis.call('INCR',KEYS[2]) end
redis.call('SET',KEYS[1],ARGV[1],'PX',2592000000)
return 1"""


def seed_record(record):
    """Raw witnesses retain their clocks; mapping-bound results must be recomputed."""
    from copy import deepcopy
    result=deepcopy(record)
    for key in ("derived","last_collected_ms","funding_report","funding_refreshed_ms",
                "funding_attempt_ms","funding_attempt_nft","funding_refresh_reason","schedule_retry_ms","nft"):
        result.pop(key,None)
    return result


def run(*,run_id,mapping_version,duration_ms,env=None):
    env=os.environ if env is None else env
    if env.get("VERCEL_ENV")!="preview" or env.get("PULSE_RELEASE_TOP350")!="1":
        raise ValueError("release_setup_preview_only")
    if type(duration_ms) is not int or not 1<=duration_ms<=55000:
        raise ValueError("release_setup_duration")
    data_run=capsule_run(env,run_id)
    if data_run==run_id:raise ValueError("release_setup_source_required")
    source_mapping=env.get("PULSE_RELEASE_SOURCE_MAPPING")
    deadline=time.monotonic()+duration_ms/1000
    client=connect(env)
    manifest=RuntimeDocument(client,run_id,"source:release350",IDENTITY)
    publisher=None
    try:
        # No active old collector may race the new release's shared raw capsules.
        old=ArtifactPublisher(client,data_run,source_mapping)
        if client.eval(RETIRE,2,old.lease,old.fence,"release-handoff:"+run_id)!=1:
            raise RuntimeError("release_source_writer_active")
        manifest.acquire()
        seconds,micros=client.time();now=seconds*1000+micros//1000
        prior=manifest.read(now_ms=now)
        if prior["status"]=="available":
            value=json.loads(prior["payload"])
            rows=load(client,run_id,value.get("mapping_version"),now_ms=now)
        elif prior.get("reason")=="document_missing":
            rows=load_source(client,data_run,source_mapping,now_ms=now)
            source=Sources(client,scope="vercel-bom1-all-v1",deadline=deadline-5)
            source.check_lease=manifest.renew
            source.initialise()
            bulk=fetch_bulk(source)
            value=freeze_top_volume(rows,bulk,now_ms=source.clock())
            result=manifest.publish(document(value,source_ms=value["source_ms"],
                expires_ms=value["source_ms"]+30*86400000),expected_revision=prior["revision"])
            if result[0] not in (b"published",b"duplicate"):
                raise RuntimeError("release_manifest_seal_refused")
            rows=value["universe"]
        else:raise RuntimeError("release_manifest_not_replaceable")
        publisher=ArtifactPublisher(client,run_id,value["mapping_version"])
        publisher.acquire()
        state=WorkerState(publisher)
        old_state=WorkerState(old)
        names=[r["symbol"] for r in rows]
        existing=state.read_many(names)
        retained=old_state.read_many(names)
        copied=0;pending=0
        for name in names:
            if existing[name] is not None or retained[name] is None:continue
            if time.monotonic()+5>=deadline:
                pending+=1;continue
            manifest.renew();publisher.renew()
            state.write(name,seed_record(retained[name]));copied+=1
        return dict(status="published" if pending==0 else "incomplete",
            release_status="release_ready" if pending==0 else "release_seeding",pairs=len(rows),
            mapping_version=value["mapping_version"],ranking_sha256=value["ranking_sha256"],
            volume_source_ms=value["source_ms"],copied_records=copied,pending_records=pending,
            release_universe=rows,cutoff=value["ranking"][349],
            deletions=0,production_access=False)
    finally:
        try:
            if publisher is not None:publisher.release()
            if manifest.owner:
                client.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('PEXPIRE',KEYS[1],1) end return 0",
                    1,manifest.lease,manifest.owner)
        finally:client.close()
