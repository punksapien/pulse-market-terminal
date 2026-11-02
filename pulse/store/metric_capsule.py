"""ADR-0062 descriptor-pinned staging adapter. No exchange calls or deletion API."""
import struct
import uuid
from pathlib import Path
from threading import Lock
from weakref import WeakKeyDictionary

from .capsule_model import CapsuleSpec, observation, source_bytes, empty_capsule, decode_derived
from .expansion_guard import guard
from .staging_policy import staging_key
from pulse.observation import MAX_SOURCE_TIME_MS

# ADR-0150: share only within the exact client/scope lifetime, not across deployments.
_CLIENT_SCRIPTS=WeakKeyDictionary()
_SCRIPT_LOCK=Lock()

ACQUIRE="""if redis.call('EXISTS',KEYS[1])==1 then return false end
local epoch=redis.call('INCR',KEYS[2])
local owner=tostring(epoch)..':'..ARGV[1]
redis.call('SET',KEYS[1],owner,'PX',30000)
return owner"""
RENEW="""if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('PTTL',KEYS[1])<=0 then return 0 end
redis.call('PEXPIRE',KEYS[1],30000)
return 1"""
INITIALISE="""if #ARGV~=6 or (ARGV[6]~='existing_only' and ARGV[6]~='allow_new') then return {'invalid_arguments'} end
if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return {'fenced_out'} end
if redis.call('EXISTS',KEYS[1])==1 then
 if redis.call('PTTL',KEYS[1])~=-1 then return {'invalid_ring_lifecycle'} end
 if redis.call('STRLEN',KEYS[1])~=tonumber(ARGV[3]) or redis.call('GETRANGE',KEYS[1],tonumber(ARGV[4])+16,tonumber(ARGV[4])+31)~=ARGV[5] then return {'mapping_invalid'} end
 return {'already_initialised'}
end
if ARGV[6]=='existing_only' then return {'existing_ring_required'} end
if #ARGV[2]~=tonumber(ARGV[3]) then return {'invalid_preallocation'} end
redis.call('SET',KEYS[1],ARGV[2])
return {'initialised'}"""


