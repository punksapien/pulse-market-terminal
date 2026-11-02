// ADR-0071: worker-only codec measurement of the exact real canary document.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {performance} from 'node:perf_hooks';
import {brotliCompressSync, brotliDecompressSync, gzipSync, gunzipSync, constants} from 'node:zlib';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const [source, output]=process.argv.slice(2).map(p=>path.resolve(p));
if (path.dirname(output)!==path.join(root,'docs/evidence') || fs.existsSync(output)) throw Error('invalid evidence path');
const record=JSON.parse(fs.readFileSync(source,'utf8'));
if(record.status!=='funding_canary_verified_local_only') throw Error('real verified funding document required');
const bytes=Buffer.from(JSON.stringify(record.live_funding_document));
const result={source_sha256:createHash('sha256').update(bytes).digest('hex'),node:process.version,
  payload:'actual eight-row live-funding canary document, not503pair snapshot',raw_bytes:bytes.length,
  rows:record.live_funding_document.rows.length,iterations:100,exchange_calls:0,redis_access:false};
for(const [name, encode, decode] of [
  ['gzip6',b=>gzipSync(b,{level:6}),gunzipSync],
  ['brotli5',b=>brotliCompressSync(b,{params:{[constants.BROTLI_PARAM_QUALITY]:5}}),brotliDecompressSync]
]) {
  const encoded=encode(bytes);
  if(!decode(encoded).equals(bytes)) throw Error('codec roundtrip mismatch');
  for(let i=0;i<10;i++) encode(bytes);
  const cpuStart=process.cpuUsage(), times=[];
  for(let i=0;i<100;i++) {const start=performance.now();encode(bytes);times.push(performance.now()-start);}
  const cpu=process.cpuUsage(cpuStart);times.sort((a,b)=>a-b);
  result[name]={bytes:encoded.length,ratio:encoded.length/bytes.length,
    p50_ms:times[49],p95_ms:times[94],max_ms:times[99],cpu_ms_per_encode:(cpu.user+cpu.system)/100000,
    roundtrip_exact:true};
}
result.brotli_saving_vs_gzip_fraction=1-result.brotli5.bytes/result.gzip6.bytes;
result.publisher_adopted=false;
fs.writeFileSync(output,JSON.stringify(result,null,2)+'\n');
console.log(JSON.stringify(result));
