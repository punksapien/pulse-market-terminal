"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs");
const A=require("../frontend/src/pulse/pulse_adapter.js"),M=require("../frontend/src/pulse/pulse_model.js");
const base=JSON.parse(fs.readFileSync(require("node:path").join(__dirname,"../docs/contracts/fixtures/snapshot.valid.1H.json"),"utf8"));
const field={volatility:(r,w)=>r.volatility.windows[w],long_short:r=>r.ls.global_ratio,
  funding:r=>r.funding.native_estimate,funding_change:r=>r.funding_change.settled_last,
  open_interest:(r,w)=>r.oi["change_"+w.toLowerCase()]};
let checked=0;
for(const metric of Object.keys(field))for(const window of M.WINDOWS)for(const level of [1,2,3,4]){
  const wire=structuredClone(base),now=wire.generated_ms+1;
  wire.horizon=window;
  const f=field[metric](wire.rows[0],window);
  Object.assign(f,{classification_status:"classified",classification_version:"fixture.l2.v1",level});
  const snap=A.adapt(wire,now),index=new M.Index(snap);
  if(["funding","funding_change"].includes(metric)&&window!=="1H"){
    // 1D / 7D windows are cumulative or settled-delta values; no pilot band exists for them
    assert.equal(snap.rows[0].metrics[metric][window].level,null,metric+window+" has no band");
    continue;
  }
  assert.equal(snap.rows[0].metrics[metric][window].level,level);
  assert.equal(index.select({metric,window,level},now).ids.length,1,metric+window+level);
  assert.equal(index.select({metric,window,level:level%4+1},now).ids.length,0);
  for(const invalid of ["uncalibrated","expired","unavailable","unversioned"]){
    const bad=structuredClone(wire),bf=field[metric](bad.rows[0],window);
    if(invalid==="uncalibrated")bf.classification_status="uncalibrated";
    if(invalid==="expired")bf.expires_ms=now;
    if(invalid==="unavailable"){bf.status="unavailable";bf.value=null;bf.reason="source_unavailable";}
    if(invalid==="unversioned")delete bf.classification_version;
    const rejected=A.adapt(bad,now);
    assert.equal(rejected.rows[0].metrics[metric][window].level,null,metric+invalid);
    assert.equal(new M.Index(rejected).select({metric,window,level},now).ids.length,0);
  }
  checked++;
}
// These are transport fixtures, NOT approved cutoffs or calibration evidence.
const wire=structuredClone(base),now=wire.generated_ms+1;
Object.assign(wire.rows[0].funding.native_estimate,{classification_status:"classified",classification_version:"fixture.l2.v1",level:4});
Object.assign(wire.rows[0].funding_change.settled_last,{classification_status:"classified",classification_version:"fixture.l2.v1",level:3});
const fc=wire.rows[0].funding_change,snapshot=A.adapt(wire,now);
for(const [rate,expected] of [[wire.rows[0].funding.native_estimate.value,4],["-0.0002",null]]){
  const doc={schema:A.LIVE_SCHEMA,mapping_version:wire.mapping_version,generated_ms:now,expires_ms:now+15000,
    rows:[{id:0,status:"available",h:fc.h,nft:fc.nft,fr:fc.settled_rate,efr:rate,source_ms:now,expires_ms:now+60000}]};
  const result=A.applyLiveFunding(snapshot,wire,doc,now);
  assert.equal(result.snapshot.rows[0].metrics.funding["1H"].level,expected);
  assert.equal(result.snapshot.rows[0].metrics.funding_change["1H"].level,3,"live estimate must not reclassify settled change");
  doc.rows[0].h=fc.h===8?4:8;
  assert.equal(A.applyLiveFunding(snapshot,wire,doc,now).snapshot.rows[0].metrics.funding["1H"].level,null,"interval change invalidates the old rate band");
}
// ADR-0116: a live row that carries its own backend-computed band replaces the snapshot band, even for a changed rate
{
  const liveRow=(extra)=>({schema:A.LIVE_SCHEMA,mapping_version:wire.mapping_version,generated_ms:now,expires_ms:now+15000,
    rows:[{id:0,status:"available",h:fc.h,nft:fc.nft,fr:fc.settled_rate,efr:"-0.0002",source_ms:now,expires_ms:now+60000,...extra}]});
  const levelOf=(doc)=>A.applyLiveFunding(snapshot,wire,doc,now).snapshot.rows[0].metrics.funding["1H"].level;
  assert.equal(levelOf(liveRow({level:2,classification_version:"funding.v1.l2.pilot1"})),2,"live band is used");
  assert.equal(levelOf(liveRow({level:2,classification_version:"other.v1.l2.pilot1"})),null,"foreign policy identity is not a funding band");
  assert.equal(levelOf(liveRow({level:7,classification_version:"funding.v1.l2.pilot1"})),null,"out-of-range level is refused");
  assert.equal(levelOf(liveRow({})),null,"no live band: a changed rate still has no level");
  checked++;
}
console.log(`L2 transport: ${checked} metric/window/level combinations plus unavailable, expiry, policy-version and live-funding regressions pass; no live cutoffs selected`);
