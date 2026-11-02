/* ADR-0189 offline retry-decision CPU bound; not a provider load generator. */
'use strict';
const {performance}=require('node:perf_hooks');
const {transientRetryDelay}=require('../../pulse/stream/bridge.cjs');
const values=[];
for(let run=0;run<10;run++){
 const start=performance.now();
 for(let i=0;i<100000;i++){
  const delay=transientRetryDelay(503,{'retry-after':'45'},1800000000000,i%3,()=>.5);
  if(delay!==47500&&delay!==62500)throw Error('invalid_retry_benchmark');
 }
 values.push((performance.now()-start)*1000/100000);
}
values.sort((a,b)=>a-b);
console.log(JSON.stringify({iterations:100000,repeats:10,decision_us:{median:values[4],max:values[9]},
 exchange_calls:0,redis_calls:0,extra_pair_state:'one delay and at most one timer',
 not_measured:['real future provider failure','hosted throughput']}));
