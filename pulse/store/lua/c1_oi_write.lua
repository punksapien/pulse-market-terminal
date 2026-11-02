-- ADR-0056: fixed BTC OI capsule; bounded ranges and trusted Redis TIME.
-- KEYS frame,lease. ARGV owner,source_ms,binary64,source_string,mapping16.
local function fail(reason) return {reason} end
if redis.call('GET',KEYS[2]) ~= ARGV[1] or redis.call('PTTL',KEYS[2]) <= 0 then return fail('fenced_out') end
local length=redis.call('STRLEN',KEYS[1])
local raw = redis.call('GETRANGE',KEYS[1],0,63)
if not raw or #raw < 64 then return fail('invalid_length') end
local function u32(s,p)
  local a,b,c,d=string.byte(s,p,p+3)
  return a+b*256+c*65536+d*16777216
end
local function pack(n,count)
  local s=''
  for i=1,count do local b=n%256; s=s..string.char(b); n=math.floor(n/256) end
  return s
end
local function u53(s,p)
  local value=0
  for i=6,0,-1 do value=value*256+string.byte(s,p+i) end
  return value
end
local function crc(s)
  local c=0xffffffff
  for i=1,#s do
    c=bit.bxor(c,string.byte(s,i))
    for j=1,8 do
      if bit.band(c,1)==1 then c=bit.bxor(bit.rshift(c,1),0xedb88320) else c=bit.rshift(c,1) end
    end
  end
  c=bit.bnot(c)
  if c<0 then c=c+4294967296 end
  return c
end
if string.sub(raw,1,8)~='PULSER02' or u32(raw,9)%65536~=2 then return fail('invalid_header') end
if u32(raw,61)~=crc(string.sub(raw,1,60)) then return fail('header_crc_failed') end
local columns=string.byte(raw,11)+256*string.byte(raw,12)
local step,capacity=u32(raw,13),u32(raw,17)
if step~=60000 or capacity~=1536 or columns~=1 then return fail('invalid_header') end
if string.sub(raw,37,60)~=string.rep(string.char(0),24) then return fail('invalid_header') end
local mask=math.floor((capacity+7)/8)
local stride=8*(columns+1)
local ringbytes=64+mask+capacity*stride
if length~=ringbytes+320 then return fail('invalid_length') end
local timestamp=tonumber(ARGV[2])
local clock=redis.call('TIME')
local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
if not timestamp or timestamp<0 or timestamp>9007199254740991 or timestamp~=math.floor(timestamp) then return fail('invalid_timestamp') end
if not now or now<0 or now>9007199254740991 or now~=math.floor(now) then return fail('invalid_observation_clock') end
if timestamp>now+5000 then return fail('future_timestamp') end
if now-timestamp>900000 then return fail('stale_timestamp') end
local values=ARGV[3]
if #values~=columns*8 then return fail('finite_binary64_values_required') end
for i=1,columns do
  local p=(i-1)*8
  local b7,b8=string.byte(values,p+7,p+8)
  if bit.band(b8,127)==127 and b7>=240 then return fail('finite_binary64_values_required') end
end
local source=ARGV[4]
if #source<1 or #source>63 or not string.match(source,'^[0-9eE.+-]+$') or not tonumber(source) then return fail('invalid_source_quantity') end
local b7,b8=string.byte(values,7,8)
local exponent=(b8%128)*16+math.floor(b7/16)
if b8>=128 or exponent==0 and string.sub(values,1,7)~=string.rep(string.char(0),7) then return fail('invalid_quantity') end
local fraction=0
for i=6,1,-1 do fraction=fraction*256+string.byte(values,i) end
fraction=fraction+(b7%16)*281474976710656
local quantity=exponent==0 and 0 or (1+fraction/4503599627370496)*2^(exponent-1023)
if tonumber(source)~=quantity then return fail('source_value_mismatch') end
local footer=redis.call('GETRANGE',KEYS[1],ringbytes,ringbytes+319)
if #footer~=320 or string.sub(footer,1,8)~='PULSEC11' or string.sub(footer,17,32)~=ARGV[5] then return fail('mapping_or_footer_invalid') end
local state=string.sub(footer,9,9)
if state~='R' and state~='Q' then return fail('partial_write_quarantined') end
if u32(footer,11)~=crc(string.sub(footer,33)) then return fail('footer_crc_failed') end
local derived=string.sub(footer,33,128)
local coverage=string.sub(footer,129,320)
local current_time=u53(derived,1)
local version=u53(derived,25)
if string.byte(derived,8)~=0 or string.byte(derived,32)~=0 or string.byte(derived,7)>31 or string.byte(derived,31)>31 then return fail('invalid_derived') end
if version>=9007199254740991 or timestamp+900001>9007199254740991 then return fail('derived_limit') end
local latest
if string.sub(raw,21,28)==string.rep(string.char(255),8) then latest=-1
else
  if string.byte(raw,28)~=0 or string.byte(raw,27)>31 then return fail('invalid_header') end
  latest=u53(raw,21)
