/* ADR-0163: fixed scheduled Preview triggers; no exchange/Redis client, no import-time I/O. */
"use strict";
function configuration(env){
  const production=env.VERCEL_ENV==='production'&&env.PULSE_PRODUCTION_DRIVER_ENABLED==='1';
  if(!production&&(env.VERCEL_ENV!=="preview"||env.PULSE_PREVIEW_DRIVER_ENABLED!=="1"))throw Error("driver_scope");
  const u=new URL(production?env.PULSE_PRODUCTION_ORIGIN:env.PULSE_PREVIEW_ORIGIN);
  if(u.protocol!=="https:"||!(production?u.hostname==='dcxpulse.vercel.app':/^volatility-terminal-[a-z0-9]+-punksapien-projects\.vercel\.app$/.test(u.hostname))
    ||u.pathname!=="/"||u.search||u.hash||u.username||u.password||u.port)throw Error("driver_origin");
  for(const key of production?["PULSE_WORKER_SECRET"]:["PULSE_WORKER_SECRET","PULSE_PREVIEW_BYPASS"])
    if(typeof env[key]!=="string"||! /^[\x21-\x7e]{32,512}$/.test(env[key]))throw Error("driver_secret");
  return {origin:u.origin,secret:env.PULSE_WORKER_SECRET,bypass:production?null:env.PULSE_PREVIEW_BYPASS,production};
}
async function request(c,path,{fetchImpl=fetch,signal}={}){
  if(!["/api/pulse_worker","/api/pulse_publish","/api/pulse_history","/api/pulse_live"].includes(path))throw Error("driver_path");
  const timeout=AbortSignal.timeout(70000);
  const response=await fetchImpl(c.origin+path,{method:"POST",redirect:"error",
    headers:{Authorization:"Bearer "+c.secret,...(c.bypass?{"x-vercel-protection-bypass":c.bypass}:{})},
    signal:signal?AbortSignal.any([signal,timeout]):timeout});
  // Response bodies are never logged. Stream the bounded receipt, then allowlist numeric/status fields.
  let text="",size=0;const decoder=new TextDecoder();
  for await(const chunk of response.body){
    size+=chunk.byteLength;
    if(size>65536){await response.body.cancel().catch(()=>{});throw Error("driver_response_bound");}
    text+=decoder.decode(chunk,{stream:true});
  }
  let body;try{body=JSON.parse(text);}catch{body={};}
  const result={path,http:response.status};
  for(const key of ["elapsed_ms","pairs","schedule_attempted","schedule_due","price_publishes","funding_publishes",
    "metric_source_dispatches","schedule_overdue_max_ms","funding_max_publish_gap_ms"])
    if(Number.isSafeInteger(body[key])&&body[key]>=0)result[key]=body[key];
  if(Array.isArray(body.horizons)&&body.horizons.length<=3&&body.horizons.every(h=>["1H","4H","24H"].includes(h)))
    result.horizons=body.horizons;
  if(Array.isArray(body.history_buckets)&&body.history_buckets.length<=4&&body.history_buckets.every(i=>Number.isSafeInteger(i)&&i>=0&&i<35))
    result.history_buckets=body.history_buckets;
  if(Array.isArray(body.compose_phase_ms)&&body.compose_phase_ms.length<=3)
    result.compose_phase_ms=body.compose_phase_ms.map(p=>{
      const clean={};
      if(["1H","4H","24H","all"].includes(p.horizon))clean.horizon=p.horizon;
      for(const k of ["sources","rows","witnesses","assembly","validated_wire"])
        if(Number.isSafeInteger(p[k])&&p[k]>=0)clean[k]=p[k];
      return clean;
    });
  if(["published","incomplete","unavailable","lease_busy"].includes(body.status))result.status=body.status;
  if(["time_budget","memory_allocation_stop","source_transport_failed"].includes(body.stopped))result.stopped=body.stopped;
  if(["ExpansionStopped","RuntimeError","ValueError","KeyError","TypeError","TimeoutError","ConnectionError"].includes(body.error_type))result.error_type=body.error_type;
  if(["staging_memory_gate_headroom_denied","staging_memory_gate_stop","snapshot_shared_inputs_unavailable"].includes(body.detail))result.detail=body.detail;
  if(["budget_exhausted:1","budget_exhausted:2","header_probe_busy","header_refresh_required","circuit_open","budget_pruned_retry"].some(r=>body.stopped==="admission:"+r))result.stopped=body.stopped;
  if(["budget_exhausted:1","budget_exhausted:2","header_probe_busy","header_refresh_required","circuit_open","budget_pruned_retry"].some(r=>body.funding_stopped==="admission:"+r))result.funding_stopped=body.funding_stopped;
  if(["ValueError","KeyError","TypeError","TimeoutError","ConnectionError","OSError","RuntimeError"].includes(body.funding_error_type))result.funding_error_type=body.funding_error_type;
  for(const key of ["history_budget_paused","binance_header_paused"])
    if(typeof body[key]==="boolean")result[key]=body[key];
  if(["normal","admission_paused","bootstrap_repair_paused","hard_stop"].includes(body.memory_pressure))result.memory_pressure=body.memory_pressure;
  for(const [key,names] of [["memory",["used_memory","used_memory_rss","used_memory_peak","mem_fragmentation_ratio"]],
    ["quota",["history_charged","weight_charged","history_cap","coindcx_minute_charged","coindcx_second_charged","coindcx_minute_cap","coindcx_second_cap"]]]){
    const clean={};for(const name of names){const n=body[key]?.[name];if(typeof n==="number"&&Number.isFinite(n)&&n>=0&&n<1e12)clean[name]=n;}
    if(Object.keys(clean).length)result[key]=clean;
  }
  // Bounded numeric receipts only; never echo arbitrary provider/state content.
  for(const key of ["scheduled_jobs","job_time_ms"]){
    if(!body[key]||typeof body[key]!=="object"||Array.isArray(body[key]))continue;
    const clean={};
    for(const name of ["oi","instrument","global","detail","legacy","funding","references","bootstrap","period_1H","period_4H","period_24H"]){
      const v=body[key][name];
      if(key==="scheduled_jobs"&&Number.isSafeInteger(v)&&v>=0)clean[name]=v;
      if(key==="job_time_ms"&&v&&typeof v==="object"){
        const item={};for(const n of ["attempts","sum_ms","max_ms"])
          if(Number.isSafeInteger(v[n])&&v[n]>=0)item[n]=v[n];
        if(Object.keys(item).length)clean[name]=item;
      }
    }
    result[key]=clean;
  }
  return result;
}
async function run({env=process.env,signal,fetchImpl=fetch,log=console.log,
  sleep=(ms,s)=>new Promise(resolve=>{
    if(s.aborted)return resolve();
    const done=()=>{clearTimeout(t);s.removeEventListener("abort",done);resolve();};
    const t=setTimeout(done,ms);s.addEventListener("abort",done,{once:true});
  }),now=Date.now}={}){
  const c=configuration(env);
  if(!signal||typeof signal.aborted!=="boolean")throw Error("driver_signal");
  let refused=false;
  async function lane(path,offset,period=60000){
    await sleep(offset,signal);
    while(!signal.aborted&&!refused){
      const started=now();
      try{
        const result=await request(c,path,{fetchImpl,signal});
        log(JSON.stringify({service:"pulse-preview-driver",...result}));
        if([401,403,418,429,451].includes(result.http)){refused=true;break;}
      }catch(error){
        if(signal.aborted)break;
        log(JSON.stringify({service:"pulse-preview-driver",path,status:"transport_failed"}));
      }
      // At most one invocation per lane/minute, no overlap or same-cycle retries.
      await sleep(Math.max(1000,period-(now()-started)),signal);
    }
  }
  // ADR-0190: production crons own the three slow lanes; this host owns live only.
  await Promise.all(c.production?[lane("/api/pulse_live",0,50000)]:
    [lane("/api/pulse_worker",0),lane("/api/pulse_publish",20000),lane("/api/pulse_history",40000),lane("/api/pulse_live",0,50000)]);
  return refused?78:0;
}
if(require.main===module){
  const stop=new AbortController();
  for(const signal of ["SIGTERM","SIGINT"])process.once(signal,()=>stop.abort());
  run({signal:stop.signal}).then(code=>{process.exitCode=code;}).catch(()=>{
    console.error('{"service":"pulse-preview-driver","status":"configuration_refused"}');process.exitCode=78;
  });
}
module.exports={configuration,request,run};
