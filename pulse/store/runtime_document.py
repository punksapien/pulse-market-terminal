"""ADR-0074 bounded staging latest/previous documents; no connection on import."""
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

from .expansion_guard import guard
from .metric_capsule import ACQUIRE, RENEW
from .staging_policy import staging_key

MAX_BYTES = 131072
COMMIT = """for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return {'scope_refused'} end end
if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return {'fenced_out'} end
local clock=redis.call('TIME'); local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
local source,expiry=tonumber(ARGV[3]),tonumber(ARGV[4])
if not source or not expiry or source>now+5000 or source<0 or expiry<=now or expiry<=source then return {'invalid_time'} end
if #ARGV[5]>131072 or #ARGV[6]~=64 or #ARGV[7]>128 then return {'invalid_document'} end
local identity=redis.call('HGET',KEYS[1],'identity')
if identity and identity~=ARGV[7] then return {'identity_conflict'} end
local rev=tonumber(redis.call('HGET',KEYS[1],'revision') or '0')
if rev~=tonumber(ARGV[2]) then return {'revision_conflict'} end
local oldsource=tonumber(redis.call('HGET',KEYS[1],'source') or '-1')
if source<oldsource then return {'source_regression'} end
local olddigest=redis.call('HGET',KEYS[1],'digest')
if olddigest==ARGV[6] then
 redis.call('HSET',KEYS[1],'failure','none','attempt_ms',now)
 return {'duplicate',tostring(rev)}
end
if source==oldsource then return {'same_source_conflict'} end
local previous=redis.call('HGET',KEYS[1],'payload')
if previous then
 if #previous>131072 then return {'stored_size_refused'} end
 redis.call('HSET',KEYS[1],'previous_payload',previous,'previous_digest',olddigest)
end
redis.call('HSET',KEYS[1],'payload',ARGV[5],'digest',ARGV[6],'source',ARGV[3],'expiry',ARGV[4],
 'identity',ARGV[7],'revision',rev+1,'failure','none','attempt_ms',now)
return {'published',tostring(rev+1)}"""
FAILURE = """if string.sub(KEYS[1],1,10)~='pulse:stg:' or string.sub(KEYS[2],1,10)~='pulse:stg:' then return 0 end
if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 or #ARGV[2]>64 then return 0 end
local t=redis.call('TIME')
redis.call('HSET',KEYS[1],'failure',ARGV[2],'attempt_ms',tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000))
return 1"""


@dataclass(frozen=True)
class Document:
    payload: bytes
    digest: str
    source_ms: int
    expires_ms: int


def document(value, *, source_ms, expires_ms):
    if (type(source_ms) is not int or type(expires_ms) is not int
            or not 0<=source_ms<expires_ms<=9007199254740991):
        raise ValueError("invalid_document_time")
    payload = json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    if len(payload)>MAX_BYTES:
        raise ValueError("document_size_exceeded")
    return Document(payload,hashlib.sha256(payload).hexdigest(),source_ms,expires_ms)


class RuntimeDocument:
    def __init__(self,client,run_id,channel,identity):
        if (not isinstance(run_id,str) or not re.fullmatch(r"[0-9a-f]{32}",run_id)
                or not isinstance(channel,str) or not re.fullmatch(r"[a-z0-9:_-]{1,80}",channel)
                or not isinstance(identity,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}",identity)):
            raise ValueError("runtime_document_identity")
        self.client,self.identity=client,identity
        prefix="pulse:stg:{"+run_id+"}:runtime:"+channel+":"
        self.data,self.lease,self.fence=tuple(staging_key(prefix+k) for k in ("data","lease","fence"))
        self._keys=(self.data,self.lease,self.fence)
        self.owner=None

    def scope(self):
        if (self.data,self.lease,self.fence)!=self._keys:
            raise ValueError("runtime_document_scope_refused")
        for key in self._keys: staging_key(key)

    def preflight(self,extra=0):
        self.scope()
        guard(self.client.info("memory"),additional_bytes=extra,approved_plan_fallback=True)

    def acquire(self):
        self.preflight()
        self.owner=self.client.eval(ACQUIRE,2,self.lease,self.fence,uuid.uuid4().hex)
        if not self.owner: raise RuntimeError("runtime_document_lease_busy")
        return self.owner

    def renew(self):
        self.preflight()
        if not self.owner or self.client.eval(RENEW,1,self.lease,self.owner)!=1:
            raise RuntimeError("runtime_document_fenced_out")

    def publish(self,candidate,*,expected_revision):
        if (not isinstance(candidate,Document) or type(expected_revision) is not int
                or not 0<=expected_revision<9007199254740991
                or type(candidate.payload) is not bytes or type(candidate.source_ms) is not int
                or type(candidate.expires_ms) is not int
                or not 0<=candidate.source_ms<candidate.expires_ms<=9007199254740991
                or len(candidate.payload)>MAX_BYTES
                or hashlib.sha256(candidate.payload).hexdigest()!=candidate.digest):
            raise ValueError("invalid_document_candidate")
        self.preflight(2*len(candidate.payload)+4096)
        result=self.client.eval(COMMIT,2,self.data,self.lease,self.owner or b"",expected_revision,
            candidate.source_ms,candidate.expires_ms,candidate.payload,candidate.digest,self.identity)
        self.preflight()
        return result

    def failed(self,reason):
        if not isinstance(reason,str) or not re.fullmatch(r"[a-z0-9_]{1,64}",reason):
            raise ValueError("invalid_refresh_reason")
        self.preflight(4096)
        if self.client.eval(FAILURE,2,self.data,self.lease,self.owner or b"",reason)!=1:
            raise RuntimeError("runtime_document_fenced_out")

    def read(self,*,now_ms):
        self.scope()
        if type(now_ms) is not int or not 0<=now_ms<=9007199254740991:
            raise ValueError("invalid_observation_clock")
        fields=self.client.hmget(self.data,"payload","digest","source","expiry","identity","revision","failure","attempt_ms")
        payload,digest,source,expiry,identity,revision,failure,attempt=fields
        result={"status":"unavailable","reason":"document_missing","revision":0}
        if payload is None: return result
        try:
            if len(payload)>MAX_BYTES or identity.decode()!=self.identity or hashlib.sha256(payload).hexdigest()!=digest.decode():
                raise ValueError()
            source,expiry,revision=int(source),int(expiry),int(revision)
            if not 0<=source<expiry<=9007199254740991 or not 1<=revision<=9007199254740991:
                raise ValueError()
            if source>now_ms+5000: raise ValueError()
            result.update(revision=revision,source_ms=source,expires_ms=expiry,
                          digest=digest.decode(),failure=failure.decode() if failure else None,
                          attempt_ms=int(attempt) if attempt else None)
            if now_ms>=expiry:
                return dict(result,reason="document_expired")
            return dict(result,status="available",reason=None,payload=payload)
        except (ValueError,TypeError,AttributeError,UnicodeError):
            return dict(status="unavailable",reason="document_integrity_failed",revision=0)
