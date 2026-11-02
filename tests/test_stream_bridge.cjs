"use strict";
const assert=require("node:assert/strict"), {createHash}=require("node:crypto"), {EventEmitter}=require("node:events");
const B=require("../pulse/stream/bridge.cjs");
const F=require("../frontend/src/pulse/pulse_bridge.js");
const now=1800000000000;
for(const code of [500,502,503,504]){
  assert.equal(B.transientRetryDelay(code,{},now,0,()=>0),30000);
  assert.equal(B.transientRetryDelay(code,{},now,9,()=>.5),62500);
  assert.equal(B.transientRetryDelay(code,{'retry-after':'120'},now,0,()=>0),120000);
  assert.equal(B.transientRetryDelay(code,{'retry-after':'0'},now,0,()=>0),30000);
  const date={'retry-after':new Date(now+120000).toUTCString(),date:new Date(now-10000).toUTCString()};
  assert.equal(B.transientRetryDelay(code,date,now,0,()=>0),131000);
  for(const value of ['bad','10.5','2147484','9999999999','',[],null])
    assert.equal(B.transientRetryDelay(code,{'retry-after':value},now,0,()=>0),null);
}
for(const code of [null,101,200,301,400,401,403,404,418,429,451,501,505])
  assert.equal(B.transientRetryDelay(code,{},now,0,()=>0),null);
const env={VERCEL_ENV:"preview",PULSE_STREAM_ENABLED:"1",PULSE_RUNTIME_RUN_ID:"a".repeat(32),PULSE_MAPPING_VERSION:"sha256:"+"b".repeat(64),PULSE_REDIS_URL:"rediss://default:synthetic@fixture.invalid:6380"};
assert.equal(B.configuration(env).stopBytes,120000000);
for(const value of ['40000000','115000000','120000000'])
  assert.equal(B.configuration({...env,PULSE_MEMORY_STOP_BYTES:value}).stopBytes,Number(value));
for(const value of ['39999999','120000001','1e8','NaN','','-1',' 120000000','120000000\n','１20000000',120000000,null])
  assert.throws(()=>B.configuration({...env,PULSE_MEMORY_STOP_BYTES:value}),/invalid_memory_stop_bytes/);
