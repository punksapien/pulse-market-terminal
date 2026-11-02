"""ADR-0092 fenced staging writes; readers never import this module."""
import hashlib
import json
import re
import uuid

from .metric_capsule import ACQUIRE, RENEW
from .runtime_document import RuntimeDocument
from .published_artifacts import PublishedArtifacts, descriptor_valid, MAX_BLOB_BYTES
from .expansion_guard import guard
from .staging_policy import staging_key
from pulse.wire.composer import Publication, revisions
from pulse.wire.publication import byte_digest
from pulse.wire.rows import SNAPSHOT_ENVELOPE_MS

LIVE_STAGE_MS = 60000  # ADR-0121: temporary bytes, not observation freshness.
LIVE_ARTIFACT_STAGE_MS = 15000  # ADR-0196: promotion retains live bytes through expiry + 15s grace.

STAGE="""for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return {'scope_refused'} end end
local started=redis.call('TIME')
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('PTTL',KEYS[1])<=0 then return {'fenced_out'} end
local ttl=tonumber(ARGV[3])
if not ttl or ttl<1000 or ttl>300000 or #ARGV[2]>1114112 then return {'stage_bound_refused'} end
local old=redis.call('GET',KEYS[2])
if old and old~=ARGV[2] then return {'immutable_conflict'} end
if not old then redis.call('SET',KEYS[2],ARGV[2],'PX',ttl)
elseif redis.call('PTTL',KEYS[2])<ttl then redis.call('PEXPIRE',KEYS[2],ttl) end
local ended=redis.call('TIME')
return {'staged',tostring((tonumber(ended[1])-tonumber(started[1]))*1000000+tonumber(ended[2])-tonumber(started[2]))}"""

REGISTER="""for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return {'scope_refused'} end end
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('PTTL',KEYS[1])<=0 then return {'fenced_out'} end
if redis.call('EXISTS',KEYS[3])~=1 or #ARGV[2]>128 or #ARGV[3]~=71 then return {'witness_missing'} end
local n=tonumber(redis.call('HGET',KEYS[2],'r:'..ARGV[2]) or '0')
local previous=redis.call('HGET',KEYS[2],'w:'..ARGV[2])
if previous==ARGV[3] then return {'registered',tostring(n)} end
if n>=9007199254740990 then return {'revision_overflow'} end
redis.call('HSET',KEYS[2],'r:'..ARGV[2],n+1,'w:'..ARGV[2],ARGV[3])
return {'registered',tostring(n+1)}"""

PROMOTE="""for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return {'scope_refused'} end end
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('PTTL',KEYS[1])<=0 then return {'fenced_out'} end
local t=redis.call('TIME'); local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
local plan=cjson.decode(ARGV[2]); local inputs=cjson.decode(ARGV[3]); local sizes=cjson.decode(ARGV[4])
if #plan<1 or #plan>5 or #KEYS>167 then return {'promotion_bound_refused'} end
for name,revision in pairs(inputs) do
 if tonumber(redis.call('HGET',KEYS[2],'r:'..name) or '-1')~=revision then return {'input_revision_conflict'} end
end
local keep=0;local source_keep=0;local snapshot_only=true
for i,p in ipairs(plan) do
 local maxage=SNAPSHOT_MAX_AGE_MS
 if p.history==true then maxage=720000 end
 if p.source>now+5000 or p.source<0 or p.expiry<=now or p.expiry<=p.source or p.expiry>p.source+maxage then return {'invalid_time'} end
 if #p.payload>131072 or #p.digest~=64 then return {'invalid_document'} end
 local key=KEYS[2+i]
 local identity=redis.call('HGET',key,'identity')
 if identity and identity~=p.identity then return {'identity_conflict'} end
 local revision=tonumber(redis.call('HGET',key,'revision') or '0')
 if revision~=p.expected or revision>=9007199254740990 then return {'revision_conflict'} end
 local source=tonumber(redis.call('HGET',key,'source') or '-1')
 if source>p.source then return {'source_regression'} end
 if source==p.source and redis.call('HGET',key,'digest')~=p.digest then return {'same_source_conflict'} end
 if redis.call('HSTRLEN',key,'payload')>131072 then return {'stored_size_refused'} end
 if p.grace~=15000 and p.grace~=120000 then return {'invalid_retention'} end
 if p.grace~=120000 or p.history==true then snapshot_only=false end
 if p.grace==15000 and p.expiry>p.source+15000 then return {'invalid_time'} end
 if p.expiry+p.grace>keep then keep=p.expiry+p.grace end
 if p.expiry>source_keep then source_keep=p.expiry end
end
local source_only=plan[1].source_only or {}
if type(source_only)~='table' then return {'invalid_retention_vector'} end
local source_count=0
for index,flag in pairs(source_only) do
 source_count=source_count+1
 local n=tonumber(index)
 if source_count>#KEYS-2-#plan or not snapshot_only or flag~=true or not n or n~=math.floor(n) or tostring(n)~=index or n<1 or n>#KEYS-2-#plan then return {'invalid_retention_vector'} end
end
for i=3+#plan,#KEYS do
 if redis.call('STRLEN',KEYS[i])~=sizes[i-2-#plan] or redis.call('PTTL',KEYS[i])<=0 then return {'dependency_missing'} end
end
for i=3+#plan,#KEYS do
 local expiry=source_only[tostring(i-2-#plan)] and source_keep or keep
 if redis.call('PTTL',KEYS[i])<expiry-now then redis.call('PEXPIREAT',KEYS[i],expiry) end
end
for i,p in ipairs(plan) do
 local key=KEYS[2+i]; local previous=redis.call('HGET',key,'payload')
 if redis.call('HGET',key,'digest')~=p.digest then
  if previous then redis.call('HSET',key,'previous_payload',previous,'previous_digest',redis.call('HGET',key,'digest')) end
  redis.call('HSET',key,'payload',p.payload,'digest',p.digest,'source',p.source,'expiry',p.expiry,
   'identity',p.identity,'revision',p.expected+1,'failure','none','attempt_ms',now)
 end
end
local ended=redis.call('TIME')
return {'published',tostring((tonumber(ended[1])-tonumber(t[1]))*1000000+tonumber(ended[2])-tonumber(t[2]))}""".replace("SNAPSHOT_MAX_AGE_MS", str(SNAPSHOT_ENVELOPE_MS))


