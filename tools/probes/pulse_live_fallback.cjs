/* ADR-0203: finite operator fallback, existing live endpoint/leases/quotas only. */
'use strict';
const {run}=require('../../pulse/stream/preview_driver.cjs');

function boundedFetch(until,stop,{now=Date.now,fetchImpl=fetch}={}){
  return (...args)=>{
    // Reserve the deployed function's full90s lifetime, not just the client timeout.
    if(now()+95000>=until){stop.abort();throw Error('fallback_window_complete');}
    return fetchImpl(...args);
  };
}

async function main(config){
  if(!config||!Number.isInteger(config.seconds)||config.seconds<120||config.seconds>600
    ||typeof config.secret!=='string')throw Error('fallback_scope');
  const stop=new AbortController(),until=Date.now()+config.seconds*1000;
  const timer=setTimeout(()=>stop.abort(),config.seconds*1000);
  const halt=()=>stop.abort();
  process.once('SIGINT',halt);process.once('SIGTERM',halt);
  try{
    return await run({env:{VERCEL_ENV:'production',PULSE_PRODUCTION_DRIVER_ENABLED:'1',
      PULSE_PRODUCTION_ORIGIN:'https://dcxpulse.vercel.app',PULSE_WORKER_SECRET:config.secret},
      signal:stop.signal,fetchImpl:boundedFetch(until,stop)});
  }finally{
    clearTimeout(timer);process.removeListener('SIGINT',halt);process.removeListener('SIGTERM',halt);
  }
}
module.exports={boundedFetch,main};
if(require.main===module){
  const fs=require('node:fs');
  const body=fs.readFileSync(0,'utf8');
  if(body.length>2048)throw Error('fallback_input_bound');
  main(JSON.parse(body)).then(code=>{process.exitCode=code;}).catch(()=>{
    console.error('{"status":"fallback_stopped"}');process.exitCode=1;
  });
}
