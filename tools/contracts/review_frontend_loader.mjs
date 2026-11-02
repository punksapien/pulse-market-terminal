// Read-only external consumer review. No frontend files or network endpoints changed.
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {webcrypto,createHash} from 'node:crypto';
import {gzipSync,brotliCompressSync,constants} from 'node:zlib';
const require=createRequire(import.meta.url);
if(!process.argv[2]) throw Error('Pass frontend worktree explicitly');
const root=process.argv[2];
const W=require(resolve(root,'frontend/src/pulse/pulse_wire.js'));
const Loader=require(resolve(root,'frontend/src/pulse/pulse_loader.js'));
const golden=Object.fromEntries(W.PARTS.map(n=>[n,JSON.parse(readFileSync(new URL(`../../docs/contracts/fixtures/wire/${n}.json`,import.meta.url),'utf8'))]));
const digest=v=>W.digest(v,webcrypto.subtle);
async function generation(label,offset=0) {
  const parts=structuredClone(golden);
  for(const doc of Object.values(parts)) {
    doc.generated_ms+=offset;
    if(doc.header) doc.header.generated_ms+=offset;
  }
  const c=parts.core.columns.find(c=>JSON.stringify(c[0])==='["outlook","description"]');
  c[3]=['c',label];
  const logical=W.assemble(parts).snapshot;
  delete logical.generation;
  for(const row of logical.rows) delete row.references.generation_id;
  const id=await digest(logical);
  for(const doc of Object.values(parts)) {
    doc.generation=id;
    if(doc.header) doc.header.generation=id;
  }
  const echo=parts.references.columns.find(c=>JSON.stringify(c[0])==='["references","generation_id"]');
  echo[3]=['c',id];
  const manifest={schema:'pulse.manifest.v1',mapping_version:parts.core.mapping_version,generation:id,horizon:'1H',
    generated_ms:parts.core.generated_ms,expires_ms:parts.core.expires_ms,parts:{}};
  const bodies=new Map();
  for(const [name,doc] of Object.entries(parts)) {
    const hash=await digest(doc); bodies.set(hash,doc);
    const bytes=Buffer.from(W.canonical(doc),'utf8');
    const variants={};
    for(const [coding,body] of Object.entries({identity:bytes,gzip:gzipSync(bytes,{level:6}),
        br11:brotliCompressSync(bytes,{params:{[constants.BROTLI_PARAM_QUALITY]:11}})})) {
      variants[coding]={sha256:'sha256:'+createHash('sha256').update(body).digest('hex'),bytes:body.length,
        encoding:coding==='br11'?'br':coding};
    }
    manifest.parts[name]={schema:'pulse.artifact.v1',logical_sha256:hash,raw_bytes:bytes.length,
      mapping_version:doc.mapping_version,generation:id,variants};
  }
  return {manifest,bodies};
}
const pause=()=>new Promise(r=>setTimeout(r,2));
async function until(test,label) {
  for(let i=0;i<1000;i++) {if(test()) return; await pause();}
  throw Error('review_timeout:'+label);
}
function rig(handler) {
  const timers=new Map(),calls=[]; let next=1;
  const loader=Loader.create({wire:W,digest,now:()=>golden.core.generated_ms+1000,random:()=>.5,
    fetch:async(url,init)=>{
      calls.push({url,headers:init.headers});
      const r=await handler(new URL(url,'http://fixture'),init);
      return {status:r.status,headers:{get:k=>k==='etag'?r.etag:null},text:async()=>JSON.stringify(r.body)};
    },setTimer:(fn,ms,kind)=>{const id=next++;timers.set(id,{fn,ms,kind});return id;},clearTimer:id=>timers.delete(id),
    snapshotUrl:h=>'/api/pulse?horizon='+h,liveUrl:()=>'/api/pulse?live=1',artifactUrl:h=>'/api/pulse?artifact='+encodeURIComponent(h)});
  return {loader,calls,fire:()=>{
    const t=[...timers].find(([,v])=>v.kind==='snapshot');
    if(!t) throw Error('snapshot_timer_missing'); timers.delete(t[0]);t[1].fn();
  }};
}
const g1=await generation('Consumer recovery fixture one');
const g2=await generation('Consumer recovery fixture two',1000);
const output=[];

// A failed G2 artifact must heal without waiting for an unrelated G3 publication.
{
  let current=g1,missing=false;
  const r=rig(async(u,init)=>{
    if(u.searchParams.has('horizon')) {
      const tag='"'+current.manifest.generation+'"';
      return init.headers['If-None-Match']===tag?{status:304}:{status:200,body:current.manifest,etag:tag};
    }
    const hash=u.searchParams.get('artifact');
    if(hash) return missing&&hash===g2.manifest.parts.oi.logical_sha256?{status:404}:{status:200,body:current.bodies.get(hash)};
    return {status:503};
  });
  r.loader.start('1H',['oi']);
  await until(()=>r.loader.state.generation===g1.manifest.generation,'initial');
  current=g2;missing=true;r.fire();
  await until(()=>r.loader.state.snapshotError==='http_404','missing');
  missing=false;const before=r.calls.length;r.fire();
  await until(()=>r.calls.length>before,'304-request');
  for(let i=0;i<5;i++) await pause();
  output.push({case:'retry_after_partial_generation_failure',expected:g2.manifest.generation,
    actual:r.loader.state.generation,recovered:r.loader.state.generation===g2.manifest.generation,
    retained_error:r.loader.state.snapshotError,
    retry_sent_candidate_etag:r.calls.some(c=>c.headers['If-None-Match']==='"'+g2.manifest.generation+'"')});
  r.loader.stop();
}

// A late old-generation lazy-part request must not overwrite a newer generation.
{
  let current=g1,release;
  const held=new Promise(r=>{release=r;}); let waiting=false;
  const r=rig(async(u)=>{
    if(u.searchParams.has('horizon')) return {status:200,body:current.manifest,etag:'"'+current.manifest.generation+'"'};
    const hash=u.searchParams.get('artifact');
    if(hash===g1.manifest.parts.ls.logical_sha256) {waiting=true;await held;return {status:200,body:g1.bodies.get(hash)};}
    if(hash) return {status:200,body:current.bodies.get(hash)};
    return {status:503};
  });
  r.loader.start('1H',['oi']);
  await until(()=>r.loader.state.generation===g1.manifest.generation,'initial-race');
  const old=r.loader.ensureParts(['ls']);
  await until(()=>waiting,'held-ls');
  current=g2;r.loader.wake();
  await until(()=>r.loader.state.generation===g2.manifest.generation,'new-generation');
  release();await old;
  output.push({case:'late_lazy_part_generation_regression',expected:g2.manifest.generation,
    actual:r.loader.state.generation,kept_new_generation:r.loader.state.generation===g2.manifest.generation});
  r.loader.stop();
}
console.log(JSON.stringify({frontend:root,results:output},null,2));
if(output.some(c=>c.recovered===false||c.kept_new_generation===false)) process.exitCode=1;
