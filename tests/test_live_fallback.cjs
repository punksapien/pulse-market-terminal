'use strict';
const assert=require('node:assert/strict');
const {boundedFetch,main}=require('../tools/probes/pulse_live_fallback.cjs');
(async()=>{
  let at=0,calls=0;const stop=new AbortController();
  const request=boundedFetch(100000,stop,{now:()=>at,fetchImpl:async()=>{calls++;return 'ok';}});
  assert.equal(await request('allowed'),'ok');assert.equal(calls,1);assert.equal(stop.signal.aborted,false);
  at=5000;
  assert.throws(()=>request('too-late'),/fallback_window_complete/);
  assert.equal(calls,1);assert.equal(stop.signal.aborted,true);
  for(const seconds of [0,119,601,Infinity,NaN,'600'])
    await assert.rejects(main({seconds,secret:'x'.repeat(32)}),/fallback_scope/);
  console.log('finite fallback: full server-lifetime reserve and no late dispatch pass');
})().catch(e=>{console.error(e);process.exitCode=1;});
