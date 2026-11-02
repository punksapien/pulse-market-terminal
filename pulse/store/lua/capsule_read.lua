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
-- ADR-0062: bounded source-free read; descriptor and trusted expiry.
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

if #KEYS~=1 or #ARGV~=8 or string.sub(KEYS[1],1,10)~='pulse:stg:' then return {'scope_refused'} end
local step,slots,columns=tonumber(ARGV[2]),tonumber(ARGV[3]),tonumber(ARGV[4])
local ttl,inclusive=tonumber(ARGV[5]),tonumber(ARGV[7])
if not step or step<1 or step>4294967295 or step~=math.floor(step)
 or not slots or slots<1 or slots>1536 or slots~=math.floor(slots)
 or not columns or columns<1 or columns>3 or columns~=math.floor(columns)
 or not ttl or ttl<1 or ttl>691200000 or ttl~=math.floor(ttl)
 or (inclusive~=0 and inclusive~=1) or #ARGV[1]~=16 then return {'invalid_descriptor'} end
local ringbytes=64+math.floor((slots+7)/8)+slots*8*(columns+1)
local derived_size=24+72*columns
local size=ringbytes+32+derived_size+math.floor((slots+7)/8)
if redis.call('STRLEN',KEYS[1])~=size or redis.call('PTTL',KEYS[1])~=-1 then return {'missing_or_invalid_frame'} end
local header=redis.call('GETRANGE',KEYS[1],0,63)
if string.sub(header,1,8)~='PULSER02' or u32(header,9)%65536~=2 or string.byte(header,11)+256*string.byte(header,12)~=columns or u32(header,13)~=step or u32(header,17)~=slots or u32(header,61)~=crc(string.sub(header,1,60)) then return {'ring_integrity_failed'} end
local raw=redis.call('GETRANGE',KEYS[1],ringbytes,size-1)
if string.sub(raw,1,8)~='PULSEC11' or string.sub(raw,17,32)~=ARGV[1] then return {'mapping_invalid'} end
if string.sub(raw,9,9)~='R' then return {'quarantined'} end
if u32(raw,11)~=crc(string.sub(raw,33)) then return {'footer_crc_failed'} end
local derived=string.sub(raw,33,32+derived_size)
local version_pos=17+8*columns
local expiry_pos=9+8*columns
if string.byte(derived,version_pos+7)~=0 or string.byte(derived,version_pos+6)>31 or u53(derived,version_pos)==0 then return {'no_observation'} end
local clock=redis.call('TIME')
local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
local source=u53(derived,1)
local expiry=u53(derived,expiry_pos)
if string.byte(derived,8)~=0 or string.byte(derived,expiry_pos+7)~=0 or string.byte(derived,7)>31 or string.byte(derived,expiry_pos+6)>31 or expiry<=source or expiry>source+ttl+inclusive then return {'invalid_expiry'} end
if source>now+5000 then return {'future_observation'} end
if now>=expiry then return {'expired'} end

return {'available',derived}
