"""ADR-0097 fixed-cohort staging state; bounded values, no keyspace iteration."""
import json
import re
from threading import Lock
from .staging_policy import staging_key

WRITE="""for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return 0 end end
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('PTTL',KEYS[1])<=0 then return 0 end
if #ARGV[2]>64 or #ARGV[3]>131072 or #ARGV[4]>1024 then return 0 end
redis.call('HSET',KEYS[2],ARGV[2],ARGV[3]);redis.call('PEXPIRE',KEYS[2],86400000)
if #ARGV[4]>0 then redis.call('HSET',KEYS[3],ARGV[2],ARGV[4]);redis.call('PEXPIRE',KEYS[3],86400000) end
return 1"""
READ="""if string.sub(KEYS[1],1,10)~='pulse:stg:' then return {'scope_refused'} end
if redis.call('HSTRLEN',KEYS[1],ARGV[1])>131072 then return {'size_refused'} end
local body=redis.call('HGET',KEYS[1],ARGV[1])
if not body then return {'missing'} end
return {'available',body}"""
READ_FUNDING="""if string.sub(KEYS[1],1,10)~='pulse:stg:' then return {'scope_refused'} end
if #ARGV<1 or #ARGV>16 then return {'size_refused'} end
for _,name in ipairs(ARGV) do
 if #name<1 or #name>64 or redis.call('HSTRLEN',KEYS[1],name)>1024 then return {'size_refused'} end
end
local result={'available'}
for _,name in ipairs(ARGV) do table.insert(result,redis.call('HGET',KEYS[1],name) or false) end
return result"""


def funding_projection(value):
    instrument=value.get("instrument")
    if type(instrument) is not dict or type(value.get("instrument_ms")) is not int:return None
    keys=("pair","quote_currency_short_name","settle_currency_short_name","funding_frequency")
    if any(k not in instrument for k in keys):return None
    if instrument["quote_currency_short_name"]!="USDT" or instrument["settle_currency_short_name"]!="USDT":return None
    return {"instrument":{k:instrument[k] for k in keys},"instrument_ms":value["instrument_ms"]}


class WorkerState:
    def __init__(self,publisher):
        self.publisher=publisher
        self.key=staging_key(publisher.prefix+"worker_state")
        self.funding_key=staging_key(publisher.prefix+"worker_funding")
        self.funding_sha=None
        self._io_lock=Lock()
        self._io={"record_read_bytes":0,"funding_read_bytes":0,"sent_payload_bytes":0,"write_calls":0}

    def _count(self,**values):
        with self._io_lock:
            for key,value in values.items():self._io[key]+=value

    def io_stats(self):
        """ADR-0146: payload-byte lower bounds, excluding Redis protocol/scripts."""
        with self._io_lock:return dict(self._io)

    def read(self,name):
        return self.read_many([name])[name]

    def read_many(self,names):
        """ADR-0108: bounded pipelines of atomic per-field reads, never HGETALL."""
        if (not isinstance(names,(list,tuple)) or len(names)>1024
                or any(not isinstance(n,str) or not re.fullmatch(r"[A-Z0-9:_-]{1,64}",n) for n in names)
                or len(set(names))!=len(names)):
            raise ValueError("worker_state_field")
        result={}
        for at in range(0,len(names),16):
            batch=names[at:at+16]
            pipe=self.publisher.client.pipeline(transaction=False)
            for name in batch: pipe.eval(READ,1,self.key,name)
            values=pipe.execute()
            if len(values)!=len(batch): raise ValueError("worker_state_read_shape")
            for name,value in zip(batch,values):
                if value==[b"missing"]: result[name]=None
                elif isinstance(value,list) and len(value)==2 and value[0]==b"available":
                    self._count(record_read_bytes=len(value[1]))
                    if len(value[1])>131072: raise ValueError("worker_state_size")
                    record=json.loads(value[1])
                    if type(record) is not dict: raise ValueError("worker_state_shape")
                    result[name]=record
                else: raise ValueError("worker_state_size_or_scope")
        return result

    def write(self,name,value):
        if not re.fullmatch(r"[A-Z0-9:_-]{1,64}",name): raise ValueError("worker_state_field")
        if type(value) is not dict:raise ValueError("worker_state_shape")
        payload=json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        if len(payload)>131072: raise ValueError("worker_state_size")
        projection=funding_projection(value)
        mini=b"" if projection is None else json.dumps([projection["instrument"]["pair"],
            projection["instrument"]["funding_frequency"],projection["instrument_ms"]],
            separators=(",",":"),allow_nan=False).encode()
        if len(mini)>1024:raise ValueError("worker_funding_size")
        self.publisher.preflight(3*(len(payload)+len(mini))+4096)
        self._count(sent_payload_bytes=len(payload)+len(mini),write_calls=1)
        if self.publisher.client.eval(WRITE,3,self.publisher.lease,self.key,self.funding_key,
                self.publisher.owner or b"",name,payload,mini)!=1: raise RuntimeError("worker_state_fenced_out")

    def read_funding_many(self,names):
        if (not isinstance(names,(list,tuple)) or len(names)>1024
                or any(not isinstance(n,str) or not re.fullmatch(r"[A-Z0-9:_-]{1,64}",n) for n in names)
                or len(set(names))!=len(names)):
            raise ValueError("worker_state_field")
        if not names:return {}
        if self.funding_sha is None:self.funding_sha=self.publisher.client.script_load(READ_FUNDING)
        result={}
        for at in range(0,len(names),16):
            batch=names[at:at+16]
            values=self.publisher.client.evalsha(self.funding_sha,1,self.funding_key,*batch)
            if not isinstance(values,list) or len(values)!=1+len(batch) or values[0]!=b"available":
                raise ValueError("worker_funding_read_shape")
            for name,body in zip(batch,values[1:]):
                if body is None:result[name]=None;continue
                self._count(funding_read_bytes=len(body))
                if type(body) is not bytes or len(body)>1024:raise ValueError("worker_funding_size")
                parts=json.loads(body)
                if (type(parts) is not list or len(parts)!=3 or not name.endswith("USDT")
                        or parts[0]!="B-"+name[:-4]+"_USDT" or type(parts[2]) is not int):
                    raise ValueError("worker_funding_shape")
                result[name]={"instrument":{"pair":parts[0],"funding_frequency":parts[1],
                    "quote_currency_short_name":"USDT","settle_currency_short_name":"USDT"},"instrument_ms":parts[2]}
        return result


def measured_io(state):
    method=getattr(state,"io_stats",None)
    result=method() if callable(method) else None
    return result if type(result) is dict and all(type(v) is int and v>=0 for v in result.values()) else {}