end
if latest>math.floor(9007199254740991/step) then return fail('invalid_header') end
local bucket=math.floor(timestamp/step)
if bucket<=latest-capacity then return fail('outside_retention') end
local slot=bucket%capacity
local maskpos=65+math.floor(slot/8)
local bitmask=2^(slot%8)
local maskbyte=redis.call('GETRANGE',KEYS[1],maskpos-1,maskpos-1)
local present=bit.band(string.byte(maskbyte,1),bitmask)~=0
local offset=65+mask+slot*stride
local slotraw=redis.call('GETRANGE',KEYS[1],offset-1,offset+stride-2)
local incoming_time=timestamp
local flags=0
local collision=false
local status='written'
if present then
  local b7,b8=string.byte(slotraw,7,8)
  if b7>31 or bit.band(b8,63)~=0 then return fail('unknown_timestamp_flags') end
  local previous=u53(slotraw,1)
  local oldbucket=math.floor(previous/step)
  if oldbucket%capacity~=slot then return fail('invalid_slot') end
  for i=1,columns do
    local p=9+(i-1)*8
    local b7,b8=string.byte(slotraw,p+6,p+7)
    if bit.band(b8,127)==127 and b7>=240 then return fail('invalid_slot') end
  end
  flags=b8
  if oldbucket>bucket then return fail('newer_slot_present') end
  if oldbucket==bucket then
    local oldvalues=string.sub(slotraw,9,stride)
    if previous==timestamp then
      if bit.band(flags,128)~=0 or oldvalues~=values then
        flags=bit.bor(flags,128)
        if oldvalues<values then values=oldvalues end
        status='exact_timestamp_conflict'
      else return fail('duplicate') end
    else
      collision=bit.band(flags,64)==0
      flags=bit.bor(flags,64)
      if timestamp<previous then
        timestamp=previous; values=oldvalues; status='dropped_older'
      else flags=bit.band(flags,127) end
    end
  else flags=0 end
end
local collisions=string.sub(raw,29,36)
if collision and collisions~=string.rep(string.char(255),8) then
  local bytes={string.byte(collisions,1,8)}
  for i=1,8 do bytes[i]=(bytes[i]+1)%256; if bytes[i]~=0 then break end end
  collisions=string.char(unpack(bytes))
end
local header=string.sub(raw,1,20)..pack(math.max(latest,bucket),8)..collisions..string.rep(string.char(0),24)
header=header..pack(crc(header),4)
local newest=math.max(latest,bucket)
local advance=newest-latest
local shifted={}
for i=1,192 do
  local value=0
  if advance<capacity then
    local source_index=i-math.floor(advance/8)
    local shift=advance%8
    if source_index>=1 then value=string.byte(coverage,source_index)*2^shift end
    if source_index>1 then value=value+math.floor(string.byte(coverage,source_index-1)/2^(8-shift)) end
  end
  shifted[i]=value%256
end
local distance=newest-bucket
local cpos=math.floor(distance/8)+1
local cbit=2^(distance%8)
if status=='written' then shifted[cpos]=bit.bor(shifted[cpos],cbit)
elseif status=='exact_timestamp_conflict' then shifted[cpos]=bit.band(shifted[cpos],255-cbit) end
coverage=string.char(unpack(shifted))
if status=='written' and (version==0 or incoming_time>current_time) then
  derived=pack(incoming_time,8)..ARGV[3]..pack(incoming_time+900001,8)..pack(version+1,8)..string.char(#source)..source..string.rep(string.char(0),63-#source)
  state='R'
elseif status=='exact_timestamp_conflict' and incoming_time==current_time then state='Q' end
local body=derived..coverage
local newfooter='PULSEC11'..state..string.char(1)..pack(crc(body),4)..string.char(0,0)..ARGV[5]..body
-- Normal visibility is atomic within this script. A runtime error is not rollback:
-- W remains unreadable until explicit repair if any following write fails.
redis.call('SETRANGE',KEYS[1],ringbytes+8,'W')
redis.call('SETRANGE',KEYS[1],offset-1,pack(timestamp,7)..string.char(flags)..values)
redis.call('SETRANGE',KEYS[1],maskpos-1,string.char(bit.bor(string.byte(maskbyte,1),bitmask)))
redis.call('SETRANGE',KEYS[1],0,header)
redis.call('SETRANGE',KEYS[1],ringbytes,newfooter)
return {status,tostring(now)}
