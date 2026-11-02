'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),cp=require('node:child_process');
const H=require('../tools/probes/production_handover.cjs');
const rows=JSON.parse(fs.readFileSync('docs/fixtures/release_top350_2026-10-28.json','utf8')).universe;
const list=H.entries(rows);
assert.equal(list.length,2815);assert.equal(new Set(list.map(x=>x.next)).size,list.length);
assert.equal(list.filter(x=>x.old.endsWith(':frame')).length,1400);
assert(list.every(x=>x.old.startsWith('pulse:stg:')&&x.next.startsWith('pulse:prod:')));
assert(!list.some(x=>x.next.includes(H.RAW)));
assert.throws(()=>H.entries(rows.slice(0,349)),/membership/);
const bad=structuredClone(rows);bad[0].symbol='BTCUSDC';assert.throws(()=>H.entries(bad),/membership/);
assert(!/redis\.call\('(SCAN|KEYS|FLUSHDB|FLUSHALL|DEL|UNLINK)'/.test(H.MOVE));
assert(H.MOVE.includes("redis.call('EXISTS',KEYS[2])~=0"));
assert(H.MOVE.includes("redis.call('PTTL',KEYS[3])~=-1"));
assert(H.MOVE.includes("redis.call('EXISTS',KEYS[4])==1"));
// Use the system Lua interpreter when present; structural checks are not claimed as Redis execution.
console.log(JSON.stringify({status:'pass',manifest_keys:list.length,frames:1400,redis_calls:0,
  not_checked:'real Redis rename fault cases run separately before transfer'}));
(async()=>{
  const old=`pulse:stg:{${H.RAW}}:publication:lease`,active=`pulse:stg:{${H.RUN}}:stream:lease`;
  const data=new Map([[old,'existing-retirement']]);let sets=0;
  const c={get:async k=>data.get(k)??null,pTTL:async k=>k===old?2500000000:30000,
    set:async(k,v)=>{if(data.has(k))return null;sets++;data.set(k,v);return 'OK';}};
  data.set(active,'live-owner');await assert.rejects(H.retire(c),/writer_not_drained/);
  assert.equal(sets,0,'all live writers checked before retirement writes');
  data.delete(active);await H.retire(c);
  assert.equal(data.get(old),'existing-retirement','never overwrite the old retired owner');
  assert.equal(sets,11);await H.retire(c);assert.equal(sets,11,'retirement resumes idempotently');
  console.log('retirement: active lease refusal, preserved previous retirement and idempotency pass');
})().catch(e=>{console.error(e);process.exitCode=1;});
