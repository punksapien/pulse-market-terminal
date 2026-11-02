/* ADR-0191. Operator on verified host only. No key discovery, deletion or exchange calls. */
'use strict';
const fs=require('node:fs'),crypto=require('node:crypto');
const RUN='28c01440000000000000000000000001',RAW='28c01310000000000000000000000001';
const MAPPING='sha256:6a02d7759bfcc2c27159385e0e03119a2ea3d8112723c0a6ceeca91cadca4fc8';
const PLAN='/var/lib/pulse-handover/plan.json',OWNER='retired:dcxpulse:ADR-0191';
const prefix=(run,scope='stg')=>`pulse:${scope}:{${run}}:`;
const gate=prefix(RUN)+'publication:lease';
const sha=b=>crypto.createHash('sha1').update(b).digest('hex');
function canonical(x){return Array.isArray(x)?'['+x.map(canonical).join(',')+']':x&&typeof x==='object'?'{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+canonical(x[k])).join(',')+'}':JSON.stringify(x);}
function membership(value){
  const rows=value.universe;
  if(value.policy!=='top350.quote_24h_usdt.v1'||value.mapping_version!==MAPPING||!Array.isArray(rows)||rows.length!==350
    ||'sha256:'+crypto.createHash('sha256').update(canonical(rows)).digest('hex')!==MAPPING)throw Error('membership_refused');
  for(const [i,r] of rows.entries())if(r.id!==i||!/^B-[A-Z0-9]{1,24}_USDT$/.test(r.pair_id)||r.symbol!==r.pair_id.slice(2,-5)+'USDT')throw Error('member_refused');
  return rows;
}
function entries(rows){
  membership({universe:rows,policy:'top350.quote_24h_usdt.v1',mapping_version:MAPPING});
  const result=[];
  const add=(old,kind,required=false)=>result.push({old,next:old.replace(prefix(RAW),prefix(RUN,'prod')).replace(/^pulse:stg:/,'pulse:prod:'),kind,required});
  for(const r of rows)for(const metric of ['oi','oi_cold','ls_global','ls_top'])for(const suffix of ['frame','fence'])
    add(prefix(RAW)+r.pair_id+':'+metric+':'+suffix,'string');
  for(const suffix of ['worker_state','worker_funding'])add(prefix(RUN)+'publication:'+suffix,'hash',true);
  add(prefix(RUN)+'runtime:source:release350:data','hash',true);
  for(const suffix of ['meta','history','weight','control'])add('pulse:stg:{quota-vercel-bom1-all-v1-binance}:source:'+suffix,
    ['meta','control'].includes(suffix)?'hash':'zset',true);
  for(const suffix of ['meta','minute','requests','cooldown'])add('pulse:stg:{quota-vercel-bom1-all-v1-dcx12}:source:'+suffix,
    suffix==='meta'?'hash':suffix==='cooldown'?'string':'zset',suffix!=='cooldown');
  for(const key of ['pulse:stg:{quota-vercel-bom1-c1-binance}:source:control',
    'pulse:stg:{quota-vercel-bom1-all-v1}:policy_ready_ms',
    'pulse:stg:{quota-vercel-bom1-all-v1-dcx}:source:cooldown',
    'pulse:stg:{quota-vercel-bom1-all-v1-dcx12}:policy_ready_ms'])add(key,key.endsWith('control')?'hash':'string',true);
  if(new Set(result.map(x=>x.next)).size!==result.length)throw Error('duplicate_destination');
  return result;
}
const MOVE=`local t=redis.call('TIME')
if #KEYS~=4 or string.sub(KEYS[1],1,10)~='pulse:stg:' or string.sub(KEYS[2],1,11)~='pulse:prod:' then return {'scope_refused'} end
if redis.call('GET',KEYS[3])~=ARGV[1] or redis.call('PTTL',KEYS[3])~=-1 then return {'fenced_out'} end
if redis.call('EXISTS',KEYS[4])==1 then return {'active_lease'} end
if redis.call('EXISTS',KEYS[2])~=0 then return {'destination_exists'} end
local typ=redis.call('TYPE',KEYS[1]).ok
if typ~=ARGV[2] then return {'source_type_changed'} end
if typ=='string' then
 if redis.call('STRLEN',KEYS[1])~=tonumber(ARGV[3]) or redis.sha1hex(redis.call('GET',KEYS[1]))~=ARGV[4] then return {'source_changed'} end
elseif typ=='hash' then
 if redis.call('HLEN',KEYS[1])~=tonumber(ARGV[3]) then return {'source_changed'} end
elseif typ=='zset' then
 if redis.call('ZCARD',KEYS[1])~=tonumber(ARGV[3]) then return {'source_changed'} end
else return {'source_type_refused'} end
redis.call('RENAME',KEYS[1],KEYS[2])
local e=redis.call('TIME');return {'moved',tostring((tonumber(e[1])-tonumber(t[1]))*1000000+tonumber(e[2])-tonumber(t[2]))}`;
async function witness(c,b,key,kind,rows){
  const type=await c.type(key);if(type==='none')return null;
  if(type!==kind)throw Error('source_type_refused');
  const h=crypto.createHash('sha1');let size=0;
  if(type==='string'){
    size=await c.strLen(key);if(size>32768)throw Error('string_size_refused');
    h.update(await b.getRange(key,0,32768));
  }else if(type==='hash'){
    size=await c.hLen(key);let fields;
    if(key.endsWith(':worker_state')||key.endsWith(':worker_funding')){
      fields=rows.map(r=>r.symbol).concat(key.endsWith(':worker_state')?['SNAPSHOT_CURSOR','HISTORY_CURSOR','SCHEDULE_PRESSURE']:[]);
    }else{if(size>64)throw Error('hash_size_refused');fields=await c.hKeys(key);}
    let found=0;
    for(const field of fields.sort()){
      if(await c.hStrLen(key,field)>131072)throw Error('field_size_refused');
      const v=await b.hGet(key,field);if(v===null)continue;
      found++;
      // ADR-0191: the sole declared mutation is this immutable configuration's deadline.
      if(field==='expiry'&&key.endsWith(':runtime:source:release350:data'))continue;
      h.update(JSON.stringify(field));h.update(String(v.length)+':');h.update(v);
    }
    if(found!==size)throw Error('unknown_hash_field');
  }else{
    size=await c.zCard(key);if(size>8192)throw Error('zset_size_refused');
    for(let i=0;i<size;i+=64)h.update(canonical(await c.zRangeWithScores(key,i,Math.min(size-1,i+63))));
  }
  return {kind:type,size,sha1:h.digest('hex'),ttl:await c.pTTL(key)};
}
async function retire(c){
  const keys=[];
  for(const run of [RUN,RAW]){
    for(const lane of ['','snapshot:','history:','live:'])keys.push(prefix(run)+'publication:'+lane+'lease');
    keys.push(prefix(run)+'stream:lease',prefix(run)+'runtime:source:release350:lease');
  }
  // ADR-0147 already retired the older raw run with a month-long lease. Preserve it.
  const previousRetirement=prefix(RAW)+'publication:lease';let preserved;
  // Refuse before any write if a current writer is still active.
  for(const key of keys){const value=await c.get(key);if(value!==null&&value!==OWNER){
    if(key===previousRetirement&&await c.pTTL(key)>86400000){preserved=value;continue;}
    throw Error('writer_not_drained');}}
  for(const key of keys){
    if(key===previousRetirement&&preserved){if(await c.get(key)!==preserved)throw Error('retirement_race');continue;}
    if(await c.get(key)===OWNER)continue;
    if(await c.set(key,OWNER,{NX:true})!=='OK')throw Error('retirement_race');}
  return keys;
}
async function spike(c){
  const run=crypto.randomUUID().replaceAll('-',''),base=prefix(run),prod=prefix(run,'prod'),g=base+'handover:lease';
  const testOwner=crypto.randomUUID(),raw=Buffer.alloc(25152,19),digest=sha(raw),manifest=[g];
  if(await c.set(g,testOwner,{NX:true})!=='OK')throw Error('spike_collision');
  let max_lua_us=0;
  try{
    for(let i=0;i<8;i++){
      const old=base+'case:'+i,next=prod+'case:'+i,lease=base+'case:'+i+':lease';manifest.push(old,next,lease);
      if(await c.set(old,raw,{NX:true,PX:120000})!=='OK')throw Error('spike_collision');
      const invoke=async(owner=testOwner,hash=digest)=>c.eval(MOVE,{keys:[old,next,g,lease],arguments:[owner,'string',String(raw.length),hash]});
      for(const [owner,hash,reason] of [['wrong',digest,'fenced_out'],[testOwner,'0'.repeat(40),'source_changed']]){
        if((await invoke(owner,hash))[0]!==reason)throw Error('spike_refusal_failed');
      }
      await c.set(lease,'busy',{NX:true,PX:1000});
      if((await invoke())[0]!=='active_lease')throw Error('spike_lease_failed');
      await c.pExpire(lease,1);await new Promise(r=>setTimeout(r,5));
      const moved=await invoke();if(moved[0]!=='moved')throw Error('spike_move_failed');
      max_lua_us=Math.max(max_lua_us,Number(moved[1]));
      if(await c.strLen(next)!==raw.length||await c.exists(old)||await c.pTTL(next)<=0)throw Error('spike_preservation_failed');
      await c.set(old,'late',{NX:true,PX:120000});
      if((await invoke())[0]!=='destination_exists')throw Error('spike_destination_failed');
    }
    console.log(JSON.stringify({status:'pass',cases:8,refusals:32,bytes_per_key:raw.length,max_lua_us,
      ephemeral_keys:manifest,expiry_ms:120000,deletions:0}));
  }finally{await c.pExpire(g,120000);}
}
async function main(){
  const mode=process.argv[2];if(!['--inspect','--prepare','--apply','--verify','--spike'].includes(mode))throw Error('mode_refused');
  if(mode!=='--inspect'&&process.env.PULSE_HANDOVER_APPROVED!=='1')throw Error('approval_required');
  const {createClient,RESP_TYPES}=require('redis');
  const env=Object.fromEntries(fs.readFileSync('/etc/pulse-stream/staging.env','utf8').split('\n').filter(x=>/^[A-Z_]+=/.test(x)).map(x=>{const i=x.indexOf('=');return[x.slice(0,i),x.slice(i+1)];}));
  if(env.VERCEL_ENV!=='preview'||env.PULSE_RUNTIME_RUN_ID!==RUN)throw Error('connector_identity_refused');
  const c=createClient({url:env.PULSE_REDIS_URL,socket:{connectTimeout:5000}});c.on('error',()=>{});await c.connect();
  const b=c.withTypeMapping({[RESP_TYPES.BLOB_STRING]:Buffer});
  try{
    if(mode==='--spike')return await spike(c);
    let plan;
    if(mode==='--inspect'||mode==='--prepare'){
      const key=prefix(RUN)+'runtime:source:release350:data';
      if(await c.hStrLen(key,'payload')>131072)throw Error('membership_size_refused');
      const payload=await c.hGet(key,'payload');
      if(crypto.createHash('sha256').update(payload).digest('hex')!==await c.hGet(key,'digest'))throw Error('membership_digest_refused');
      const rows=membership(JSON.parse(payload));
      if(mode==='--prepare')await retire(c);
      plan={schema:'pulse.handover.v1',run:RUN,mapping:MAPPING,rows,entries:[],created_ms:Date.now()};
      for(const e of entries(rows)){
        if(await c.exists(e.next))throw Error('destination_exists');
        if(e.old.endsWith(':frame')&&await c.exists(e.old.replace(/frame$/,'lease')))throw Error('active_capsule_lease');
        const w=await witness(c,b,e.old,e.kind,rows);
        if(!w&&e.required)throw Error('required_source_missing');
        if(w){if(e.old.endsWith(':frame')&&w.ttl!==-1)throw Error('frame_ttl_refused');plan.entries.push({...e,...w});}
      }
      if(mode==='--prepare'){
        fs.mkdirSync('/var/lib/pulse-handover',{recursive:true,mode:0o700});
        if(fs.existsSync(PLAN))throw Error('plan_already_exists');
        fs.writeFileSync(PLAN,JSON.stringify(plan),{mode:0o600,flag:'wx'});
      }
      console.log(JSON.stringify({mode,keys:plan.entries.length,frames:plan.entries.filter(x=>x.old.endsWith(':frame')).length,
        manifest_sha256:crypto.createHash('sha256').update(JSON.stringify(plan)).digest('hex')}));return;
    }
    plan=JSON.parse(fs.readFileSync(PLAN,'utf8'));membership({universe:plan.rows,policy:'top350.quote_24h_usdt.v1',mapping_version:plan.mapping});
    const allowed=new Map(entries(plan.rows).map(x=>[x.old,x.next]));
    if(plan.schema!=='pulse.handover.v1'||plan.run!==RUN||plan.entries.length>2820)throw Error('plan_refused');
    for(const e of plan.entries)if(allowed.get(e.old)!==e.next)throw Error('plan_scope_refused');
    let moved=0,verified=0,max_lua_us=0;
    for(const e of plan.entries){
      if(await c.get(gate)!==OWNER)throw Error('retirement_fence_lost');
      let old=await witness(c,b,e.old,e.kind,plan.rows);
      if(e.old.endsWith(':source:meta')&&await c.hGet(e.old,'retired')==='ADR-0191')old=null;
      if(old){
        if(mode==='--verify')throw Error('source_not_transferred');
        if(old.sha1!==e.sha1||old.size!==e.size||old.kind!==e.kind)throw Error('source_changed');
        const lease=e.old.endsWith(':frame')?e.old.replace(/frame$/,'lease'):prefix(RUN)+'handover:absent_lease';
        const result=await c.eval(MOVE,{keys:[e.old,e.next,gate,lease],arguments:[OWNER,e.kind,String(e.size),e.sha1]});
        if(result[0]!=='moved')throw Error('rename_refused');
        moved++;max_lua_us=Math.max(max_lua_us,Number(result[1]));
      }
      const actual=await witness(c,b,e.next,e.kind,plan.rows);
      if(!actual||actual.sha1!==e.sha1||actual.size!==e.size||actual.ttl===-2||e.ttl===-1&&actual.ttl!==-1)throw Error('destination_witness_failed');
      verified++;
      if(verified%350===0)console.log(JSON.stringify({mode,moved,verified,max_lua_us}));
    }
    if(mode==='--apply'){
      const key=prefix(RUN,'prod')+'runtime:source:release350:data';
      await c.hSet(key,'expiry',String(plan.created_ms+365*86400000));
      for(const scope of ['vercel-bom1-all-v1-binance','vercel-bom1-all-v1-dcx12'])
        await c.hSet('pulse:stg:{quota-'+scope+'}:source:meta',{state:'RETIRED',retired:'ADR-0191'});
      fs.writeFileSync('/var/lib/pulse-handover/receipt.json',JSON.stringify({moved,verified,max_lua_us,completed_ms:Date.now()}),{mode:0o600});
    }
    console.log(JSON.stringify({mode,moved,verified,max_lua_us,status:'complete'}));
  }finally{await c.quit();}
}
if(require.main===module)main().catch(e=>{console.error(JSON.stringify({status:'refused',reason:/^[a-z_]+$/.test(e.message)?e.message:'operation_failed'}));process.exitCode=1;});
module.exports={entries,membership,MOVE,canonical,retire,RUN,RAW,MAPPING};
