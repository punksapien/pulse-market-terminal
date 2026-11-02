"use strict";
const assert=require("node:assert/strict"),{run}=require("../pulse/stream/daemon.cjs");
const {createBridge}=require("../pulse/stream/bridge.cjs");
(async()=>{
  const stop=new AbortController();let closed=0;
  const env={VERCEL_ENV:"preview",PULSE_STREAM_DAEMON_ENABLED:"1"};
  const fake=()=>({collect:async(ms,o)=>{assert.equal(ms,55000);assert.equal(o.continuous,true);assert.equal(o.signal,stop.signal);return {status:"completed"};},close:async()=>{closed++;}});
  assert.equal(await run({env,signal:stop.signal,bridgeFactory:fake,log:()=>{}}),0);
  assert.equal(closed,1);
  await assert.rejects(run({env:{...env,VERCEL_ENV:"production"},signal:stop.signal,bridgeFactory:fake}),/scope/);
  await assert.rejects(run({env:{VERCEL_ENV:"preview"},signal:stop.signal,bridgeFactory:fake}),/scope/);
  const failure=()=>({collect:async()=>{throw Error("secret provider detail must never be logged");},close:async()=>{closed++;}});
  await assert.rejects(run({env,signal:stop.signal,bridgeFactory:failure}),/provider/);
  assert.equal(closed,2,"failure closes connections too");
  const refusal=()=>({collect:async()=>({status:"unavailable",reason:"upstream_refused",upstream_status:429}),close:async()=>{closed++;}});
  assert.equal(await run({env,signal:stop.signal,bridgeFactory:refusal,log:()=>{}}),78);
  assert.equal(closed,3,"upstream refusal closes and requires operator intervention");
  assert.equal(await run({env:{...env,VERCEL_ENV:'production',PULSE_PRODUCTION_ENABLED:'1',
    PULSE_UNIVERSE_MODE:'all',PULSE_RELEASE_TOP350:'1'},signal:stop.signal,bridgeFactory:fake,log:()=>{}}),0);
  assert.match(require("node:fs").readFileSync(require("node:path").join(__dirname,"../ops/pulse-stream.service"),"utf8"),/^RestartPreventExitStatus=78$/m);
  const b=createBridge({env:{VERCEL_ENV:"preview",PULSE_STREAM_ENABLED:"1",PULSE_RUNTIME_RUN_ID:"a".repeat(32),PULSE_MAPPING_VERSION:"sha256:"+"b".repeat(64),PULSE_REDIS_URL:"rediss://default:fake@fixture.invalid:6380"}});
  await assert.rejects(b.collect(55000,{continuous:true,signal:stop.signal}),/daemon_scope_refused/);
  console.log("stream daemon: Preview-only scope, explicit continuous opt-in, shutdown and connection cleanup pass");
})().catch(e=>{console.error(e);process.exitCode=1;});