assert(B.configuration(env).bus.endsWith(":ticks:v2"));
const packet=(T=now,p="101.25")=>'42["price-change",{"data":'+JSON.stringify(JSON.stringify({T,p,pr:"f"}))+'}]';
assert.equal(B.parseTick(packet(),now).price,"101.25");
for(const p of ["0","-1","1e3","NaN",null,1,"9".repeat(41)])assert.equal(B.parseTick(packet(now,p),now),null);
for(const T of [-1,now+1001,now-60001,1.5,null])assert.equal(B.parseTick(packet(T),now),null);
for(const raw of ["42[", "9".repeat(5000),"", "42[]"])assert.equal(B.parseTick(raw,now),null);
assert.throws(()=>B.configuration({...env,VERCEL_ENV:"production"}));
const prod={...env,VERCEL_ENV:'production',PULSE_PRODUCTION_ENABLED:'1',PULSE_UNIVERSE_MODE:'all',PULSE_RELEASE_TOP350:'1'};
const pc=B.configuration(prod);
assert(pc.production);assert(pc.bus.startsWith('pulse:prod:'));assert(pc.lease.startsWith('pulse:prod:'));
assert(pc.registry.startsWith('pulse:prod:'));
for(const script of [B.READ_REGISTRY,B.FENCED_PUBLISH,B.RELEASE]){
  assert.equal(B.scopedScript(script,B.configuration(env)),script);
  assert(!B.scopedScript(script,pc).includes('pulse:stg:'));
  assert(B.scopedScript(script,pc).includes("1,11)~='pulse:prod:'"));
}
assert.throws(()=>B.scopedScript("return 'pulse:stg:payload'",pc));
assert.throws(()=>B.configuration({...prod,PULSE_REDIS_URL:'redis://default:x@fixture.invalid:6380',PULSE_STAGING_PLAINTEXT_APPROVED:'1'}));
assert(B.configuration({...prod,PULSE_REDIS_URL:'redis://default:x@fixture.invalid:6380',PULSE_PRODUCTION_PLAINTEXT_APPROVED:'1'}));
assert.throws(()=>B.configuration({...env,PULSE_REDIS_URL:"redis://default:x@fixture.invalid:6380"}));
assert.throws(()=>B.configuration({...env,PULSE_REDIS_URL:env.PULSE_REDIS_URL+"?ssl=false"}));
assert(B.authorised("Bearer "+"s".repeat(32),"s".repeat(32)));
assert(!B.authorised("Bearer wrong","s".repeat(32)));
const universe=Array.from({length:50},(_,i)=>({id:i,pair_id:`B-P${i}_USDT`,symbol:`P${i}USDT`}));
const hash=x=>createHash("sha256").update(x).digest("hex");
const mapping="sha256:"+hash(B.canonical(universe));
const payload=JSON.stringify({universe,mapping_version:mapping});
const fields=[payload,hash(payload),String(now-1),String(now+60000)];
assert.equal(B.registry(fields,mapping,now).length,50);
assert.throws(()=>B.registry([...fields.slice(0,3),String(now)],mapping,now));
assert.throws(()=>B.registry(fields,env.PULSE_MAPPING_VERSION,now));
const releaseUniverse=Array.from({length:350},(_,i)=>({id:i,pair_id:`B-P${i}_USDT`,symbol:`P${i}USDT`}));
const releaseMapping="sha256:"+hash(B.canonical(releaseUniverse));
const releasePayload=JSON.stringify({universe:releaseUniverse,mapping_version:releaseMapping,policy:"top350.quote_24h_usdt.v1"});
const releaseFields=[releasePayload,hash(releasePayload),String(now-1),String(now+60000)];
assert.equal(B.registry(releaseFields,releaseMapping,now,350).length,350);
assert.throws(()=>B.registry(releaseFields,releaseMapping,now),/registry_mapping/);
assert.throws(()=>B.registry(fields,mapping,now,350),/registry_mapping/);
assert.throws(()=>B.registry(releaseFields,releaseMapping,now,351),/registry_mapping/);
assert.equal(B.configuration({...env,PULSE_RELEASE_TOP350:"1"}).bytesPerSecond,524288);
assert(B.configuration({...env,PULSE_RELEASE_TOP350:"1"}).registry.endsWith(":source:release350:data"));
assert(B.configuration(env).registry.endsWith(":source:registry:data"));
const pending=new Map(releaseUniverse.map(r=>[r.pair_id,{source_ms:now,price:"123.45"}]));
const delivered=[];
for(let i=0;i<7;i++){
  // Hot pairs update without jumping the queue ahead of quieter contracts.
  pending.set("B-P349_USDT",{source_ms:now+i,price:"124.45"});
  const rows=B.pendingRows(pending);
  assert.equal(rows.length,50);
  assert(Buffer.byteLength(JSON.stringify({...batchPlaceholder(),rows}))<B.MAX_FRAME);
  for(const row of rows){delivered.push(row[0]);pending.delete(row[0]);}
}
assert.deepEqual(delivered,releaseUniverse.map(r=>r.pair_id));
assert.equal(pending.size,0);
function batchPlaceholder(){return {schema:"pulse.ticks.v1",mapping_version:releaseMapping,sent_ms:now};}
const batch={schema:"pulse.ticks.v1",mapping_version:mapping,sent_ms:now,rows:[["B-BTC_USDT",now,"103.125"]]};
assert(B.batchValid(batch,mapping,now));
assert(!B.batchValid({...batch,rows:[["B-BTC_USDT",now,"-1"]]},mapping,now));
assert(!B.batchValid(batch,mapping,now+2001));

let time=now,id=0;const timers=new Map(),sockets=[],ticks=[];
class WS {constructor(url){this.url=url;sockets.push(this);}close(){this.closed=true;this.onclose?.();}}
const client=F.create({url:"wss://fixture.invalid/api/pulse_stream",mapping:()=>mapping,WebSocketImpl:WS,now:()=>time,
  setTimer:(f,ms)=>{timers.set(++id,{f,at:time+ms});return id;},clearTimer:k=>timers.delete(k),random:()=>0.5,onTick:(p,t)=>ticks.push([p,t])});
