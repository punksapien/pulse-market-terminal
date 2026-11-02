"""ADR-0062 rolling source-call admission shared by every writer on an egress scope.

No default quota, namespace-per-run bootstrap, automatic retry, or exchange access.
Reconciliation requires explicit independently verified usage and evidence.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import re
import uuid

from .expansion_guard import guard
from .staging_policy import staging_key


@dataclass(frozen=True)
class RollingLimit:
    cap: int
    window_ms: int
    max_entries: int=8192

    def __post_init__(self):
        for value,maximum in ((self.cap,1000000000),(self.window_ms,3600000),(self.max_entries,8192)):
            if type(value) is not int or not 1<=value<=maximum:
                raise ValueError("invalid_shared_budget_limit")


RECONCILE="""for _,key in ipairs(KEYS) do if string.sub(key,1,10)~='pulse:stg:' then return {'scope_refused'} end end
if redis.call('EXISTS',KEYS[1])==1 then return {'cannot_reset_live_budget'} end
for i=2,#KEYS do if redis.call('EXISTS',KEYS[i])==1 then return {'orphaned_budget_ledger'} end end
local clock=redis.call('TIME')
local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
redis.call('HSET',KEYS[1],'mapping',ARGV[1],'state','Q','clock',now,'evidence',ARGV[2])
for i=2,#KEYS do
 local amount=tonumber(ARGV[i+1])
 if amount>0 then redis.call('ZADD',KEYS[i],now,tostring(amount)..':'..ARGV[2+#KEYS]) end
 redis.call('HSET',KEYS[1],'total:'..(i-1),amount,'count:'..(i-1),amount>0 and 1 or 0)
end
redis.call('HSET',KEYS[1],'state','R')
return {'reconciled',tostring(now)}"""


class SharedAdmission:
    def __init__(self,client,scope,limits,*,approved_plan_fallback=False):
        if (not isinstance(scope,str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}",scope)
                or type(limits) is not dict or not 1<=len(limits)<=8
                or any(not isinstance(k,str) or not re.fullmatch(r"[a-z_]{1,32}",k)
                       or not isinstance(v,RollingLimit) for k,v in limits.items())
                or type(approved_plan_fallback) is not bool):
            raise ValueError("invalid_shared_budget_identity")
        self.client,self.scope,self.limits=client,scope,dict(sorted(limits.items()))
        self.approved_plan_fallback=approved_plan_fallback
        # Egress identity, not a collector lease/run/pair. Caller must verify scope.
        self.prefix=staging_key("pulse:stg:{quota-"+scope+"}:source:")
        self.keys=[self.prefix+"meta"]+[self.prefix+name for name in self.limits]
        self._expected_keys=tuple(self.keys)
        self.mapping=hashlib.sha256(json.dumps({k:asdict(v) for k,v in self.limits.items()},
            sort_keys=True,separators=(",",":")).encode()).hexdigest()
        self.sha=None

    def preflight(self):
        if tuple(self.keys)!=self._expected_keys:
            raise ValueError("shared_budget_scope_refused")
        for key in self.keys:
            staging_key(key)
        _,self.last_memory=guard(self.client.info("memory"),additional_bytes=131072,
            approved_plan_fallback=self.approved_plan_fallback)

    def reconcile(self,usage,*,evidence_id):
        if (type(usage) is not dict or usage.keys()!=self.limits.keys()
                or not isinstance(evidence_id,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}",evidence_id)
                or any(type(usage[k]) is not int or not 0<=usage[k]<=v.cap for k,v in self.limits.items())):
            raise ValueError("invalid_shared_budget_evidence")
        self.preflight()
        result=self.client.eval(RECONCILE,len(self.keys),*self.keys,self.mapping,evidence_id,
            *(usage[k] for k in self.limits),uuid.uuid4().hex)
        self.preflight()
        return result

    def reserve(self,costs):
        if (type(costs) is not dict or costs.keys()!=self.limits.keys()
                or any(type(costs[k]) is not int or not 0<=costs[k]<=v.cap for k,v in self.limits.items())
                or not any(costs.values())):
            raise ValueError("invalid_shared_budget_cost")
        self.preflight()
        if self.sha is None:
            self.sha=self.client.script_load((Path(__file__).with_name("lua")/"shared_admission.lua").read_bytes())
            self.preflight()
        config=[]
        for name,limit in self.limits.items():
            config.extend((limit.cap,limit.window_ms,costs[name],limit.max_entries))
        result=self.client.evalsha(self.sha,len(self.keys),*self.keys,self.mapping,uuid.uuid4().hex,*config)
        self.preflight()
        return result
