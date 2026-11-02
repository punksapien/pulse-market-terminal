"use strict";
const assert=require("node:assert/strict");
const H=require("../frontend/src/pulse/pulse_history.js");
const M=require("../frontend/src/pulse/pulse_model.js");
assert.deepEqual(M.detailParts("funding"),["funding","funding_change","oi"]);
assert.deepEqual(M.detailParts("long_short"),["ls","oi","funding"]);
assert.deepEqual(M.detailParts("open_interest"),["oi","ls","funding"]);
const NOW=1800000000000,MAP="sha256:"+"a".repeat(64);
function doc(){
  const s=(unit)=>({unit,label:"Retained observations",status:"available",reason:null,points:[[NOW-600000,"1.0"],[NOW-300000,"2.0"]]});
  return {schema:"pulse.history.v1",mapping_version:MAP,generation:"sha256:"+"b".repeat(64),bucket:0,generated_ms:NOW,expires_ms:NOW+180000,
    rows:[0,1].map(id=>({id,pair_id:id?"B-ETH_USDT":"B-BTC_USDT",series:{open_interest:s("quantity"),long_short:s("ratio"),funding:s("fraction"),funding_change:s("fraction"),volatility:Object.fromEntries(["1H","4H","24H"].map(h=>[h,s("fraction")]))}}))};
}
(async()=>{
  H.validate(doc(),MAP,0,NOW);
  const high=doc();high.bucket=34;high.expires_ms=NOW+720000;high.rows=[{...high.rows[0],id:349}];
  H.validate(high,MAP,34,NOW);
  const full=H.create({now:()=>NOW,url:id=>"/api/pulse?history="+id,fetch:async()=>({status:200,text:async()=>JSON.stringify(high)})});
  assert.equal((await full.load(349,MAP)).id,349);
  await assert.rejects(()=>full.load(350,MAP),/identity/);
  for(const change of [d=>d.mapping_version="sha256:"+"c".repeat(64),d=>d.expires_ms=NOW,d=>d.rows[0].id=20,
    d=>d.rows.push(d.rows[0]),d=>d.rows[0].series.open_interest.unit="fraction",d=>d.rows[0].series.funding.points[1][0]=NOW-600000,
    d=>d.rows[0].series.funding.points[0][1]="NaN",d=>d.rows[0].series.long_short.points[0][1]="0"]){
    const bad=doc();change(bad);assert.throws(()=>H.validate(bad,MAP,0,NOW));
  }
  let calls=0,now=NOW;
  const client=H.create({now:()=>now,url:id=>"/api/pulse?history="+id,fetch:async()=>{calls++;return {status:200,text:async()=>JSON.stringify(doc())};}});
  const [a,b]=await Promise.all([client.load(0,MAP),client.load(1,MAP)]);
  assert.equal(a.id,0);assert.equal(b.id,1);assert.equal(calls,1,"same bucket has one in-flight request");
  await client.load(0,MAP);assert.equal(calls,1,"cached sheet re-open performs no fetch");
  now=NOW+180000;assert.equal(client.peek(0,MAP),null);
  await assert.rejects(()=>client.load(0,MAP));assert.equal(client.peek(0,MAP),null);
  const broken=H.create({now:()=>NOW,url:()=>"/history",digest:async()=>"wrong",fetch:async()=>({status:200,text:async()=>JSON.stringify(doc())})});
  await assert.rejects(()=>broken.load(0,MAP),/digest/);assert.equal(broken.peek(0,MAP),null);
  console.log("Lazy history: units, mapping, time order, expiry, corruption and shared bucket caching passed.");
})().catch(e=>{console.error(e);process.exitCode=1;});
