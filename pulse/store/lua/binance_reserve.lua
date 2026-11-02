-- ADR-0062: shared rolling quotas, trusted clock, all dimensions before charge.
-- KEYS metadata followed by one ledger per dimension. No automatic bootstrap.
-- ARGV mapping,permit_uuid followed by cap,window_ms,cost,max_entries per ledger.
local n=#KEYS-2
local function refuse(reason) return {reason} end
if n<1 or n>8 or #ARGV~=3+4*n then return refuse('invalid_dimensions') end
for _,key in ipairs(KEYS) do if string.sub(key,1,10)~='pulse:stg:' then return refuse('scope_refused') end end
-- ADR-0064: circuit and provider usage share the reservation's atomic step.
if n~=2 then return refuse('invalid_source_dimensions') end
local gate=KEYS[#KEYS]
local clock0=redis.call('TIME')
local now0=tonumber(clock0[1])*1000+math.floor(tonumber(clock0[2])/1000)
if not string.match(ARGV[2], '^[0-9a-f]+$') or #ARGV[2]~=32 then return refuse('invalid_permit') end
local meta=KEYS[1]
local exists=redis.call('EXISTS',gate)==1
if exists and redis.call('HGET',gate,'mapping')~=ARGV[1] then return refuse('source_policy_mismatch') end
local cooldown=tonumber(redis.call('HGET',gate,'open_until') or '0')
if cooldown>now0 then return refuse('circuit_open') end
local only_probe=tonumber(ARGV[5])==0 and tonumber(ARGV[9])==1
if redis.call('EXISTS',meta)==0 then
 if exists and redis.call('HGET',gate,'initialised')=='1' then return refuse('budget_ledger_lost') end
 if ARGV[#ARGV]~='approved_header_bootstrap' or not only_probe then return refuse('bootstrap_probe_required') end
 if tonumber(redis.call('HGET',gate,'next_bootstrap') or '0')>now0 then return refuse('bootstrap_already_charged') end
 if redis.call('EXISTS',KEYS[2])+redis.call('EXISTS',KEYS[3])~=0 then return refuse('orphaned_budget_ledger') end
 redis.call('HSET',gate,'mapping',ARGV[1],'state','B','owner',ARGV[2],
  'issued',now0,'next_bootstrap',now0+300000,'open_until',cooldown,'initialised','0')
 return {'reserved',tostring(now0),'bootstrap'}
end
if not exists or redis.call('HGET',gate,'initialised')~='1' then return refuse('source_control_unknown') end
if redis.call('HGET',gate,'state')~='R' then return refuse('header_reconciliation_pending') end
local header_at=tonumber(redis.call('HGET',gate,'header_at') or '-1')
local provider_minute=tonumber(redis.call('HGET',gate,'provider_minute') or '-1')
local provider_floor=tonumber(redis.call('HGET',gate,'provider_floor') or '-1')
local needs_refresh=header_at<0 or now0-header_at>=60000 or provider_minute~=math.floor(now0/60000)
if provider_floor<0 then return refuse('provider_usage_unknown') end
if needs_refresh then
 if not only_probe then return refuse('header_refresh_required') end
 if tonumber(redis.call('HGET',gate,'refresh_until') or '0')>now0 then return refuse('header_probe_busy') end
end
if redis.call('HGET',meta,'mapping')~=ARGV[1] then return refuse('budget_reconciliation_required') end
if redis.call('HGET',meta,'state')~='R' then return refuse('budget_quarantined') end
if not string.match(ARGV[2], '^[0-9a-f]+$') or #ARGV[2]~=32 then return refuse('invalid_permit') end
local clock=redis.call('TIME')
local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
local last=tonumber(redis.call('HGET',meta,'clock'))
if not last or now<last then return refuse('clock_regression') end
local pending={}
local total_cost=0
local needs_prune=false
local denied=nil
for i=1,n do
 local p=3+(i-1)*4
 local cap,window,cost,maximum=tonumber(ARGV[p]),tonumber(ARGV[p+1]),tonumber(ARGV[p+2]),tonumber(ARGV[p+3])
 if not cap or cap<1 or cap>1000000000 or cap~=math.floor(cap)
  or not window or window<1 or window>3600000 or window~=math.floor(window)
  or not cost or cost<0 or cost>cap or cost~=math.floor(cost)
  or not maximum or maximum<1 or maximum>8192 or maximum~=math.floor(maximum) then return refuse('invalid_cost_or_limit') end
 local amount=tonumber(redis.call('HGET',meta,'total:'..i))
 local count=tonumber(redis.call('HGET',meta,'count:'..i))
 if not amount or not count or amount<0 or count<0 or count~=redis.call('ZCARD',KEYS[i+1]) then return refuse('budget_ledger_lost') end
 local expired=redis.call('ZRANGEBYSCORE',KEYS[i+1],'-inf',now-window,'LIMIT',0,65)
 if #expired>64 then needs_prune=true; table.remove(expired,65) end
 local removed=0
 for _,member in ipairs(expired) do
  local charge=tonumber(string.match(member,'^(%d+):[0-9a-f]+$'))
  if not charge or charge<1 then return refuse('budget_ledger_corrupt') end
  removed=removed+charge
 end
 if removed>amount then return refuse('budget_ledger_corrupt') end
 if amount-removed+cost>cap then denied=denied or 'budget_exhausted:'..i end
 if i==2 and not needs_refresh and provider_floor+cost>cap then denied=denied or 'provider_budget_exhausted' end
 if cost>0 and count-#expired>=maximum then denied=denied or 'budget_ledger_full:'..i end
 -- Reusing a permit is not permission to dispatch a second HTTP request.
 if redis.call('ZSCORE',KEYS[i+1],tostring(cost)..':'..ARGV[2]) then return refuse('permit_reused') end
 pending[i]={amount=amount-removed,count=count-#expired,cost=cost,expired=expired}
 total_cost=total_cost+cost
end
if total_cost==0 then return refuse('empty_reservation') end
if denied and not needs_prune then return refuse(denied) end
-- Runtime errors do not roll Redis writes back. Leave Q to fail closed on error.
redis.call('HSET',meta,'state','Q')
for i,data in ipairs(pending) do
 for _,member in ipairs(data.expired) do redis.call('ZREM',KEYS[i+1],member) end
 if data.cost>0 and not needs_prune then
  redis.call('ZADD',KEYS[i+1],now,tostring(data.cost)..':'..ARGV[2])
  data.amount=data.amount+data.cost
  data.count=data.count+1
 end
 redis.call('HSET',meta,'total:'..i,data.amount,'count:'..i,data.count)
end
redis.call('HSET',meta,'clock',now,'state','R')
if needs_prune then return {'budget_pruned_retry',tostring(now)} end
if needs_refresh then redis.call('HSET',gate,'refresh_owner',ARGV[2],'refresh_until',now+30000)
else redis.call('HSET',gate,'provider_floor',provider_floor+tonumber(ARGV[9])) end
return {'reserved',tostring(now),'ordinary'}
