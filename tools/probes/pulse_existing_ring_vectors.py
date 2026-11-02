"""ADR-0188 read-only Redis Lua control-flow vectors; no real keys accessed.

The caller executes script() with zero Redis keys. redis.call is replaced by a
strict fixture inside the Lua closure, so even the positive bootstrap vector
cannot create a key. This is not a real-frame mutation or slot-write benchmark.
"""
import json
from pulse.store.metric_capsule import INITIALISE


def vectors():
    base=dict(mode="existing_only",exists=1,owner="owner",lease_ms=30000,
              frame_ttl=-1,length=5400,mapping="0123456789abcdef",
              expected="already_initialised",sets=0)
    return [dict(base,**changes) for changes in (
        {},dict(exists=0,expected="existing_ring_required"),
        dict(frame_ttl=1000,expected="invalid_ring_lifecycle"),
        dict(length=5401,expected="mapping_invalid"),
        dict(mapping="fedcba9876543210",expected="mapping_invalid"),
        dict(owner="old",expected="fenced_out"),
        dict(lease_ms=0,expected="fenced_out"),
        dict(mode="unknown",expected="invalid_arguments"),
        dict(mode="allow_new",exists=0,expected="initialised",sets=1),
    )]


def script():
    return """local cases=cjson.decode(ARGV[1])
if #KEYS~=0 or #cases~=9 then error('fixture_bounds') end
local results={}
for _,case in ipairs(cases) do
 local sets=0
 local KEYS={'fixture:frame','fixture:lease'}
 local ARGV={'owner',case.mode=='allow_new' and string.rep('x',5400) or '',
             '5400','5232','0123456789abcdef',case.mode}
 local redis={call=function(command,key,a,b)
  if command=='GET' and key==KEYS[2] then return case.owner end
  if command=='PTTL' then return key==KEYS[2] and case.lease_ms or case.frame_ttl end
  if command=='EXISTS' and key==KEYS[1] then return case.exists end
  if command=='STRLEN' and key==KEYS[1] then return case.length end
  if command=='GETRANGE' and key==KEYS[1] and a==5248 and b==5263 then return case.mapping end
  if command=='SET' and key==KEYS[1] then sets=sets+1;return 'OK' end
  error('unexpected_fixture_command')
 end}
 local function initialise()
"""+INITIALISE+"""
 end
 local result=initialise()[1]
 if result~=case.expected or sets~=case.sets then error('fixture_mismatch') end
 results[#results+1]={result,sets}
end
return results"""


if __name__=="__main__":
    print(json.dumps(dict(script=script(),argument=json.dumps(vectors(),separators=(",",":")))))
