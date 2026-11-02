/* ADR-0101: existing-host staging publisher. No HTTP listener or production mode. */
"use strict";
const {createBridge}=require("./bridge.cjs");
async function run({env=process.env,signal,bridgeFactory=createBridge,log=console.log}={}) {
  const production=env.VERCEL_ENV==='production'&&env.PULSE_PRODUCTION_ENABLED==='1'
    &&env.PULSE_UNIVERSE_MODE==='all'&&env.PULSE_RELEASE_TOP350==='1';
  if(env.PULSE_STREAM_DAEMON_ENABLED!=="1"||(env.VERCEL_ENV!=="preview"&&!production))throw Error("daemon_scope_refused");
  const bridge=bridgeFactory({env});
  // ADR-0161: bounded safe heartbeat; no source payloads or credentials in logs.
  const heartbeat=setInterval(()=>{
    if(bridge.stats)log(JSON.stringify({service:"pulse-staging-stream",status:"running",...bridge.stats}));
  },30000);
  heartbeat.unref();
  try {
    const result=await bridge.collect(55000,{continuous:true,signal});
    log(JSON.stringify({service:"pulse-staging-stream",...result}));
    if(result.reason==="upstream_refused")return 78;
    return result.status==="completed"?0:1;
  } finally {clearInterval(heartbeat);await bridge.close();}
}
if(require.main===module){
  const stop=new AbortController();
  for(const sig of ["SIGINT","SIGTERM"])process.once(sig,()=>stop.abort());
  run({signal:stop.signal}).then(code=>{process.exitCode=code;}).catch(()=>{
    console.error('{"service":"pulse-staging-stream","reason":"stopped_fail_closed"}');process.exitCode=1;
  });
}
module.exports={run};
