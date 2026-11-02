"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const M=require("../frontend/src/pulse/pulse_model.js");
const box={Date};vm.createContext(box);vm.runInContext(fs.readFileSync("frontend/src/pulse/pulse_fixture.js","utf8"),box);
const snap=box.createPulseDesignFixture(M,1000), idx=new M.Index(snap);
const opts={metric:"volatility",window:"24H",sort:"change",filters:M.defaults()};
for(const descending of [true,false])for(const window of M.WINDOWS){
  const values=[-.2,0,.4,.4,NaN,null];
  const actual=idx.select({...opts,descending,window},1000,r=>({price:values[idx.byId.get(r.id)%6],changes:{[window]:values[idx.byId.get(r.id)%6]}})).ids;
  const expected=Array.from(snap.rows,(_,i)=>i).sort((a,b)=>{
    const x=values[a%6],y=values[b%6],okx=Number.isFinite(x),oky=Number.isFinite(y);
    return okx!==oky?(okx?-1:1):okx?((descending?y-x:x-y)||a-b):a-b;
  });
  assert.deepEqual(actual,expected);
}
// The displayed live frame changes ordering without rebuilding an index; NaN remains last.
let current=new Map(snap.rows.map((r,i)=>[r.id,i]));
const read=r=>({price:current.get(r.id),changes:{"24H":current.get(r.id)}});
const first=idx.select({...opts,descending:true},1000,read).ids;
current.set(snap.rows[0].id,100000);
const second=idx.select({...opts,descending:true},1000,read).ids;
assert.notEqual(first[0],second[0]);assert.equal(second[0],0);
for(const size of [350,5000]){
  const large={...snap,rows:Array.from({length:size},(_,i)=>({...snap.rows[i%snap.rows.length],id:"B-T"+i+"_USDT",symbol:"T"+i}))};
  const big=new M.Index(large),times=[];
  for(let i=0;i<100;i++){const start=performance.now();big.select({...opts,descending:!!(i%2)},1000,r=>({changes:{"24H":Number(r.id.slice(3,-5))%97}}));times.push(performance.now()-start);}
  times.sort((a,b)=>a-b);console.log(JSON.stringify({mechanism:"live_numeric_sort",size,p50_ms:times[50],p95_ms:times[95],max_hz:4}));
}
console.log("Live sort: every horizon, signed numeric values, ties, absent values and tick ordering pass");
