"""Reproducible synthetic 504-row size/round-trip benchmark, no network."""
import gzip
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse.wire.compact import encode, decode, raw, digest
from wire_spec import varied_snapshot
from semantics import validate_snapshot


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--independent-versions",action="store_true")
    args=parser.parse_args()
    source=varied_snapshot(independent_versions=args.independent_versions)
    validate_snapshot(source)
    started=time.perf_counter(); documents=encode(source)
    encode_ms=(time.perf_counter()-started)*1000
    started=time.perf_counter(); restored=decode(documents)
    decode_ms=(time.perf_counter()-started)*1000
    assert raw(source)==raw(restored)
    report={"corpus":"synthetic-not-market-seed81","rows":504,"logical_sha256":digest(source),
            "logical_raw":len(raw(source)),"encode_ms":encode_ms,"decode_ms":decode_ms,"parts":{}}
    import subprocess
    # Node's bundled Brotli implementation: no dependency or runtime installation.
    script="""const z=require('node:zlib'), fs=require('node:fs');
const b=fs.readFileSync(0), result={}; for(const q of [5,11]) {
const t=performance.now(); const c=z.brotliCompressSync(b,{params:{[z.constants.BROTLI_PARAM_QUALITY]:q}});
if(!z.brotliDecompressSync(c).equals(b)) throw Error('roundtrip');
result['br'+q]={bytes:c.length,encode_ms:performance.now()-t}; }
process.stdout.write(JSON.stringify(result));"""
    for name,doc in documents.items():
        data=raw(doc); packed=gzip.compress(data,compresslevel=6,mtime=0)
        assert gzip.decompress(packed)==data
        sizes=json.loads(subprocess.check_output(["node","-e",script],input=data))
        report["parts"][name]={"raw":len(data),"gzip6":len(packed),**sizes}
    for codec in ("gzip6","br5","br11"):
        def size(name):
            v=report["parts"][name][codec]
            return v if isinstance(v,int) else v["bytes"]
        report[codec+"_largest_core_plus_metric"]=size("core")+max(size(k) for k in ("oi","ls","funding","funding_change","volatility"))
        report[codec+"_with_references"]=report[codec+"_largest_core_plus_metric"]+size("references")
    report["source_versions"]="all-independent-stress" if args.independent_versions else "materialised-source-batch-per-group-required"
    output=json.dumps(report,indent=2)+"\n"
    if args.output: Path(args.output).write_text(output)
    print(output)


if __name__=="__main__": main()
