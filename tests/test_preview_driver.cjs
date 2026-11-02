"use strict";
const assert=require("node:assert/strict"),D=require("../pulse/stream/preview_driver.cjs");
const env={VERCEL_ENV:"preview",PULSE_PREVIEW_DRIVER_ENABLED:"1",
  PULSE_PREVIEW_ORIGIN:"https://volatility-terminal-fixture-punksapien-projects.vercel.app",
  PULSE_WORKER_SECRET:"w".repeat(40),PULSE_PREVIEW_BYPASS:"b".repeat(40)};
const c=D.configuration(env);
for(const value of ["https://attacker.invalid","http://volatility-terminal-fixture-punksapien-projects.vercel.app",
  env.PULSE_PREVIEW_ORIGIN+"/x",env.PULSE_PREVIEW_ORIGIN+"?x=1",env.PULSE_PREVIEW_ORIGIN+"#x"])
  assert.throws(()=>D.configuration({...env,PULSE_PREVIEW_ORIGIN:value}));
assert.throws(()=>D.configuration({...env,VERCEL_ENV:"production"}));
assert.throws(()=>D.configuration({...env,PULSE_WORKER_SECRET:"invalid\nsecret"}));
const prod={VERCEL_ENV:'production',PULSE_PRODUCTION_DRIVER_ENABLED:'1',
  PULSE_PRODUCTION_ORIGIN:'https://dcxpulse.vercel.app',PULSE_WORKER_SECRET:env.PULSE_WORKER_SECRET};
assert(D.configuration(prod).production);assert.equal(D.configuration(prod).bypass,null);
assert.throws(()=>D.configuration({...prod,PULSE_PRODUCTION_ORIGIN:env.PULSE_PREVIEW_ORIGIN}));
(async()=>{
  const prodStop=new AbortController(),prodPaths=[];
  await D.run({env:prod,signal:prodStop.signal,sleep:async ms=>{if(ms>0)prodStop.abort();},
    fetchImpl:async(url,o)=>{prodPaths.push(new URL(url).pathname);assert(!('x-vercel-protection-bypass' in o.headers));
      return new Response('{}');},log:()=>{}});
  assert.deepEqual(prodPaths,['/api/pulse_live'],'production crons exclusively own slow lanes');
  const good=async(url,o)=>{
    assert.equal(url,c.origin+"/api/pulse_worker");assert.equal(o.method,"POST");assert.equal(o.redirect,"error");
    assert.equal(o.headers.Authorization,"Bearer "+env.PULSE_WORKER_SECRET);
    return new Response(JSON.stringify({status:"incomplete",elapsed_ms:123,secret:"must_not_log",reason:"provider secret"}),{status:200});
  };
  assert.deepEqual(await D.request(c,"/api/pulse_worker",{fetchImpl:good}),
    {path:"/api/pulse_worker",http:200,elapsed_ms:123,status:"incomplete"});
  const diagnostic=await D.request(c,"/api/pulse_worker",{fetchImpl:async()=>new Response(JSON.stringify({
    scheduled_jobs:{oi:10,secret:"never log"},job_time_ms:{oi:{attempts:10,sum_ms:100,max_ms:20,secret:"never log"},secret:{attempts:1}}
  }))});
  assert.deepEqual(diagnostic.scheduled_jobs,{oi:10});
  assert.deepEqual(diagnostic.job_time_ms,{oi:{attempts:10,sum_ms:100,max_ms:20}});
  const memory=await D.request(c,"/api/pulse_live",{fetchImpl:async()=>new Response(JSON.stringify({
    error_type:"ExpansionStopped",detail:"staging_memory_gate_headroom_denied",stopped:"admission:budget_exhausted:1"
  }),{status:503})});
  assert.equal(memory.error_type,"ExpansionStopped");assert.equal(memory.detail,"staging_memory_gate_headroom_denied");
  assert.equal(memory.stopped,"admission:budget_exhausted:1");
  const unsafe=await D.request(c,"/api/pulse_live",{fetchImpl:async()=>new Response(JSON.stringify({
    error_type:"private-provider-detail",detail:"private-provider-detail",stopped:"admission:private-provider-detail"
  }),{status:503})});
  assert.deepEqual(unsafe,{path:"/api/pulse_live",http:503});
  const receipt=await D.request(c,"/api/pulse_worker",{fetchImpl:async()=>new Response(JSON.stringify({
    memory:{used_memory:12345,mem_fragmentation_ratio:1.1,secret:'never log'},
    quota:{history_charged:400,history_cap:850,secret:'never log'},history_budget_paused:false,
    binance_header_paused:true,memory_pressure:'normal'
  }))});
  assert.deepEqual(receipt.memory,{used_memory:12345,mem_fragmentation_ratio:1.1});
  assert.deepEqual(receipt.quota,{history_charged:400,history_cap:850});
  assert.equal(receipt.history_budget_paused,false);assert.equal(receipt.binance_header_paused,true);
  assert.equal(receipt.memory_pressure,'normal');
  await assert.rejects(D.request(c,"/api/admin",{fetchImpl:good}),/driver_path/);
  await assert.rejects(D.request(c,"/api/pulse_publish",{fetchImpl:async()=>new Response("x".repeat(65537))}),/driver_response_bound/);
  const controller=new AbortController(),paths=[],logs=[];
  const exit=await D.run({env,signal:controller.signal,sleep:async()=>{},
    fetchImpl:async url=>{paths.push(new URL(url).pathname);return new Response("{}",{status:403});},log:s=>logs.push(s)});
  assert.equal(exit,78);assert(paths.length<=4);assert.equal(new Set(paths).size,paths.length);
  assert(logs.every(s=>!s.includes(env.PULSE_WORKER_SECRET)&&!s.includes(env.PULSE_PREVIEW_BYPASS)));
  const stop=new AbortController(),sleeps=[],failed=[];
  await D.run({env,signal:stop.signal,now:()=>0,sleep:async ms=>{
    sleeps.push(ms);if(ms===60000)stop.abort();
  },fetchImpl:async url=>{failed.push(url);throw Error("private transport detail");},log:s=>{
    assert(!s.includes("private transport detail"));
  }});
  assert(failed.length<=4);assert(sleeps.includes(60000));
  console.log("Preview driver: scope, fixed paths, bounded receipts, safe logs, refusal and next-cycle retry checks passed");
})().catch(e=>{console.error(e);process.exitCode=1;});
