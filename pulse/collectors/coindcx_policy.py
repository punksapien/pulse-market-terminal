"""ADR-0142: Preview-only two-dimensional quota, full-minute quiet transition."""
OLD_COOLDOWN="pulse:stg:{quota-vercel-bom1-all-v1-dcx}:source:cooldown"
READY="pulse:stg:{quota-vercel-bom1-all-v1-dcx12}:policy_ready_ms"
TRANSITION="""for _,k in ipairs(KEYS) do if string.sub(k,1,10)~='pulse:stg:' then return {'scope_refused'} end end
local t=redis.call('TIME');local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
if redis.call('HGET',KEYS[4],'mapping')~=ARGV[1] or redis.call('HGET',KEYS[4],'state')~='R' then return {'policy_ledger_invalid'} end
local ready=redis.call('GET',KEYS[2])
if ready and (not string.match(ready,'^%d+$') or tonumber(ready)>now+60001) then return {'policy_gate_invalid'} end
local old=tonumber(redis.call('GET',KEYS[1]) or '0')
local current=tonumber(redis.call('GET',KEYS[3]) or '0')
if not old or not current then return {'policy_gate_invalid'} end
if not ready then
 ready=now+60001;redis.call('SET',KEYS[2],tostring(ready))
 if old>now and old>current then redis.call('SET',KEYS[3],tostring(old),'PX',old-now) end
end
if old<now+86400000 then redis.call('SET',KEYS[1],tostring(now+86400000),'PX',86400000) end
local ended=redis.call('TIME')
local elapsed=(tonumber(ended[1])-tonumber(t[1]))*1000000+tonumber(ended[2])-tonumber(t[2])
if now<tonumber(ready) then return {'waiting',tostring(ready),tostring(elapsed)} end
return {'ready',tostring(ready),tostring(elapsed)}"""


def ensure_ready(client,ledger,cooldown):
    # Seed both entire windows BEFORE beginning the quiet minute. Never reset state.
    result=ledger.reconcile({"minute":720,"requests":12},evidence_id="approved_preview12_initial_full_windows")
    if result[0] not in (b"reconciled",b"cannot_reset_live_budget"):
        raise RuntimeError("coindcx_policy_initialisation_refused")
    outcome=client.eval(TRANSITION,4,OLD_COOLDOWN,READY,cooldown,ledger.keys[0],ledger.mapping)
    if not outcome or outcome[0] not in (b"waiting",b"ready"):
        raise RuntimeError("coindcx_policy_transition_refused")
    ledger.transition_us=int(outcome[2])
    return outcome[0]==b"ready",int(outcome[1])
