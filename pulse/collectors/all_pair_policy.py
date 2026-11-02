"""ADR-0134 quota-policy transition: retire old scope, wait a full history window."""
OLD_CONTROL="pulse:stg:{quota-vercel-bom1-c1-binance}:source:control"
READY_KEY="pulse:stg:{quota-vercel-bom1-all-v1}:policy_ready_ms"
TRANSITION="""for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return {'scope_refused'} end end
local t=redis.call('TIME');local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
local ready=redis.call('GET',KEYS[2])
if ready and (not string.match(ready,'^%d+$') or tonumber(ready)>now+300000) then return {'policy_gate_invalid'} end
if not ready then ready=now+300000;redis.call('SET',KEYS[2],tostring(ready)) end
local previous=tonumber(redis.call('HGET',KEYS[1],'open_until') or '0')
if not previous then return {'policy_gate_invalid'} end
if previous<now+86400000 then redis.call('HSET',KEYS[1],'open_until',now+86400000) end
local ended=redis.call('TIME')
local elapsed=(tonumber(ended[1])-tonumber(t[1]))*1000000+tonumber(ended[2])-tonumber(t[2])
if now<tonumber(ready) then return {'waiting',tostring(ready),tostring(elapsed)} end
return {'ready',tostring(ready),tostring(elapsed)}"""


def ensure_ready(client):
    result=client.eval(TRANSITION,2,OLD_CONTROL,READY_KEY)
    if not result or result[0] not in (b"waiting",b"ready"):
        raise RuntimeError("quota_transition_refused")
    return result[0]==b"ready",int(result[1])
