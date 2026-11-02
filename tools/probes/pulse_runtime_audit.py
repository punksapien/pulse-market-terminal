"""Offline independent reference-witness audit; optional bounded staging reads."""
import argparse
from decimal import Decimal, Context, localcontext, ROUND_HALF_EVEN
from fractions import Fraction
import hashlib
from dataclasses import asdict
import gzip
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_cohort_sources import write


def audit(evidence):
    if evidence.get("status")!="runtime_documents_canaries_verified_not_live_loop":
        raise ValueError("successful_runtime_evidence_required")
    source={}
    for request in evidence["reference_sources"]:
        pair=request["params"]["pair"]
        for row in request["payload"]["data"]:
            key=(pair,row["time"])
            value=Decimal(row["open"])
            if key in source and source[key]!=value: raise ValueError("raw_duplicate_conflict")
            source[key]=value
    windows={"1H":3600000,"4H":14400000,"24H":86400000}
    checks=values=0
    def candle_matches(candle,pair,expected):
        if candle is None: return
        if (candle["pair_id"]!=pair or candle["open_time_ms"]!=expected
                or source.get((pair,expected))!=Decimal(candle["price"])):
            raise ValueError("reference_not_identical_to_raw")
    for row in evidence["references"]:
        tick,ladder=row["tick"],row["view"]
        pair=tick["pair_id"]
        if tick["source_time_ms"]!=row["raw_ticker"]["btST"] or Decimal(tick["price"])!=Decimal(row["raw_ticker"]["ls"]):
            raise ValueError("numerator_not_identical_to_raw")
        for slot,candles in enumerate(ladder["slots"]):
            for window,candle in zip(windows.values(),candles):
                candle_matches(candle,pair,ladder["start_ms"]+slot*60000-window)
                checks+=candle is not None
        for (horizon,window),candle in zip(windows.items(),row["fallback"]["references"]):
            expected_minute=(tick["source_time_ms"]-window)//60000*60000
            candle_matches(candle,pair,expected_minute)
            metric=row["changes"][horizon]
            if candle is None:
                if metric["status"]!="unavailable" or metric["value_fraction"] is not None:
                    raise ValueError("missing_reference_not_null")
                continue
            exact=(Fraction(Decimal(tick["price"]))-Fraction(Decimal(candle["price"])))/Fraction(Decimal(candle["price"]))
            with localcontext(Context(prec=34,rounding=ROUND_HALF_EVEN)):
                expected=Decimal(exact.numerator)/Decimal(exact.denominator)
            if (metric["status"]!="available" or Decimal(metric["value_fraction"])!=expected
                    or metric["reference_asof_ms"]!=expected_minute
                    or metric["numerator_asof_ms"]!=tick["source_time_ms"]):
                raise ValueError("raw_reference_arithmetic_mismatch")
            values+=1
    return {"status":"passed","ladder_raw_open_comparisons":checks,"fallback_fraction_comparisons":values,
            "pairs":len(evidence["references"]),"exchange_calls":0,"redis_commands":0}


def read_benchmark(cli,run_id,mapping):
    from pulse_staging_gate import connect_staging
    from pulse.store.runtime_document import RuntimeDocument
    client,_=connect_staging(cli,approved_plaintext=True)
    try:
        store=RuntimeDocument(client,run_id,"funding:btcusdt",mapping)
        seconds,micros=client.time()
        start_ms=seconds*1000+micros//1000
        monotonic=time.monotonic_ns()
        walls=[]; cpus=[]
        for _ in range(16):
            now=start_ms+(time.monotonic_ns()-monotonic)//1000000
            wall,cpu=time.perf_counter_ns(),time.process_time_ns()
            record=store.read(now_ms=now)
            walls.append((time.perf_counter_ns()-wall)/1e6)
            cpus.append((time.process_time_ns()-cpu)/1e6)
            if record["status"]!="available": raise RuntimeError("benchmark_document_unavailable")
        return {"samples":16,"hmget_commands":16,"time_commands":1,"writes":0,"exchange_calls":0,
                "wall_p95_ms":sorted(walls)[15],"cpu_p95_ms":sorted(cpus)[15],
                "wall_ms":walls,"cpu_ms":cpus,"lua_cpu_claim":False}
    finally:
        client.close()


def reference_benchmark(evidence):
    from pulse.collectors.price_references import collect
    from pulse.metrics.price_change import PriceTick
    responses={json.dumps(v["params"],sort_keys=True):v["payload"] for v in evidence["reference_sources"]}
    def replay(path,params,**kwargs):
        if path!="/market_data/candlesticks": raise ValueError("unexpected_benchmark_source")
        return responses[json.dumps(params,sort_keys=True)]
    results=[]
    for item in evidence["references"]:
        tick=PriceTick(item["tick"]["pair_id"],item["tick"]["source_time_ms"],Decimal(item["tick"]["price"]))
        ladder=item["view"]; times=[]
        for _ in range(100):
            start=time.perf_counter_ns()
            view=collect(tick,replay,now_ms=ladder["built_at_ms"],mapping_version=ladder["mapping_version"],
                         generation_id=ladder["generation_id"])
            times.append((time.perf_counter_ns()-start)/1000)
        raw=json.dumps({"ladder":asdict(view["ladder"]),"fallback":asdict(view["fallback"])},
                       default=str,separators=(",",":")).encode()
        results.append({"symbol":item["symbol"],"iterations":100,"worker_p95_us":sorted(times)[94],
                        "named_json_bytes":len(raw),"gzip6_bytes":len(gzip.compress(raw,6,mtime=0)),
                        "selected_opens":view["selected_open_count"]})
    return {"exchange_calls":0,"redis_commands":0,"rows":results,
            "claim":"offline replay of recorded narrow responses, not network or full-universe performance"}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",required=True); parser.add_argument("--output",required=True)
    parser.add_argument("--vercel-cli"); parser.add_argument("--mapping")
    args=parser.parse_args()
    source=Path(args.input).resolve(); output=Path(args.output).resolve()
    if source.parent!=ROOT/"docs/evidence" or output.parent!=source.parent or output.exists():
        raise SystemExit("invalid_evidence_path")
    raw=source.read_bytes(); evidence=json.loads(raw)
    result=audit(evidence)
    result["reference_worker_benchmark"]=reference_benchmark(evidence)
    result["input_sha256"]=hashlib.sha256(raw).hexdigest()
    if args.vercel_cli:
        result["read_benchmark"]=read_benchmark(args.vercel_cli,evidence["run_id"],args.mapping)
    write(output,result)
    print(json.dumps(result))
