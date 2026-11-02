"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs");
const L=require("../frontend/src/pulse/pulse_loader.js");
const wire=JSON.parse(fs.readFileSync("docs/contracts/fixtures/snapshot.valid.1H.json","utf8"));
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
  for(const hang of ["fetch","body"]){
    let broken=true, calls=0,id=0;const timers=new Map();
    const loader=L.create({now:()=>wire.generated_ms+1000,random:()=>.5,
      snapshotUrl:h=>h,liveUrl:()=>"live",
      setTimer:(fn,ms,kind)=>{timers.set(++id,{fn,ms,kind});return id;},clearTimer:k=>timers.delete(k),
      fetch:async url=>{
        calls++;
        if(broken&&hang==="fetch")return new Promise(()=>{});
        return {status:200,headers:{get:()=>null},text:()=>broken&&hang==="body"?new Promise(()=>{}):Promise.resolve(JSON.stringify(wire))};
      }});
    loader.start("1H");await flush();
    const request=[...timers.values()].find(t=>t.kind==="request");assert.equal(request.ms,12000);request.fn();
    await flush();await flush();assert.equal(loader.state.snapshotError,"request_timeout");
    broken=false;loader.wake();await flush();await flush();
    assert.equal(loader.state.snapshot.rows.length,1,"timeout releases single flight for a healthy retry");
    loader.stop();await flush();assert.equal(timers.size,0);
  }
  let resolveOld, resolveNew,id=0;const timers=new Map();let fetches=0;
  const response=value=>({status:200,headers:{get:()=>null},text:async()=>JSON.stringify(value)});
  const loader=L.create({now:()=>wire.generated_ms+1000,snapshotUrl:h=>h,liveUrl:()=>"live",
    setTimer:(fn,ms,kind)=>{timers.set(++id,{fn,ms,kind});return id;},clearTimer:k=>timers.delete(k),
    fetch:url=>{
      if(url==="live")return Promise.resolve({status:503,headers:{get:()=>null}});
      fetches++;return new Promise(resolve=>{if(fetches===1)resolveOld=resolve;else resolveNew=resolve;});
    }});
  loader.start("1H");loader.setHorizon("4H");await flush();
  resolveOld(response(wire));await flush(); // superseded finally must not clear the newer request.
  assert.equal(fetches,2);const newer=structuredClone(wire);newer.horizon="4H";
  // Existing fixture is only 1H-consistent; rejected payload must not roll the horizon back.
  resolveNew(response(newer));await flush();await flush();assert.equal(loader.state.horizon,"4H");
  loader.stop();await flush();assert.equal(timers.size,0);
  console.log("Request bounds: hung fetch/body recover; superseded requests cannot clobber new flights; stop cancels timers");
})().catch(e=>{console.error(e);process.exitCode=1;});
