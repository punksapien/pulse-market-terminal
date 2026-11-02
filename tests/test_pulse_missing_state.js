'use strict';
const assert=require('node:assert/strict');
const M=require('../frontend/src/pulse/pulse_model.js');
for(const [reason,kind,label] of [
  ['part_not_loaded','loading','Loading'],['settlement_pending','pending','Pending'],
  ['unsupported','unavailable','N/A'],['insufficient_history','pending','Pending'],
  ['baseline_missing','pending','Pending'],['discarded_baseline','pending','Pending'],
  ['expired','updating','Updating'],['stale_estimate','updating','Updating'],
  ['unknown','unavailable','No data'],[null,'unavailable','No data'],
  ['<script>','unavailable','No data']]){
  const s=M.missingState(reason);assert.equal(s.kind,kind);assert.equal(s.label,label);
  assert(!s.description.includes('<script>'));assert(!s.description.includes('live value'));
}
assert.equal(M.missingState('expired',true).kind,'loading');
assert.equal(M.effective({value:1,status:'available',asof:100,expires_at:200},200),'unavailable');
assert.equal(M.format('volatility',null),'—','presentation must not change the null numeric contract');
assert.equal(M.format('funding',0),'0.00 bps','zero must remain distinct from missing');
console.log('missing-field presentation: clock boundaries, reason taxonomy, loading and null/zero pass');
