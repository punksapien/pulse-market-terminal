"""ADR-0147: only the two explicitly approved retired staging state hashes."""
import hashlib
import json
import os
import time

from .preview_runtime import connect
from .release_universe import load
from pulse.store.runtime_document import RuntimeDocument,document

OLD_RUN="28c01310000000000000000000000001"
NEW_RUN="28c01440000000000000000000000001"
OLD_PREFIX="pulse:stg:{"+OLD_RUN+"}:publication:"
KEYS=(OLD_PREFIX+"worker_state",OLD_PREFIX+"worker_funding")
COPY_PROOF="""if #ARGV<1 or #ARGV>16 then return 0 end
for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return 0 end end
for _,name in ipairs(ARGV) do
 if redis.call('HEXISTS',KEYS[1],name)==1 and redis.call('HSTRLEN',KEYS[2],name)==0 then return 0 end
end
return 1"""
UNLINK="""if KEYS[1]~='OLD_LEASE' then return -1 end
if KEYS[2]~='OLD_STATE' and KEYS[2]~='OLD_FUNDING' then return -1 end
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('PTTL',KEYS[1])<60000 then return -1 end
if redis.call('TYPE',KEYS[2]).ok~='hash' or redis.call('HLEN',KEYS[2])~=tonumber(ARGV[2]) then return -1 end
local before=redis.call('TIME');local removed=redis.call('UNLINK',KEYS[2]);local after=redis.call('TIME')
return {removed,(tonumber(after[1])-tonumber(before[1]))*1000000+tonumber(after[2])-tonumber(before[2])}""".replace('OLD_LEASE',OLD_PREFIX+'lease').replace('OLD_STATE',KEYS[0]).replace('OLD_FUNDING',KEYS[1])


def clock(client):
    seconds,micros=client.time();return seconds*1000+micros//1000


def checked_unlink(client,entry,owner):
    if type(entry) is not dict or entry.get('key') not in KEYS:
        raise ValueError('retired_cleanup_key_refused')
    key=entry['key']
    raw=client.dump(key)
    if raw is None:return dict(outcome='already_absent',unlink_us=None)
    if (hashlib.sha1(raw).hexdigest()!=entry.get('dump_sha1') or len(raw)!=entry.get('dump_bytes')):
        raise RuntimeError('retired_cleanup_dump_changed')
    # DUMP hashing stays in the worker, not a multi-MB blocking Lua script. The
    # parked old lease prevents every legitimate writer; Lua rechecks that fence.
    result=client.eval(UNLINK,2,OLD_PREFIX+'lease',key,owner,entry['hash_fields'])
    if not isinstance(result,list) or len(result)!=2 or result[0]!=1:
        raise RuntimeError('retired_cleanup_fence_or_shape_changed')
    return dict(outcome='unlinked',unlink_us=int(result[1]))


def run(*,run_id,mapping_version,duration_ms,env=None):
    env=os.environ if env is None else env
    if (env.get('VERCEL_ENV')!='preview' or env.get('PULSE_RETIRED_STATE_CLEANUP_APPROVED')!='1'
            or run_id!=NEW_RUN or env.get('PULSE_CAPSULE_RUN_ID')!=OLD_RUN
            or env.get('PULSE_RELEASE_TOP350')!='1'):
        raise ValueError('retired_cleanup_not_approved')
    if type(duration_ms) is not int or not 1<=duration_ms<=55000:raise ValueError('retired_cleanup_duration')
    deadline=time.monotonic()+duration_ms/1000
    def check_time():
        if time.monotonic()+5>=deadline:raise RuntimeError('retired_cleanup_time_budget')
    client=connect(env)
    manifest=RuntimeDocument(client,NEW_RUN,'source:retired_cleanup','pulse.retired.cleanup.v1')
    owner=('release-handoff:'+NEW_RUN).encode()
    try:
        if client.get(OLD_PREFIX+'lease')!=owner or client.pttl(OLD_PREFIX+'lease')<60000:
            raise RuntimeError('retired_cleanup_writer_not_retired')
        rows=load(client,NEW_RUN,mapping_version,now_ms=clock(client))
        names=[row['symbol'] for row in rows]
        new_key='pulse:stg:{'+NEW_RUN+'}:publication:worker_state'
        for offset in range(0,len(names),16):
            check_time()
            if client.eval(COPY_PROOF,2,KEYS[0],new_key,*names[offset:offset+16])!=1:
                raise RuntimeError('retired_cleanup_copy_incomplete')
        manifest.acquire();now=clock(client);previous=manifest.read(now_ms=now)
        if previous['status']=='available':
            value=json.loads(previous['payload'])
            if (value.get('approved_keys')!=list(KEYS) or value.get('target_mapping')!=mapping_version
                    or type(value.get('entries')) is not list
                    or [entry.get('key') for entry in value['entries']]!=list(KEYS)):
                raise ValueError('retired_cleanup_manifest_refused')
        elif previous.get('reason')=='document_missing':
            entries=[]
            for key in KEYS:
                check_time()
                raw=client.dump(key)
                if raw is not None and client.type(key)!=b'hash':raise ValueError('retired_cleanup_type_refused')
                entries.append(dict(key=key,dump_sha1=hashlib.sha1(raw).hexdigest() if raw is not None else None,
                    dump_bytes=len(raw) if raw is not None else 0,hash_fields=client.hlen(key) if raw is not None else 0))
            value=dict(approved_keys=list(KEYS),target_mapping=mapping_version,entries=entries)
            outcome=manifest.publish(document(value,source_ms=now,expires_ms=now+86400000),expected_revision=previous['revision'])
            if outcome[0] not in (b'published',b'duplicate'):raise RuntimeError('retired_cleanup_manifest_not_saved')
        else:raise RuntimeError('retired_cleanup_manifest_expired')
        before=client.info('memory').get('used_memory')
        result=[]
        for entry in value['entries']:
            check_time()
            manifest.renew()
            result.append(dict(**entry,**checked_unlink(client,entry,owner)))
        return dict(status='published',cleanup=result,used_memory_before=before,
            used_memory_after=client.info('memory').get('used_memory'),
            deletions=sum(item['outcome']=='unlinked' for item in result),production_access=False,exchange_calls=0)
    finally:
        try:
            if manifest.owner:
                client.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('PEXPIRE',KEYS[1],1) end return 0",
                    1,manifest.lease,manifest.owner)
        finally:client.close()
