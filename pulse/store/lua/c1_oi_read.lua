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
-- ADR-0056: source-free fixed-size read with server-clock expiry.
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

if redis.call('STRLEN',KEYS[1])~=25152 then return {'missing_or_invalid_frame'} end
local header=redis.call('GETRANGE',KEYS[1],0,63)
if string.sub(header,1,8)~='PULSER02' or u32(header,61)~=crc(string.sub(header,1,60)) then return {'ring_integrity_failed'} end
local raw=redis.call('GETRANGE',KEYS[1],24832,25151)
if string.sub(raw,1,8)~='PULSEC11' or string.sub(raw,17,32)~=ARGV[1] then return {'mapping_invalid'} end
if string.sub(raw,9,9)~='R' then return {'quarantined'} end
if u32(raw,11)~=crc(string.sub(raw,33)) then return {'footer_crc_failed'} end
local derived=string.sub(raw,33,128)
if string.byte(derived,32)~=0 or string.byte(derived,31)>31 or u53(derived,25)==0 then return {'no_observation'} end
local clock=redis.call('TIME')
local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
local source=u53(derived,1)
local expiry=u53(derived,17)
if string.byte(derived,8)~=0 or string.byte(derived,24)~=0 or string.byte(derived,7)>31 or string.byte(derived,23)>31 or expiry~=source+900001 then return {'invalid_expiry'} end
if source>now+5000 then return {'future_observation'} end
if now>=expiry then return {'expired'} end
local length=string.byte(derived,33)
if length<1 or length>63 then return {'invalid_source'} end
return {'available',derived}
