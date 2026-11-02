"""ADR-0074/75 bounded operator proof; exact staging keys, no deletes or deploy."""
import argparse
from dataclasses import asdict, replace
from decimal import Decimal, Context, localcontext, ROUND_HALF_EVEN
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_cohort_sources import write
from pulse_cohort_bootstrap import Dispatch, WorkerAdmission
from pulse_funding_canary import run as funding_canary
from pulse.collectors.binance_transport import fetch
from pulse.collectors.price_references import collect
from pulse.metrics.price_change import PriceTick, HORIZONS, fallback_change
from pulse.metrics.price_precision import prepare_price
from pulse.store.runtime_document import RuntimeDocument, document
from pulse.store.funding_cache import FundingCache, encode


def run(cli,ca_file,sources,output,funding_output,*,run_id):
    client,identity=connect_staging(cli,approved_plaintext=True)
    result={"status":"running","production_access":False,"deletions":0,
            "run_id":run_id,"keys":[],"funding":[],"references":[],"reference_sources":[],"checks":[],
            "transport":identity["transport"],"synthetic_id":uuid.uuid4().hex}
    def clock():
        seconds,micros=client.time(); return seconds*1000+micros//1000
    def store(channel,mapping,*,synthetic=False):
        value=RuntimeDocument(client,result["synthetic_id"] if synthetic else run_id,channel,mapping)
        result["keys"].extend(value._keys)
        write(output,result)  # Exact manifest durable before any allocation.
        value.acquire()
        return value
    def check(name,condition):
        if not condition: raise RuntimeError("runtime_proof_failed:"+name)
        result["checks"].append(name); write(output,result)
    try:
        result["memory_before"]=client.info("memory")
        synthetic=store("atomic:proof","synthetic",synthetic=True)
        now=clock()
        first=document({"schema":"synthetic","value":"0.0000"},source_ms=now,expires_ms=now+120000)
        check("initial_publish",synthetic.publish(first,expected_revision=0)==[b"published",b"1"])
        check("duplicate_no_revision",synthetic.publish(first,expected_revision=1)==[b"duplicate",b"1"])
        changed=document({"schema":"synthetic","value":"1"},source_ms=now+1,expires_ms=now+120000)
        check("stale_revision",synthetic.publish(changed,expected_revision=0)==[b"revision_conflict"])
        same=document({"schema":"synthetic","value":"1"},source_ms=now,expires_ms=now+120000)
        check("same_source_conflict",synthetic.publish(same,expected_revision=1)==[b"same_source_conflict"])
        earlier=document({},source_ms=now-1,expires_ms=now+120000)
        check("source_regression",synthetic.publish(earlier,expected_revision=1)==[b"source_regression"])
        future=document({},source_ms=now+10**12,expires_ms=now+10**12+1)
        check("future_refused",synthetic.publish(future,expected_revision=1)==[b"invalid_time"])
        elapsed=document({},source_ms=now-2,expires_ms=now-1)
        check("expired_refused",synthetic.publish(elapsed,expected_revision=1)==[b"invalid_time"])
        owner=synthetic.owner; synthetic.owner=b"0:old"
        check("stale_owner_refused",synthetic.publish(changed,expected_revision=1)==[b"fenced_out"])
        synthetic.owner=owner
        synthetic.failed("source_timeout")
        before=synthetic.read(now_ms=clock())
        check("failure_retains_payload",before["payload"]==first.payload and before["revision"]==1)
        check("failure_does_not_freshen",before["source_ms"]==now and before["expires_ms"]==first.expires_ms)
        # Real expiry/handoff; do not delete a lease to manufacture a success.
        time.sleep(31)
        second=RuntimeDocument(client,result["synthetic_id"],"atomic:proof","synthetic")
        second.acquire()
        check("expired_owner_still_refused",synthetic.publish(changed,expected_revision=1)==[b"fenced_out"])
        updated=document({"schema":"synthetic","value":"2"},source_ms=clock(),expires_ms=clock()+60000)
        check("handoff_publish",second.publish(updated,expected_revision=1)==[b"published",b"2"])
        check("previous_generation_preserved",client.hget(second.data,"previous_payload")==first.payload)
        mapping=sources["manifest"]["digest"][:32]
        def sink(report,now,symbol):
            record=store("funding:"+symbol.lower(),mapping)
            cache=FundingCache(record,report.metadata.pair_id,mapping)
            previous=record.read(now_ms=now)
            outcome=cache.refresh(report,expected_revision=previous["revision"])
            item={"symbol":symbol,"publish":outcome,"verified_at_ms":report.verified_at_ms}
            if outcome[0] not in (b"published",b"duplicate",b"retained_last_good"):
                raise RuntimeError("funding_cache_publication_refused")
            if report.verified_at_ms is not None:
                loaded=cache.read(now_ms=now)
                check(symbol+"_exact_roundtrip",loaded["status"]=="available" and loaded["report"]==report)
                cache.refresh(replace(report,verified_at_ms=None),expected_revision=loaded["revision"])
                later=cache.read(now_ms=min(now+130000,report.metadata.next_funding_ms-1))
                check(symbol+"_failed_refresh_retained",later["status"]=="available" and later["report"]==report)
                due=report.latest.time_ms+report.metadata.coindcx_hours*3600000
                pending=cache.read(now_ms=due)
                check(symbol+"_due_pending",pending["status"]=="available" and pending["report"].last.status!="available")
                check(symbol+"_r6_expired",cache.read(now_ms=due+900000)["reason"]=="document_expired")
                cache.refresh(report,expected_revision=loaded["revision"])
                check(symbol+"_successful_retry_clears_failure",record.read(now_ms=clock())["failure"]=="none")
                payload=encode(report).payload
                costs=[]
                for _ in range(100):
                    start=time.perf_counter_ns(); encode(report); costs.append(time.perf_counter_ns()-start)
                item.update(payload_bytes=len(payload),gzip6_bytes=len(gzip.compress(payload,compresslevel=6,mtime=0)),
                            encode_p95_us=sorted(costs)[94]/1000)
            result["funding"].append(item); write(output,result)
        proof=funding_canary(cli,ca_file,sources,funding_output,report_sink=sink)
        check("funding_source_cycle",proof["status"]=="funding_canary_verified_with_report_sink")
        dispatch=Dispatch(client,ca_file)
        admission=WorkerAdmission(dispatch)
        def request(path,params,*,host):
            body=fetch(path,params,admission,{"history":1,"weight":0},ca_file,host=host,decimal_numbers=True)
            if path=="/market_data/candlesticks":
                result["reference_sources"].append({"params":params,"payload":body})
            return body
        ticker=request("/market_data/v3/current_prices/futures/rt",{},host="https://public.coindcx.com")
        for pair in sources["manifest"]["pairs"][:5]:
            row=ticker["prices"][pair["pair_id"]]
            prepared=prepare_price(str(row["ls"]))
            if not prepared.available: raise ValueError("reference_numerator_precision")
            tick=PriceTick(pair["pair_id"],row["btST"],prepared.value)
            now=clock()
            view=collect(tick,request,now_ms=now,mapping_version=mapping,generation_id=str(now))
            fractions={h:asdict(fallback_change(view["fallback"],pair_id=tick.pair_id,horizon=h,
                mapping_version=mapping,definition_version="price_change.v1",generation_id=str(now),
                expires_at_ms=tick.source_time_ms+900000,now_ms=now)) for h in HORIZONS}
            # Reference adapter is checked against selected source opens, not another venue's ticker.
            for horizon,candle in zip(HORIZONS,view["fallback"].references):
                metric=fractions[horizon]
                if candle is None: check(pair["symbol"]+"_"+horizon+"_missing",metric["status"]=="unavailable")
                else:
                    exact=(Fraction(tick.price)-Fraction(candle.price))/Fraction(candle.price)
                    with localcontext(Context(prec=34,rounding=ROUND_HALF_EVEN)):
                        expected=Decimal(exact.numerator)/Decimal(exact.denominator)
                    check(pair["symbol"]+"_"+horizon+"_fraction",metric["value_fraction"]==expected)
            result["references"].append({"symbol":pair["symbol"],"tick":asdict(tick),
                "raw_ticker":row,
                "view":asdict(view["ladder"]),"fallback":asdict(view["fallback"]),"changes":fractions,
                "requests":view["requests"],"selected_open_count":view["selected_open_count"]})
            write(output,result)
        result.update(status="runtime_documents_canaries_verified_not_live_loop",reference_dispatches=dispatch.calls,
                      reference_status_counts=dispatch.statuses,memory_after=client.info("memory"))
    except Exception as error:
        result.update(status="stopped",reason=str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__)
    finally:
        write(output,result); client.close()
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("vercel-cli","ca-file","sources","output","funding-output","run-id"):
        parser.add_argument("--"+name,required=True)
    args=parser.parse_args()
    output,funding_output=Path(args.output).resolve(),Path(args.funding_output).resolve()
    if output==funding_output or any(p.parent!=ROOT/"docs/evidence" or p.exists() for p in (output,funding_output)):
        raise SystemExit("invalid_evidence_path")
    sources=json.loads(Path(args.sources).read_text())
    if sources.get("status")!="manifest_verified": raise SystemExit("verified_manifest_required")
    outcome=run(args.vercel_cli,args.ca_file,sources,output,funding_output,run_id=args.run_id)
    print(json.dumps({"status":outcome["status"],"reason":outcome.get("reason"),"checks":len(outcome["checks"])}))
    if outcome["status"]=="stopped": raise SystemExit(1)
