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

-- ADR-0062: at most two physical slots, pinned mapping and footer version.
if #KEYS~=1 or #ARGV~=6 or string.sub(KEYS[1],1,10)~='pulse:stg:' then return {'scope_refused'} end
local step,slots,columns=tonumber(ARGV[2]),tonumber(ARGV[3]),tonumber(ARGV[4])
local target,tolerance=tonumber(ARGV[5]),tonumber(ARGV[6])
if not step or step<1 or step>4294967295 or step~=math.floor(step)
 or not slots or slots<1 or slots>1536 or slots~=math.floor(slots)
 or not columns or columns<1 or columns>3 or columns~=math.floor(columns)
 or not target or target<0 or target>9007199254740991 or target~=math.floor(target)
 or not tolerance or tolerance<0 or tolerance>step or tolerance~=math.floor(tolerance)
 or #ARGV[1]~=16 then return {'invalid_lookup'} end
local mask=math.floor((slots+7)/8)
local stride=8*(columns+1)
local ringbytes=64+mask+slots*stride
local derived_size=24+72*columns
local total=ringbytes+32+derived_size+mask
if redis.call('STRLEN',KEYS[1])~=total or redis.call('PTTL',KEYS[1])~=-1 then return {'missing_or_invalid_frame'} end
local header=redis.call('GETRANGE',KEYS[1],0,63)
if string.sub(header,1,8)~='PULSER02' or u32(header,9)%65536~=2
 or string.byte(header,11)+256*string.byte(header,12)~=columns or u32(header,13)~=step or u32(header,17)~=slots
 or u32(header,61)~=crc(string.sub(header,1,60)) then return {'ring_integrity_failed'} end
local footer=redis.call('GETRANGE',KEYS[1],ringbytes,total-1)
if string.sub(footer,1,8)~='PULSEC11' or string.sub(footer,17,32)~=ARGV[1] then return {'mapping_invalid'} end
if string.sub(footer,9,9)~='R' then return {'quarantined'} end
if u32(footer,11)~=crc(string.sub(footer,33)) then return {'footer_crc_failed'} end
if string.sub(header,21,28)==string.rep(string.char(255),8) then return {'baseline_missing'} end
if string.byte(header,28)~=0 or string.byte(header,27)>31 then return {'ring_integrity_failed'} end
local latest=u53(header,21)
local bucket=math.floor(target/step)
local derived=string.sub(footer,33,32+derived_size)
local vp=17+8*columns
if string.byte(derived,vp+7)~=0 or string.byte(derived,vp+6)>31 or u53(derived,vp)==0 then return {'invalid_derived'} end
for candidate=bucket,bucket-1,-1 do
 if candidate>=0 and candidate>latest-slots and candidate<=latest then
  local slot=candidate%slots
  local maskbyte=redis.call('GETRANGE',KEYS[1],64+math.floor(slot/8),64+math.floor(slot/8))
  if bit.band(string.byte(maskbyte,1),2^(slot%8))~=0 then
   local offset=64+mask+slot*stride
   local raw=redis.call('GETRANGE',KEYS[1],offset,offset+stride-1)
   if string.byte(raw,7)>31 or bit.band(string.byte(raw,8),63)~=0 then return {'slot_integrity_failed'} end
   local stamp=u53(raw,1)
   if math.floor(stamp/step)%slots~=slot then return {'slot_integrity_failed'} end
   for i=1,columns do
    local b7,b8=string.byte(raw,8*i+7,8*i+8)
    if bit.band(b8,127)==127 and b7>=240 then return {'slot_integrity_failed'} end
   end
   if math.floor(stamp/step)==candidate then
    local flags=string.byte(raw,8)
    if bit.band(flags,128)~=0 then return {'exact_timestamp_conflict'} end
    if stamp>target then
     if bit.band(flags,64)~=0 then return {'discarded_baseline_candidate'} end
    elseif target-stamp<=tolerance then
     return {'available',pack(stamp,8)..string.sub(raw,9),tostring(u53(derived,vp))}
    end
   end
  end
 end
end
return {'baseline_missing'}
