"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const A = require("../frontend/src/pulse/pulse_adapter.js");
const wire = JSON.parse(fs.readFileSync("docs/contracts/fixtures/snapshot.valid.1H.json", "utf8"));
const now = wire.generated_ms + 1000;
const snap = A.adapt(wire, now);
const doc = {schema:A.PRICE_SCHEMA,mapping_version:snap.mapping_version,generated_ms:now,expires_ms:now+15000,
  rows:[{id:0,status:"unavailable",price:null,volume:{status:"available",reason:null,value:"12345.67",unit:"USDT",source_ms:now,expires_ms:now+60000}}]};
const merge = d => A.applyLiveVolume(snap,d,now);
assert.equal(merge(doc).rows[0].volume,12345.67,"volume does not need a price or reference ladder");
const zero=structuredClone(doc);zero.rows[0].volume.value="0";assert.equal(merge(zero).rows[0].volume,0);
for(const mutate of [d=>d.mapping_version="other",d=>d.expires_ms=now,d=>d.rows[0].volume.source_ms=now+5001,
  d=>d.rows[0].volume.value="-1",d=>d.rows[0].volume.unit="BTC",d=>d.rows[0].volume.expires_ms=now,
  d=>d.rows.push(d.rows[0]),d=>d.rows[0].volume.source_ms=0]) {
  const bad=structuredClone(doc);mutate(bad);assert.equal(merge(bad).rows[0].volume,snap.rows[0].volume);
}
const expired={...snap,rows:snap.rows.map(r=>({...r,volume_expires_ms:now}))};
const old=structuredClone(doc);old.expires_ms=now;
assert.equal(A.applyLiveVolume(expired,old,now).rows[0].volume,null,"expired fallback is never kept as fresh");
console.log("Fast volume: independent price/refs, zero, mapping, source, expiry and invalid-data checks pass");
const v2=structuredClone(doc);Object.assign(v2.rows[0].volume,{definition_version:"volume.rt.v2",expires_ms:now+300000});
const accepted=merge(v2);
for(const age of [16000,60000,299999]){
  const held=A.retainVolume(expired,accepted,now+age);
  assert.equal(held.rows[0].volume,12345.67);
  assert.equal(held.rows[0].volume_expires_ms,now+300000);
  assert.equal(held.rows[0].price,expired.rows[0].price,"volume retention never replaces a price");
}
assert.equal(A.retainVolume(expired,accepted,now+300000).rows[0].volume,null);
assert.equal(A.retainVolume({...expired,mapping_version:"other"},accepted,now+16000).rows[0].volume,null);
const unsupported=structuredClone(v2);unsupported.rows[0].volume.definition_version="v3";
assert.equal(merge(unsupported).rows[0].volume,snap.rows[0].volume);
