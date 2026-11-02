// Read-only cross-team checker. No imports/edits in Review's worktree unless supplied.
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
const require=createRequire(import.meta.url);
if(!process.argv[2]) throw Error('Pass frontend worktree root explicitly');
const {currentDelta}=require(resolve(process.argv[2],'frontend/src/pulse/pulse_live.js'));
const D=require(resolve(process.argv[2],'frontend/src/pulse/pulse_decimal.js'));
const fixture=JSON.parse(readFileSync(new URL('../../docs/contracts/fixtures/current_delta.boundaries.json',import.meta.url),'utf8'));
const failures=[];
for(const c of fixture.cases) {
  const actual=currentDelta(c.envelope,c.live,c.settled,c.now_ms);
  const available=actual.status==='available';
  if(available!==c.expected.available || (!available && actual.value!==null) ||
     (available && D.compare(D.parse(actual.value),D.parse(c.expected.value))!==0))
    failures.push({name:c.name,expected:c.expected,actual});
}
console.log(JSON.stringify({cases:fixture.cases.length,failures},null,2));
if(failures.length) process.exitCode=1;
