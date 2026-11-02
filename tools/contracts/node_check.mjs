// Independent Node canonical-digest parity. No dependencies or frontend writes.
import assert from 'node:assert/strict';
import {readFileSync,readdirSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const folder=fileURLToPath(new URL('../../docs/contracts/fixtures/',import.meta.url));
function canonical(v) {
  if(Array.isArray(v)) return '['+v.map(canonical).join(',')+']';
  if(v!==null && typeof v==='object') return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}';
  return JSON.stringify(v);
}
const digest=v=>'sha256:'+createHash('sha256').update(canonical(v),'utf8').digest('hex');
let checks=0;
for(const file of readdirSync(folder).filter(f=>f.startsWith('snapshot.')&&f.endsWith('.json'))) {
  const s=JSON.parse(readFileSync(folder+file,'utf8'));
  assert.equal(digest(s.universe),s.mapping_version); checks++;
  const identifier=s.generation;
  delete s.generation;
  for(const row of s.rows) {
    assert.equal(row.references.generation_id,identifier); checks++;
    delete row.references.generation_id;
  }
  assert.equal(digest(s),identifier); checks++;
}
console.log(`Independent Node canonical parity: ${checks} checks passed`);