class ArtifactPublisher:
    def __init__(self, client, run_id, mapping_version,*,lane="main"):
        if lane not in ("main","snapshot","history","live"):raise ValueError("publication_lane_refused")
        self.client=client
        self.artifacts=PublishedArtifacts(client,run_id,mapping_version)
        self.run_id,self.mapping_version=run_id,mapping_version
        self.prefix=staging_key("pulse:stg:{"+run_id+"}:publication:")
        self.lease,self.fence,self.inputs=(staging_key(self.prefix+n) for n in ("lease","fence","inputs"))
        if lane!="main":
            self.lease,self.fence=(staging_key(self.prefix+lane+":"+n) for n in ("lease","fence"))
        self.owner=None
        self.script_timings=[]

    def preflight(self, extra=0):
        guard(self.client.info("memory"),additional_bytes=extra,approved_plan_fallback=True)

    def acquire(self):
        self.preflight()
        self.owner=self.client.eval(ACQUIRE,2,self.lease,self.fence,uuid.uuid4().hex)
        if not self.owner: raise RuntimeError("publication_lease_busy")

    def renew(self):
        if not self.owner or self.client.eval(RENEW,1,self.lease,self.owner)!=1:
            raise RuntimeError("publication_fenced_out")

    def release(self):
        # Only the current owner shortens its ephemeral lease; never a data-key delete.
        if self.owner is not None:
            self.client.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('PEXPIRE',KEYS[1],1) end return 0",
                1,self.lease,self.owner)
            self.owner=None

    def stage(self, key, body, *, ttl_ms=180000):
        staging_key(key)
        if not key.startswith(self.prefix) or type(body) is not bytes or not 1<=len(body)<=MAX_BLOB_BYTES:
            raise ValueError("invalid_staged_artifact")
        self.preflight(3*len(body)+4096)
        result=self.client.eval(STAGE,2,self.lease,key,self.owner or b"",body,ttl_ms)
        if not result or result[0]!=b"staged": raise RuntimeError("artifact_stage_refused:"+result[0].decode())
        if len(result)>1: self.script_timings.append(("stage",int(result[1])))

    def register_source(self, name, body, *, ttl_ms=180000):
        if not isinstance(name,str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}",name):
            raise ValueError("invalid_source_name")
        identifier=byte_digest(body)
        key=self.artifacts.key("blob",identifier)
        self.stage(key,body,ttl_ms=ttl_ms)
        result=self.client.eval(REGISTER,3,self.lease,self.inputs,key,self.owner or b"",name,identifier)
        if result[0]!=b"registered": raise RuntimeError("source_registration_refused:"+result[0].decode())
        return int(result[1]),identifier

    def publish(self, publications, *, expected_revisions):
        if (not isinstance(publications,(tuple,list)) or not 1<=len(publications)<=5
                or any(not isinstance(p,Publication) for p in publications)
                or len({p.channel for p in publications})!=len(publications)):
            raise ValueError("invalid_publication_batch")
        vector={};entries={};plan=[];channels=[]
        for p in publications:
            if p.mapping_version!=self.mapping_version: raise ValueError("publication_mapping_conflict")
            if type(p.pointer) is not bytes or not 1<=len(p.pointer)<=131072:
                raise ValueError("publication_pointer_limit")
            pointer=json.loads(p.pointer)
            supplied={key:json.loads(body) for key,body in p.descriptors}
            if p.channel.startswith("snapshot:"):
                if (pointer.get("schema")!="pulse.manifest.v1" or
                        {v["logical_sha256"]:v for v in pointer.get("parts",{}).values()}!=supplied):
                    raise ValueError("publication_pointer_dependencies")
            elif p.channel in ("live_price_v1","live_funding_v1") or re.fullmatch(r"history:(?:[0-9]|[12][0-9]|3[0-4])",p.channel):
                if supplied!={pointer.get("logical_sha256"):pointer}:
                    raise ValueError("publication_pointer_dependencies")
            else: raise ValueError("publication_channel_refused")
            for name,rev in revisions(dict(p.input_revisions)):
                if name in vector and vector[name]!=rev: raise ValueError("publication_input_conflict")
                vector[name]=rev
            if len(vector)>256: raise ValueError("publication_input_limit")
            for identifier,body in p.blobs:
                if byte_digest(body)!=identifier: raise ValueError("publication_blob_hash")
                key=self.artifacts.key("blob",identifier)
                if key in entries and entries[key]!=body: raise ValueError("publication_blob_collision")
                entries[key]=body
            for identifier,body in p.descriptors:
                descriptor=descriptor_valid(json.loads(body),logical_hash=identifier,mapping_version=self.mapping_version)
                for variant in descriptor["variants"].values():
                    data=dict(p.blobs).get(variant["sha256"])
                    if data is None or len(data)!=variant["bytes"]: raise ValueError("publication_variant_missing")
                entries[self.artifacts.key("artifact",identifier)]=body
            expected=expected_revisions.get(p.channel)
            if type(expected) is not int or not 0<=expected<9007199254740991:
                raise ValueError("invalid_expected_revision")
            store=RuntimeDocument(self.client,self.run_id,p.channel,self.mapping_version)
            channels.append(store.data)
            history_channel=p.channel.startswith("history:")
            limit=720000 if history_channel else SNAPSHOT_ENVELOPE_MS if p.channel.startswith("snapshot:") else 15000
            if not p.generated_ms<p.expires_ms<=p.generated_ms+limit:raise ValueError("publication_expiry_refused")
            plan.append(dict(source=p.generated_ms,expiry=p.expires_ms,payload=p.pointer.decode("utf-8"),history=history_channel,
                digest=hashlib.sha256(p.pointer).hexdigest(),identity=self.mapping_version,expected=expected,
                grace=120000 if p.channel.startswith(("snapshot:","history:")) else 15000))
        if len(entries)>160 or sum(len(v) for v in entries.values())>8*1024*1024:
            raise ValueError("publication_allocation_limit")
        self.preflight(3*sum(len(v) for v in entries.values())+65536)
        stage_ms=180000 if any(p.channel.startswith(("snapshot:","history:")) for p in publications) else LIVE_ARTIFACT_STAGE_MS
        for key,body in entries.items():
            self.renew();self.stage(key,body,ttl_ms=stage_ms)
        # Source witnesses are promotion dependencies too; a vanished witness fails closed.
        for name in vector:
            witness=self.client.hget(self.inputs,"w:"+name)
            if type(witness) is not bytes: raise ValueError("publication_witness_missing")
            key=self.artifacts.key("blob",witness.decode("ascii"))
            size=self.client.strlen(key)
            if not 1<=size<=MAX_BLOB_BYTES: raise ValueError("publication_witness_missing")
            if key not in entries: entries[key]=None
        if len(entries)>160: raise ValueError("publication_dependency_limit")
        keys=list(entries)
        sizes=[len(entries[k]) if entries[k] is not None else self.client.strlen(k) for k in keys]
        # ADR-0179: public/shared artifacts keep full grace; only private proofs omit post-expiry grace.
        plan[0]["source_only"]={str(i+1):True for i,k in enumerate(keys) if entries[k] is None}
        if not all(p.channel.startswith("snapshot:") for p in publications):plan[0]["source_only"]={}
        self.renew()
        result=self.client.eval(PROMOTE,2+len(channels)+len(keys),self.lease,self.inputs,*channels,*keys,
            self.owner or b"",json.dumps(plan,separators=(",",":")),json.dumps(vector),json.dumps(sizes))
        if not result or result[0]!=b"published": raise RuntimeError("publication_promotion_refused:"+result[0].decode())
        if len(result)>1: self.script_timings.append(("promote",int(result[1])))
        return {"status":"published","channels":[p.channel for p in publications],"artifacts":len(keys)}