client.setPairs(["B-BTC_USDT","B-ETH_USDT"]);assert.equal(sockets.length,1);
client.setPairs([...releaseUniverse.map(r=>r.pair_id)]);
sockets[0].onmessage({data:JSON.stringify({...batch,rows:[["B-P349_USDT",now,"123.45"]]})});
assert.equal(ticks.length,1,"350th offscreen pair updates without a second connection");ticks.length=0;
client.setPairs(["B-BTC_USDT","B-ETH_USDT"]);
sockets[0].onmessage({data:JSON.stringify(batch)});assert.equal(ticks.length,1);assert.equal(client.state.status,"live");
sockets[0].onmessage({data:JSON.stringify({...batch,mapping_version:env.PULSE_MAPPING_VERSION})});assert.equal(ticks.length,1);
sockets[0].onmessage({data:JSON.stringify({...batch,rows:[...batch.rows,["B-BTC_USDT",now,"0"]]})});assert.equal(ticks.length,1,"whole invalid batch refused");
client.setPairs([]);assert(sockets[0].closed);assert.equal(client.state.status,"idle");
client.setPairs(["B-BTC_USDT"]);assert.equal(sockets.length,2);sockets[1].onclose();assert.equal(client.state.status,"delayed");
client.stop();assert.equal(timers.size,0);

