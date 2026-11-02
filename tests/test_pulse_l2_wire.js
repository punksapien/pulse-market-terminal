"use strict";
const fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
const W=require("../frontend/src/pulse/pulse_wire.js"),A=require("../frontend/src/pulse/pulse_adapter.js"),M=require("../frontend/src/pulse/pulse_model.js");
const dir=path.join(__dirname,"../docs/contracts/fixtures");
const parts=Object.fromEntries(W.PARTS.map(p=>[p,JSON.parse(fs.readFileSync(path.join(dir,"wire_classified_all_l2",p+".json"),"utf8"))]));
const logical=JSON.parse(fs.readFileSync(path.join(dir,"snapshot.classified_all_l2.json"),"utf8"));
const assembled=W.assemble(parts).snapshot;
assert.deepEqual(assembled,logical,"all L2 compact parts reconstruct exact logical fixture");
const now=logical.generated_ms+1,adapted=A.adapt(assembled,now),index=new M.Index(adapted);
assert.equal(new Set(M.WINDOWS.map(w=>adapted.rows[0].metrics.open_interest[w].level)).size,3,"OI fixture must exercise distinct horizon bands");
let checked=0;
for(const metric of ["volatility","long_short","funding","funding_change","open_interest"]){
 for(const window of M.WINDOWS){
  const level=adapted.rows[0].metrics[metric][window].level;
  if(["funding","funding_change"].includes(metric)&&window!=="1H"){assert.equal(level,null,metric+window);continue;}
  assert(Number.isInteger(level)&&level>=1&&level<=4,metric+window);
  assert.equal(index.select({metric,window,level},now).ids.length,1);
  assert.equal(index.select({metric,window,level:level%4+1},now).ids.length,0);checked++;
 }
}
console.log(`All-L2 wire: exact Python fixture reconstruction and ${checked} metric/window membership checks pass; fictional policy only`);