class MetricCapsule:
    def __init__(self,client,run_id,spec,*,approved_plan_fallback=False):
        if (not isinstance(run_id,str) or len(run_id)!=32
                or any(c not in "0123456789abcdef" for c in run_id)
                or not isinstance(spec,CapsuleSpec) or type(approved_plan_fallback) is not bool):
            raise ValueError("invalid_capsule_configuration")
        self.client,self.spec=client,spec
        self.prefix=staging_key("pulse:stg:{"+run_id+"}:"+spec.pair_id+":"+spec.metric+":")
        self.frame,self.lease,self.fence=(staging_key(self.prefix+name) for name in ("frame","lease","fence"))
        self._expected_keys=(self.frame,self.lease,self.fence)
        self.approved_plan_fallback=approved_plan_fallback
        self.owner=None
        # Lazy script loading: construction never touches the network.
        self._scripts={}

    def preflight(self,additional_bytes=0):
        self._scope()
        capacity,self.last_memory=guard(self.client.info("memory"),additional_bytes=additional_bytes,
            approved_plan_fallback=self.approved_plan_fallback)
        return capacity

    def _scope(self):
        current=(self.frame,self.lease,self.fence)
        if current!=self._expected_keys:
            raise ValueError("capsule_scope_refused")
        for key in current:
            staging_key(key)

    def _script(self,name):
        if name not in self._scripts:
            with _SCRIPT_LOCK:
                scripts=_CLIENT_SCRIPTS.setdefault(self.client,{})
                if name not in scripts:
                    self.preflight()
                    scripts[name]=self.client.script_load((Path(__file__).with_name("lua")/name).read_bytes())
                    self.preflight()
                self._scripts[name]=scripts[name]
        return self._scripts[name]

    def acquire(self):
        self.preflight()
        self.owner=self.client.eval(ACQUIRE,2,self.lease,self.fence,uuid.uuid4().hex)
        if not self.owner:
            raise RuntimeError("capsule_lease_busy")
        self.preflight()
        return self.owner

    def release(self):
        """Shorten only this owner's own ephemeral lease so the next pass is not refused; never touches data keys."""
        if getattr(self,"owner",None) is not None:
            self.client.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('PEXPIRE',KEYS[1],1) end return 0",
                1,self.lease,self.owner)
            self.owner=None

    def renew(self):
        if self.owner is None:
            raise RuntimeError("capsule_no_lease")
        self.preflight()
        if self.client.eval(RENEW,1,self.lease,self.owner)!=1:
            raise RuntimeError("capsule_fenced_out")

    def initialise(self,*,require_existing=False):
        if type(require_existing) is not bool:
            raise ValueError("invalid_existing_ring_policy")
        if self.owner is None:
            raise RuntimeError("capsule_no_lease")
        # ADR-0188: check existence atomically; never preallocate a repair-only frame.
        self.preflight(0 if require_existing else self.spec.nbytes)
        result=self.client.eval(INITIALISE,2,self.frame,self.lease,self.owner,
            b"" if require_existing else empty_capsule(self.spec),
            self.spec.nbytes,self.spec.ring_bytes,self.spec.mapping,
            "existing_only" if require_existing else "allow_new")
        self.preflight()
        accepted=([b"already_initialised"],) if require_existing else ([b"initialised"],[b"already_initialised"])
        if result not in accepted:
            raise RuntimeError("capsule_initialisation_refused:"+result[0].decode("ascii"))
        return result

    def write(self,point):
        if self.owner is None:
            raise RuntimeError("capsule_no_lease")
        checked=observation(self.spec,point.source_ms,point.sources,expires_ms=point.expires_ms)
        if checked!=point:
            raise ValueError("source_value_mismatch")
        self.preflight()
        result=self.client.evalsha(self._script("capsule_write.lua"),2,self.frame,self.lease,self.owner,
            point.source_ms,struct.pack("<"+"d"*self.spec.columns,*point.values),source_bytes(point),
            self.spec.mapping,*self.spec.wire_config,point.expires_ms)
        self.preflight()
        return result

    def write_many(self,points):
        """ADR-0064: bounded pipeline, atomic per capsule update, never whole-ring IO."""
        if self.owner is None:
            raise RuntimeError("capsule_no_lease")
        if not isinstance(points,(list,tuple)) or not 1<=len(points)<=16:
            raise ValueError("invalid_capsule_batch")
        for point in points:
            if observation(self.spec,point.source_ms,point.sources,expires_ms=point.expires_ms)!=point:
                raise ValueError("source_value_mismatch")
        self.preflight()
        sha=self._script("capsule_write.lua")
        pipe=self.client.pipeline(transaction=False)
        pipe.eval(RENEW,1,self.lease,self.owner)
        for point in points:
            pipe.evalsha(sha,2,self.frame,self.lease,self.owner,point.source_ms,
                struct.pack("<"+"d"*self.spec.columns,*point.values),source_bytes(point),
                self.spec.mapping,*self.spec.wire_config,point.expires_ms)
        results=pipe.execute()
        self.preflight()
        if results[0]!=1:
            raise RuntimeError("capsule_batch_fenced_out")
        for result in results[1:]:
            if result[0] not in (b"written",b"duplicate",b"dropped_older"):
                raise RuntimeError("capsule_batch_refused:"+result[0].decode("ascii"))
        return results[1:]

    def read(self):
        self._scope()
        result=self.client.evalsha(self._script("capsule_read.lua"),1,self.frame,self.spec.mapping,*self.spec.wire_config)
        if result[0]!=b"available":
            return {"status":"unavailable","reason":result[0].decode("ascii")}
        try:
            point,version=decode_derived(self.spec,result[1])
        except (ValueError,UnicodeError):
            return {"status":"unavailable","reason":"derived_integrity_failed"}
        return {"status":"available","pair_id":self.spec.pair_id,"symbol":self.spec.symbol,
            "metric":self.spec.metric,"unit":self.spec.unit,"source_ms":point.source_ms,
            "values":point.values,"sources":point.sources,"expires_ms":point.expires_ms,"version":version}

    def baseline(self,target_ms,tolerance_ms):
        self._scope()
        if (type(target_ms) is not int or not 0<=target_ms<=MAX_SOURCE_TIME_MS
                or type(tolerance_ms) is not int or not 0<=tolerance_ms<=self.spec.step_ms):
            raise ValueError("invalid_lookup")
        result=self.client.evalsha(self._script("capsule_baseline.lua"),1,self.frame,self.spec.mapping,
            self.spec.step_ms,self.spec.capacity,self.spec.columns,target_ms,tolerance_ms)
        if result[0]!=b"available":
            return {"status":"unavailable","reason":result[0].decode("ascii")}
        timestamp,*values=struct.unpack("<Q"+"d"*self.spec.columns,result[1])
        return {"status":"available","pair_id":self.spec.pair_id,"source_ms":timestamp,
                "values":tuple(values),"version":int(result[2])}
