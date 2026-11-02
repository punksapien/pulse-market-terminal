/* ADR-0198. Exact approved manifest only; run on the verified relay, never locally with credentials. */
'use strict';
const fs=require('node:fs'),crypto=require('node:crypto');
const OLD='28c01310000000000000000000000001',RUN='28c01440000000000000000000000001';
const PREFIX=`pulse:stg:{${OLD}}:`,PROD=`pulse:prod:{${RUN}}:`;
const MAPPING='sha256:6a02d7759bfcc2c27159385e0e03119a2ea3d8112723c0a6ceeca91cadca4fc8';
const MANIFEST_SHA='7afcd89b5573296fb3c857db6ec2c601d1eea2217750c8377b5b6c3b5252c1aa';
const REGISTRY_SHA='4b47ede85e09a9b8cd3a06f3df4d4076ed03411f9d91199a1134021978047e29';
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const canonical=x=>Array.isArray(x)?'['+x.map(canonical).join(',')+']':x&&typeof x==='object'?'{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+canonical(x[k])).join(',')+'}':JSON.stringify(x);
const GATES=['publication','publication:live','publication:snapshot','publication:history','stream','runtime:source:release350'].map(x=>PREFIX+x+':lease');
function validateManifest(bytes,rows){
  if(sha(bytes)!==MANIFEST_SHA)throw Error('manifest_digest_refused');
  const m=JSON.parse(bytes);
  if(m.schema!=='pulse.retired-frame-audit.v1'||m.run!==OLD||m.frames!==404||m.entries.length!==404||m.active_leases!==0)throw Error('manifest_refused');
  if(rows.length!==350||'sha256:'+sha(canonical(rows))!==MAPPING)throw Error('membership_refused');
  const selected=new Set(rows.map(r=>r.pair_id)),seen=new Set();let total=0,memory=0;
  for(const e of m.entries){
    if(!/^B-[A-Z0-9]{1,24}_USDT$/.test(e.pair_id)||!['oi','oi_cold','ls_global','ls_top'].includes(e.metric)
      ||e.key!==PREFIX+e.pair_id+':'+e.metric+':frame'||seen.has(e.key)||selected.has(e.pair_id)
      ||!Number.isInteger(e.raw_bytes)||e.raw_bytes<=0||e.raw_bytes>32768||!Number.isInteger(e.memory_usage)
      ||e.memory_usage<e.raw_bytes||e.lease_absent!==true||e.ttl_ms!==-1||!/^[a-f0-9]{40}$/.test(e.dump_sha1))throw Error('entry_refused');
    seen.add(e.key);total+=e.raw_bytes;memory+=e.memory_usage;
  }
  if(total!==5237456||memory!==6037584||m.raw_bytes!==total||m.memory_usage_sum!==memory)throw Error('totals_refused');
  return m;
}
// ADR-0198: all writer guards are atomic; bounded DUMP prevents a container walk.
const CHECK_UNLINK=`local begin=redis.call('TIME')
if #KEYS~=8 or #ARGV~=4 then return {'arity_refused'} end
local prefix='${PREFIX}'
if string.sub(KEYS[1],1,#prefix)~=prefix or string.sub(KEYS[1],-6)~=':frame' or KEYS[2]~=string.sub(KEYS[1],1,-6)..'lease' then return {'scope_refused'} end
${GATES.map((key,i)=>`if KEYS[${i+3}]~='${key}' then return {'gate_scope_refused'} end`).join('\n')}
if redis.call('GET',KEYS[3])~='release-handoff:${RUN}' or redis.call('PTTL',KEYS[3])<=86400000 then return {'retirement_refused'} end
for i=4,8 do
 if redis.call('GET',KEYS[i])~='retired:dcxpulse:ADR-0191' or redis.call('PTTL',KEYS[i])~=-1 then return {'retirement_refused'} end
end
if redis.call('EXISTS',KEYS[2])~=0 then return {'active_lease_refused'} end
if redis.call('TYPE',KEYS[1]).ok~='string' then return {'type_refused'} end
local n=tonumber(ARGV[2])
if not n or n<=0 or n>32768 or redis.call('STRLEN',KEYS[1])~=n or redis.call('PTTL',KEYS[1])~=-1 then return {'size_or_ttl_refused'} end
if KEYS[1]~=ARGV[3] or redis.sha1hex(redis.call('DUMP',KEYS[1]))~=ARGV[1] then return {'changed_refused'} end
local status='verified'
if ARGV[4]=='apply' then redis.call('UNLINK',KEYS[1]);status='deleted'
elseif ARGV[4]~='check' then return {'mode_refused'} end
local done=redis.call('TIME')
return {status,tostring((tonumber(done[1])-tonumber(begin[1]))*1000000+tonumber(done[2])-tonumber(begin[2]))}`;
function parseEnv(path){return Object.fromEntries(fs.readFileSync(path,'utf8').split('\n').filter(x=>/^[A-Z_]+=/.test(x)).map(x=>{const i=x.indexOf('=');let v=x.slice(i+1);if(v.startsWith('"'))v=JSON.parse(v);return[x.slice(0,i),v]}));}
function memory(info){return Object.fromEntries(info.split('\n').map(x=>x.trim().split(':')).filter(([k])=>['used_memory','used_memory_rss','used_memory_peak','used_memory_lua','mem_fragmentation_ratio','maxmemory_policy'].includes(k)).map(([k,v])=>[k,Number.isFinite(Number(v))?Number(v):v]));}
async function execute(c,manifest,apply,receipt){
  for(const mode of apply?['check','apply']:['check']){
    for(const e of manifest.entries){
      const started=process.hrtime.bigint();
      const result=await c.eval(CHECK_UNLINK,{keys:[e.key,e.key.replace(/frame$/,'lease'),...GATES],arguments:[e.dump_sha1,String(e.raw_bytes),e.key,mode]});
      receipt.max_roundtrip_us=Math.max(receipt.max_roundtrip_us||0,Number((process.hrtime.bigint()-started)/1000n));
      if(result[0]!== (mode==='apply'?'deleted':'verified'))throw Error('cleanup_'+result[0]);
      receipt.max_lua_us=Math.max(receipt.max_lua_us,Number(result[1]));
      if(mode==='apply')receipt.deleted.push({key:e.key,dump_sha1:e.dump_sha1,lua_us:Number(result[1])});
      else receipt.verified++;
    }
  }
}
async function main(){
  const [mode,path]=process.argv.slice(2);
  if(!['--check','--apply-approved-404'].includes(mode)||!path)throw Error('mode_refused');
  const apply=mode==='--apply-approved-404';
  const env={...parseEnv('/etc/pulse-stream/production.env'),...parseEnv('/etc/pulse-stream/production-driver.env')};
  if(env.PULSE_RUNTIME_RUN_ID!==RUN||env.PULSE_MAPPING_VERSION!==MAPPING||env.VERCEL_ENV!=='production')throw Error('connector_identity_refused');
  const plan=JSON.parse(fs.readFileSync('/var/lib/pulse-handover/plan.json','utf8'));
  if(plan.run!==RUN||plan.mapping!==MAPPING)throw Error('handover_identity_refused');
  const manifest=validateManifest(fs.readFileSync(path),plan.rows);
  const {createClient}=require('/opt/pulse-stream/node_modules/redis');
  const c=createClient({url:env.PULSE_REDIS_URL,socket:{connectTimeout:3000,reconnectStrategy:false},commandsQueueMaxLength:64});c.on('error',()=>{});
  const receipt={schema:'pulse.retired-frame-cleanup.v1',manifest_sha256:MANIFEST_SHA,apply,at_ms:Date.now(),verified:0,deleted:[],max_lua_us:0,exchange_calls:0,production_keys_modified:0};
  const save=()=>fs.writeFileSync('/var/lib/pulse-handover/retired404-'+receipt.at_ms+'.json',JSON.stringify(receipt),{mode:0o600,flag:'wx'});
  const timer=setTimeout(()=>{receipt.status='cleanup_timeout';save();console.error(JSON.stringify(receipt));c.destroy();process.exit(1)},300000);
  try{
    await c.connect();receipt.before=memory(await c.info('memory'));
    const key=PROD+'runtime:source:release350:data';
    if(await c.hStrLen(key,'payload')>131072)throw Error('membership_size_refused');
    const payload=await c.hGet(key,'payload');
    if(sha(payload)!==await c.hGet(key,'digest'))throw Error('production_membership_digest_refused');
    const release=JSON.parse(payload);
    if(release.mapping_version!==MAPPING||canonical(release.universe)!==canonical(plan.rows))throw Error('production_membership_refused');
    const oldKey=PREFIX+'runtime:source:registry:data';
    if(await c.hStrLen(oldKey,'payload')>131072)throw Error('registry_size_refused');
    const old=await c.hGet(oldKey,'payload');
    if(sha(old)!==REGISTRY_SHA)throw Error('registry_changed');
    const members=new Set(JSON.parse(old).universe.map(x=>x.pair_id));
    if(members.size!==505||manifest.entries.some(e=>!members.has(e.pair_id)))throw Error('registry_membership_refused');
    await execute(c,manifest,apply,receipt);
    receipt.after=memory(await c.info('memory'));receipt.status=apply?'cleanup_complete':'cleanup_checked';
  }catch(e){receipt.status='cleanup_stopped';receipt.reason=/^[a-z_]+$/.test(e.message)?e.message:e.name;process.exitCode=1;}
  finally{clearTimeout(timer);c.destroy();save();console.log(JSON.stringify(receipt));}
}
module.exports={validateManifest,CHECK_UNLINK,execute,canonical,MANIFEST_SHA,MAPPING,PREFIX,memory};
if(require.main===module)main().catch(()=>{console.error('cleanup_start_refused');process.exitCode=1});