// No public attach or read opens an upstream. Redis publisher and subscriber are shared per instance.
let upstream=0,connections=0,subscription;
const fakeRedis={on(){},connect:async()=>{connections++;},destroy(){},duplicate(){return this;},subscribe:async(bus,cb)=>{subscription=cb;}};
class Upstream {constructor(){upstream++;}}
const bridge=B.createBridge({env,redisFactory:()=>fakeRedis,Socket:Upstream,now:()=>now});
class Peer extends EventEmitter{constructor(){super();this.readyState=1;this.bufferedAmount=0;this.sent=[];}send(m){this.sent.push(m);}close(code){this.code=code;this.emit("close");}}
(async()=>{
  const boundedRelease=B.createBridge({env:{...env,PULSE_RELEASE_TOP350:"1"}});
  await assert.rejects(boundedRelease.collect(),/stream_persistent_runtime_required/);
  await boundedRelease.close();
  const a=new Peer(),b=new Peer();await bridge.attach(a);await bridge.attach(b);
  assert.equal(upstream,0);assert.equal(connections,2,"one shared command connection and one subscriber");
  const msg=JSON.stringify({...batch,mapping_version:env.PULSE_MAPPING_VERSION});subscription(msg);
  assert.equal(a.sent.length,1);assert.equal(b.sent.length,1);
  a.bufferedAmount=65537;subscription(msg);assert.equal(a.code,1013);
  b.emit("message","subscribe BTC");assert.equal(b.code,1008);
  await bridge.close();console.log("stream bridge: validation, passive fanout, slow-client and visibility/reconnect checks pass");
  const subs=[];
  class Sub extends EventEmitter {
    async connect(){} async subscribe(bus,cb){this.callback=cb;}
    destroy(){this.destroyed=true;this.emit("end");}
  }
  const command={on(){},async connect(){},destroy(){},duplicate(){const s=new Sub();subs.push(s);return s;}};
  const lifecycle=B.createBridge({env,redisFactory:()=>command,now:()=>now});
  const first=new Peer();await lifecycle.attach(first);first.close();
  await new Promise(r=>setTimeout(r,1100));
  assert(subs[0].destroyed,"idle subscriber is released after the last peer");
  const second=new Peer();await lifecycle.attach(second);
  subs[0].emit("end");assert.equal(second.code,undefined,"late old end cannot close successor peers");
  second.close();const third=new Peer();await lifecycle.attach(third);
  await new Promise(r=>setTimeout(r,1100));
  assert.equal(subs.length,2,"reconnect inside grace reuses subscriber");
  assert(!subs[1].destroyed,"active successor is not destroyed by an idle timer");
  await lifecycle.close();assert(subs[1].destroyed);
  let opened=0,commits=0,blocked=false,releases=0;
  const liveUniverse=universe,liveMapping=mapping;
  const livePayload=JSON.stringify({universe:liveUniverse,mapping_version:liveMapping});
  const liveFields=()=>[livePayload,hash(livePayload),String(Date.now()-1000),String(Date.now()+60000)];
  const fakeStore={on(){},connect:async()=>{},destroy(){},info:async()=>"used_memory:5000000\r\n",set:async()=>"OK",
    eval:async script=>{if(script.includes("HSTRLEN"))return liveFields();if(script===B.RELEASE){releases++;return 1;}commits++;return blocked?-1:1;}};
  class Feed extends EventEmitter {
    constructor(){super();opened++;queueMicrotask(()=>this.emit("message",Buffer.from("0{}"),false));}
    send(s){if(s==="40")queueMicrotask(()=>this.emit("message",Buffer.from("40{}"),false));else if(s.startsWith("42"))queueMicrotask(()=>this.emit("message",Buffer.from(packet(Date.now(),"101")),false));}
    terminate(){this.emit("close");}
  }
  const producer=B.createBridge({env:{...env,PULSE_MAPPING_VERSION:liveMapping},redisFactory:()=>fakeStore,Socket:Feed});
  const result=await producer.collect(1250);
  assert.equal(result.status,"completed");assert(result.ticks>0);assert(commits>0);assert(opened<=2,"handshakes paced independently of viewers");
  assert.equal(releases,1,"successful completion expires its own lease before returning");
  blocked=true;const failed=await producer.collect(1250);
  assert.equal(failed.status,"unavailable");assert.equal(failed.reason,"stream_fenced_out");
  assert.equal(releases,2,"failed publication also attempts owner-checked lease expiry");
  await producer.close();
  class Refused extends EventEmitter {
    constructor(){super();queueMicrotask(()=>this.emit("unexpected-response",null,
      {statusCode:429,headers:{"retry-after":"120"},resume(){}}));}
    terminate(){this.emit("close");}
  }
  blocked=false;
  const refused=B.createBridge({env:{...env,PULSE_MAPPING_VERSION:liveMapping},redisFactory:()=>fakeStore,Socket:Refused});
  const refusal=await refused.collect(1250);
  assert.equal(refusal.status,"unavailable");assert.equal(refusal.upstream_status,429);
  assert.equal(refusal.retry_after_ms,120000);assert.equal(refusal.reason,"upstream_refused");
  await refused.close();
  let mixedOpened=0;
  class Mixed extends EventEmitter {
    constructor(){
      super();this.index=++mixedOpened;
      queueMicrotask(()=>this.index===1?this.emit('unexpected-response',null,
        {statusCode:503,headers:{'retry-after':'120'},resume(){}}):
        this.emit('message',Buffer.from('0{}'),false));
    }
    send(s){if(s==='40')queueMicrotask(()=>this.emit('message',Buffer.from('40{}'),false));
      else if(s.startsWith('42'))queueMicrotask(()=>this.emit('message',Buffer.from(packet(Date.now(),'101')),false));}
    terminate(){if(!this.closed){this.closed=true;this.emit('close');}}
  }
  const mixed=B.createBridge({env:{...env,PULSE_MAPPING_VERSION:liveMapping},redisFactory:()=>fakeStore,Socket:Mixed});
  const survived=await mixed.collect(1250);
  assert.equal(survived.status,'completed','one503 must not stop healthy peers');
  assert.equal(survived.reason,null);assert(survived.ticks>0);assert.equal(mixed.stats.transient_refusals,1);
  assert.equal(mixedOpened,2,'no immediate retry of the failed pair');
  await mixed.close();
  blocked=false;
  const controller=new AbortController();
  const daemon=B.createBridge({env:{...env,PULSE_MAPPING_VERSION:liveMapping,PULSE_STREAM_DAEMON_ENABLED:"1"},redisFactory:()=>fakeStore,Socket:Feed});
  const cancel=setTimeout(()=>controller.abort(),350);
  const continuous=await daemon.collect(1,{continuous:true,signal:controller.signal});
  clearTimeout(cancel);
  assert.equal(continuous.status,"completed");assert(continuous.elapsed_ms>=300,"daemon ignores the HTTP deadline only under the explicit flag");
  await daemon.close();
  const empty=B.createBridge({env:{...env,PULSE_MAPPING_VERSION:liveMapping},redisFactory:()=>({...fakeStore,info:async()=>"used_memory:120000001\r\n"}),Socket:Feed});
  await assert.rejects(empty.collect(1250),/stream_memory_stop/);await empty.close();
  console.log("stream collector: paced sources, bounded completion and lease-loss refusal pass");
})().catch(e=>{console.error(e);process.exitCode=1;});
