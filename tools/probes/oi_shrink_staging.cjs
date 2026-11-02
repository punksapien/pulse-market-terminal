/* ADR-0174: exact frozen350 own-OI replacements, no key deletion or discovery. */
"use strict";
const fs=require("node:fs"),crypto=require("node:crypto"),{spawnSync}=require("node:child_process");
const{createClient,RESP_TYPES}=require("redis");
const RUN="28c01440000000000000000000000001",CAPS="28c01310000000000000000000000001";
const MAPPING="sha256:6a02d7759bfcc2c27159385e0e03119a2ea3d8112723c0a6ceeca91cadca4fc8";
const CAS=`local t=redis.call('TIME')
if #KEYS~=2 or #ARGV~=4 or string.sub(KEYS[1],1,10)~='pulse:stg:' then return {'scope_refused'} end
if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return {'fenced_out'} end
if redis.call('PTTL',KEYS[1])~=-1 or redis.call('STRLEN',KEYS[1])~=25152 or #ARGV[3]~=5068 then return {'lifecycle_or_length_refused'} end
local old=redis.call('GETRANGE',KEYS[1],0,25151)
if redis.sha1hex(old)~=ARGV[2] then return {'changed_frame_refused'} end
if redis.sha1hex(ARGV[3])~=ARGV[4] then return {'new_frame_refused'} end
redis.call('SET',KEYS[1],ARGV[3])
local e=redis.call('TIME');return {'shrunk',tostring((tonumber(e[1])-tonumber(t[1]))*1000000+tonumber(e[2])-tonumber(t[2]))}`;
const ACQUIRE=`if redis.call('EXISTS',KEYS[1])==1 then return false end
local n=redis.call('INCR',KEYS[2]);local owner=tostring(n)..':'..ARGV[1]
redis.call('SET',KEYS[1],owner,'PX',30000);return owner`;
const RELEASE=`if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('PEXPIRE',KEYS[1],1) end return 0`;
function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
const sha=b=>crypto.createHash('sha1').update(b).digest('hex');
async function main(){
  if(process.env.PULSE_SHRINK_APPROVED!=="1"||!['--check','--apply'].includes(process.argv[2]))throw Error('approval_required');
  const config=Object.fromEntries(fs.readFileSync('/etc/pulse-stream/staging.env','utf8').split('\n')
    .filter(x=>/^[A-Z_]+=/.test(x)).map(x=>{const i=x.indexOf('=');return[x.slice(0,i),x.slice(i+1)];}));
  if(config.VERCEL_ENV!=='preview')throw Error('preview_required');
  const client=createClient({url:config.PULSE_REDIS_URL});client.on('error',()=>{});await client.connect();
  const binary=client.withTypeMapping({[RESP_TYPES.BLOB_STRING]:Buffer});
  const report={mode:process.argv[2],checked:0,shrunk:0,already_compact:0,raw_saved_bytes:0,max_lua_us:0,stages:[]};
  const memory=async()=>Number((await client.info('memory')).match(/^used_memory:(\d+)/m)?.[1]);
  report.memory_before=await memory();
  try{
    const body=await client.hGet(`pulse:stg:{${RUN}}:runtime:source:release350:data`,'payload');
    const release=JSON.parse(body),rows=release.universe;
    if(release.mapping_version!==MAPPING||release.policy!=='top350.quote_24h_usdt.v1'||!Array.isArray(rows)||rows.length!==350
      ||'sha256:'+crypto.createHash('sha256').update(canonical(rows)).digest('hex')!==MAPPING)throw Error('manifest_refused');
    for(let i=0;i<rows.length;i++){
      const r=rows[i];if(r.id!==i||!/^B-[A-Z0-9]{1,24}_USDT$/.test(r.pair_id)
        ||r.symbol!==r.pair_id.slice(2,-5)+'USDT')throw Error('member_refused');
      const prefix=`pulse:stg:{${CAPS}}:${r.pair_id}:oi:`,key=prefix+'frame',lease=prefix+'lease';
      if(await client.pTTL(key)!==-1)throw Error('frame_lifecycle_refused');
      let owner;
      if(report.mode==='--apply'){
        owner=await client.eval(ACQUIRE,{keys:[lease,prefix+'fence'],arguments:[crypto.randomUUID()]});
        if(!owner)throw Error('active_pair_lease_refused');
      }
      try{
        const raw=await binary.sendCommand(['GETRANGE',key,'0','25152']);
        const converted=spawnSync('python3',['-m','pulse.store.oi_shrink'],{cwd:'/opt/pulse-stream/shrink-pure',
          input:JSON.stringify({pair:r.pair_id,symbol:r.symbol,frame:raw.toString('base64')}),encoding:'utf8',timeout:10000,maxBuffer:65536});
        if(converted.status!==0)throw Error('offline_conversion_refused');
        const out=JSON.parse(converted.stdout),next=Buffer.from(out.frame,'base64');
        if(out.before!==raw.length||out.after!==5068||next.length!==5068)throw Error('conversion_bound_refused');
        report.checked++;
        if(raw.equals(next)){report.already_compact++;continue;}
        if(raw.length!==25152)throw Error('old_frame_bound_refused');
        if(report.mode==='--apply'){
          if(report.shrunk===0){
            for(const [o,h,b] of [['retired-owner',sha(raw),next],[owner,'0'.repeat(40),next],[owner,sha(raw),Buffer.alloc(1)]]){
              const rejected=await client.eval(CAS,{keys:[key,lease],arguments:[o,h,b,sha(b)]});
              if(rejected[0]==='shrunk')throw Error('negative_fence_failed');
              const untouched=await binary.sendCommand(['GETRANGE',key,'0','25152']);
              if(!untouched.equals(raw))throw Error('negative_fence_mutated');
            }
            report.negative_cases=3;
          }
          const result=await client.eval(CAS,{keys:[key,lease],arguments:[owner,sha(raw),next,sha(next)]});
          if(result[0]!=='shrunk')throw Error('frame_cas_refused');
          report.max_lua_us=Math.max(report.max_lua_us,Number(result[1]));
          const actual=await binary.sendCommand(['GETRANGE',key,'0','5068']);
          if(!actual.equals(next)||await client.pTTL(key)!==-1)throw Error('post_write_refused');
          report.shrunk++;report.raw_saved_bytes+=raw.length-next.length;
        }
      }finally{if(owner)await client.eval(RELEASE,{keys:[lease],arguments:[owner]});}
      if((i+1)%25===0){const stage={pairs:i+1,used_memory:await memory()};report.stages.push(stage);console.log(JSON.stringify(stage));}
    }
    report.memory_after=await memory();console.log(JSON.stringify(report));
  }finally{await client.quit();}
}
if(require.main===module)main().catch(e=>{console.error(JSON.stringify({status:'refused',reason:/^[a-z_]+$/.test(e.message)?e.message:'operation_failed'}));process.exitCode=1;});
module.exports={CAS,ACQUIRE,RELEASE,canonical};
