-- ADR-0064: provider-minute floor is separate from local rolling charges.
-- KEYS meta,history,weight,control. ARGV mapping,nonce,status,weight,date_ms,retry_ms,draw.
if #KEYS~=4 or #ARGV~=7 then return {'invalid_arguments'} end
for _,key in ipairs(KEYS) do if string.sub(key,1,10)~='pulse:stg:' then return {'scope_refused'} end end
local meta,gate=KEYS[1],KEYS[4]
if redis.call('HGET',gate,'mapping')~=ARGV[1] then return {'source_control_unknown'} end
local status,weight,date,retry,draw=tonumber(ARGV[3]),tonumber(ARGV[4]),tonumber(ARGV[5]),tonumber(ARGV[6]),tonumber(ARGV[7])
if not status or status<0 or status>599 or status~=math.floor(status)
 or not weight or weight< -1 or weight>1000000000 or weight~=math.floor(weight)
 or not date or date< -1 or date>9007199254740991 or date~=math.floor(date)
 or not retry or retry<0 or retry>9007199254740991 or retry~=math.floor(retry)
 or not draw or draw<0 or draw>1 then return {'invalid_feedback'} end
local clock=redis.call('TIME')
local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
local previous=tonumber(redis.call('HGET',gate,'feedback_clock') or '0')
previous=math.max(previous,tonumber(redis.call('HGET',meta,'clock') or '0'))
if now<previous then return {'clock_regression'} end
local initialised=redis.call('HGET',gate,'initialised')=='1'
if initialised then
 if redis.call('HGET',gate,'state')~='R' or redis.call('HGET',meta,'mapping')~=ARGV[1]
  or redis.call('HGET',meta,'state')~='R' then return {'source_state_quarantined'} end
 -- A runtime error is not a Redis rollback: mark unreadable before any mutation.
 redis.call('HSET',gate,'state','Q')
end
local hard=status==403 or status==418 or status==429 or status==451
local failed=hard or status==0 or status>=500 or status>=300
if failed then
 local count=math.min(63,tonumber(redis.call('HGET',gate,'failures') or '0')+1)
 local exponential=math.min(60000,1000*2^math.min(count-1,6))
 local delay=hard and math.max(retry,retry==0 and 60000 or 1000)+math.floor(draw*1000)
  or math.max(1000,retry,math.floor(draw*exponential))
 local old=tonumber(redis.call('HGET',gate,'open_until') or '0')
 redis.call('HSET',gate,'open_until',math.max(old,math.min(9007199254740991,now+delay)),'failures',count,'last_status',status,'feedback_clock',now)
end
local fresh=weight>=1 and date>=0 and date<=now+5000 and now-date<=15000
if not initialised then
 if redis.call('HGET',gate,'state')~='B' or redis.call('HGET',gate,'owner')~=ARGV[2]
  or now-tonumber(redis.call('HGET',gate,'issued') or '0')>30000 then return {'bootstrap_owner_or_age_refused'} end
 if failed or not fresh then
  local old=tonumber(redis.call('HGET',gate,'open_until') or '0')
  redis.call('HSET',gate,'open_until',math.max(old,now+60000))
  return {'bootstrap_header_unverified'}
 end
 if redis.call('EXISTS',meta)+redis.call('EXISTS',KEYS[2])+redis.call('EXISTS',KEYS[3])~=0 then return {'bootstrap_ledger_exists'} end
 redis.call('HSET',gate,'state','Q')
 redis.call('HSET',meta,'mapping',ARGV[1],'state','Q','clock',now,'evidence','provider-header-bootstrap',
  'total:1',1,'count:1',1,'total:2',weight,'count:2',1)
 redis.call('ZADD',KEYS[2],now,'1:'..ARGV[2])
 redis.call('ZADD',KEYS[3],now,tostring(weight)..':'..ARGV[2])
 redis.call('HSET',meta,'state','R')
 redis.call('HSET',gate,'initialised','1')
end
if fresh then
 local minute=math.floor(date/60000)
 local old_minute=tonumber(redis.call('HGET',gate,'provider_minute') or '-1')
 local floor=tonumber(redis.call('HGET',gate,'provider_floor') or '0')
 if minute>=old_minute then
  floor=minute==old_minute and math.max(floor,weight) or weight
  redis.call('HSET',gate,'provider_minute',minute,'provider_floor',floor,'header_at',now)
 end
elseif not failed and status==200 and weight~=-1 then
 -- An explicitly malformed/zero observation does not authorise more source calls.
 local old=tonumber(redis.call('HGET',gate,'open_until') or '0')
 redis.call('HSET',gate,'open_until',math.max(old,now+60000))
end
redis.call('HSET',meta,'clock',now)
redis.call('HSET',gate,'state','R','feedback_clock',now,'last_status',status)
if redis.call('HGET',gate,'refresh_owner')==ARGV[2] then redis.call('HSET',gate,'refresh_until',0) end
if not failed then redis.call('HSET',gate,'failures',0) end
return {'feedback_recorded',tostring(now),fresh and 'header_verified' or 'no_fresh_header'}
