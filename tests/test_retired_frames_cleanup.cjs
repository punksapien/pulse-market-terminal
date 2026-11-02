'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs');
const {validateManifest,execute,CHECK_UNLINK,MANIFEST_SHA,PREFIX,memory}=require('../tools/probes/retired_frames_cleanup.cjs');
const bytes=fs.readFileSync('docs/evidence/retired_unselected_frames_audit_2026-10-29.json');
const rows=JSON.parse(fs.readFileSync('docs/fixtures/release_top350_2026-10-28.json')).universe;
const m=validateManifest(bytes,rows);assert.equal(m.entries.length,404);
assert.throws(()=>validateManifest(Buffer.concat([bytes,Buffer.from(' ')]),rows),/digest/);
assert.throws(()=>validateManifest(bytes,rows.slice(1)),/membership/);
assert.throws(()=>validateManifest(bytes,rows.map((r,i)=>i? r:{...r,pair_id:m.entries[0].pair_id})),/membership/);
assert.equal(MANIFEST_SHA.length,64);
assert.ok(m.entries.every(e=>e.key.startsWith(PREFIX)&&!rows.some(r=>r.pair_id===e.pair_id)));
assert.ok(CHECK_UNLINK.indexOf('redis.sha1hex')<CHECK_UNLINK.indexOf("redis.call('UNLINK'"));
assert.ok(CHECK_UNLINK.includes("redis.call('EXISTS',KEYS[2])~=0"));
assert.ok(CHECK_UNLINK.includes('n>32768'));
for(const forbidden of ["'SCAN'","'KEYS'","'FLUSHDB'","'DEL'"])assert.ok(!CHECK_UNLINK.includes(forbidden));
assert.deepEqual(memory('used_memory:123\r\nmem_fragmentation_ratio:1.2\r\nsecret:bad\r\n'),{used_memory:123,mem_fragmentation_ratio:1.2});
(async()=>{
  for(const refused of ['changed_refused','active_lease_refused','retirement_refused','size_or_ttl_refused','type_refused']){
    const modes=[],c={eval:async(_,{arguments:a})=>{modes.push(a[3]);return modes.length===3?[refused]:['verified','20']}};
    const receipt={verified:0,deleted:[],max_lua_us:0};
    await assert.rejects(execute(c,m,true,receipt),new RegExp(refused));
    assert.equal(receipt.deleted.length,0);assert.ok(modes.every(x=>x==='check'));
  }
  for(const apply of [false,true]){
    let calls=0;const receipt={verified:0,deleted:[],max_lua_us:0};
    const c={eval:async(script,{keys,arguments:a})=>{
      assert.equal(script,CHECK_UNLINK);assert.equal(keys.length,8);assert.equal(keys[1],keys[0].replace(/frame$/,'lease'));
      assert.equal(a[2],keys[0]);assert.ok(+a[1]<=32768);
      if(a[3]==='apply')assert.ok(calls>=404);calls++;return [a[3]==='apply'?'deleted':'verified','70'];
    }};
    await execute(c,m,apply,receipt);assert.equal(receipt.verified,404);assert.equal(receipt.deleted.length,apply?404:0);assert.equal(receipt.max_lua_us,70);
  }
  let calls=0;const receipt={verified:0,deleted:[],max_lua_us:0};
  await assert.rejects(execute({eval:async()=>{calls++;return calls===407?['changed_refused']: [calls<=404?'verified':'deleted','10']}},m,true,receipt),/changed_refused/);
  assert.equal(calls,407);assert.equal(receipt.deleted.length,2);
  console.log('retired frame cleanup: manifest, membership, atomic guards and stop rules pass');
})().catch(e=>{console.error(e);process.exitCode=1});
