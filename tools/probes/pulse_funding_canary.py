"""ADR-0070 exact funding canaries, local evidence only; no new data allocations."""
import argparse
from dataclasses import asdict
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_cohort_sources import write
from pulse_cohort_bootstrap import Dispatch, WorkerAdmission
from pulse.collectors.binance_transport import fetch
from pulse.collectors.funding_inputs import inputs, history, info_index
from pulse.metrics.funding import settled_report, settled_view, current_delta, native_estimate


def witnesses(report,events,estimate):
    result = {}
    regular = sorted((e for e in events if e.rate_type=="Regular"),key=lambda e:e.time_ms)
    n1,n7 = 24//report.metadata.coindcx_hours,168//report.metadata.coindcx_hours
    for name,count,cumulative in (("last",2,False),("cumulative_1d",n1,True),
            ("cumulative_7d",n7,True),("delta_1d",n1+1,False),("delta_7d",n7+1,False)):
        value = getattr(report,name)
        if value.status!="available":
            result[name] = {"status":value.status,"reason":value.reason}
            continue
        chosen = regular[-count:]
        with localcontext() as context:
            context.prec = 60
            expected = sum((e.rate for e in chosen),Decimal(0)) if cumulative else chosen[-1].rate-chosen[0].rate
        if value.fraction!=expected:
            raise RuntimeError("funding_decimal_witness_mismatch")
        result[name] = {"status":"verified","expected_fraction":str(expected),
                        "operands":[asdict(e) for e in chosen]}
    delta = current_delta(estimate,report,now_ms=report.verified_at_ms or estimate.source_ms)
    if delta.status=="available":
        with localcontext() as context:
            context.prec = 60
            expected = estimate.rate-regular[-1].rate
        if delta.fraction!=expected or delta.classification_status!="unclassified":
            raise RuntimeError("current_delta_witness_mismatch")
    result["current_delta"] = asdict(delta)
    return result


def run(cli,ca_file,sources,output,*,report_sink=None):
    client,identity = connect_staging(cli,approved_plaintext=True)
    dispatcher=Dispatch(client,ca_file)
    admission=WorkerAdmission(dispatcher)
    result = {"status":"running","production_access":False,"deletions":0,
              "funding_data_allocations":0 if report_sink is None else None,
              "report_sink_enabled":report_sink is not None,
              "allocation_evidence":"runtime_owner_manifest" if report_sink is not None else "no_funding_store",
              "calls":[],"pairs":[]}
    def clock():
        seconds,micros = client.time()
        return seconds*1000+micros//1000
    def request(path,params,*,host="https://fapi.binance.com",weight=0,family=False):
        result["pending_request"] = {"path":path,"params":params,"host":host}
        write(output,result)
        body = fetch(path,params,admission,{"history":2 if family else int(weight==0),"weight":weight},
                     ca_file,host=host,max_response=4*1024*1024,decimal_numbers=True)
        feedback = dict(admission.source.last_feedback)
        result["calls"].append({"path":path,"params":params,"host":host,"feedback":feedback,
                                 "history_charge":2 if family else int(weight==0),"weight_charge":weight})
        write(output,result)
        return body,feedback
    try:
        info,info_feedback = request("/fapi/v1/fundingInfo",{},family=True)
        indexed = info_index(info)
        premium,_ = request("/fapi/v1/premiumIndex",{},weight=10)
        if type(premium) is not list or len(premium)>4096:
            raise ValueError("premium_complete_list_required")
        premium_map = {}
        for row in premium:
            if type(row) is not dict or not isinstance(row.get("symbol"),str) or row.get("symbol") in premium_map:
                raise ValueError("premium_identity_or_duplicate")
            premium_map[row["symbol"]] = row
        ticker,_ = request("/market_data/v3/current_prices/futures/rt",{},host="https://public.coindcx.com")
        selected = sources["manifest"]["pairs"][:8]  # base5 plus ARK/BMT/IOST1H
        for pair in selected:
            symbol,pair_id = pair["symbol"],pair["pair_id"]
            instrument,feedback = request("/exchange/v1/derivatives/futures/data/instrument",
                {"pair":pair_id,"margin_currency_short_name":"USDT"},host="https://api.coindcx.com")
            instrument = instrument.get("instrument",instrument)
            meta,estimate = inputs(pair_id,symbol,instrument,ticker,indexed,premium_map[symbol],
                instrument_ms=feedback["provider_date_ms"],info_ms=info_feedback["provider_date_ms"],
                mapping=sources["manifest"]["digest"][:32])
            end = clock(); start = end-8*86400000
            raw,_ = request("/fapi/v1/fundingRate",{"symbol":symbol,"startTime":start,"endTime":end,"limit":512},family=True)
            events = history(raw,pair_id,symbol,start_ms=start,end_ms=end)
            now = clock()
            report = settled_report(events,meta,now_ms=now)
            proof = witnesses(report,events,estimate)
            if report_sink is not None:
                report_sink(report,now,symbol)
            # Retention fixture on this actual verified report, without new Redis data.
            retained = settled_view(report,now_ms=min(now+130000,meta.next_funding_ms-1))
            native = native_estimate(estimate,meta,now_ms=now)
            native["rate"] = asdict(native["rate"])
            result["pairs"].append({"symbol":symbol,"metadata":asdict(meta),"estimate":asdict(estimate),
                "report":asdict(report),"current_delta":asdict(current_delta(estimate,report,now_ms=now)),
                "native":native,"witness":proof,
                "retained_after_metadata_freshness":asdict(retained),"raw_history":raw,
                "raw_instrument":instrument,"raw_info":indexed.get(symbol),"raw_premium":premium_map[symbol],
                "raw_ticker":ticker["prices"][pair_id],"ticker_source_ms":ticker["ts"]})
            write(output,result)
            print(json.dumps({"symbol":symbol,"h":meta.coindcx_hours,"verified_at_ms":report.verified_at_ms,
                "fields":{name:getattr(report,name).status for name in ("last","cumulative_1d","cumulative_7d")}}),flush=True)
        live_rows = [{"id":i,"pair_id":p["metadata"]["pair_id"],"h":p["metadata"]["coindcx_hours"],
            "status":p["native"]["rate"]["status"],"reason":p["native"]["rate"]["reason"],
            "nft":p["metadata"]["next_funding_ms"],"fr":p["estimate"]["settled_rate"],
            "efr":p["estimate"]["rate"] if p["native"]["rate"]["status"]=="available" else None,
            "source_ms":p["estimate"]["source_ms"],"expires_ms":min(p["estimate"]["source_ms"]+120000,p["metadata"]["next_funding_ms"])}
            for i,p in enumerate(result["pairs"])]
        generated = clock()
        result.update(status="funding_canary_verified_with_report_sink" if report_sink is not None
                      else "funding_canary_verified_local_only",live_funding_document={
            "schema":"pulse.live_funding.v1","mapping_version":sources["manifest"]["digest"][:32],
            "sequence":generated,"generated_ms":generated,"expires_ms":generated+15000,"rows":live_rows},
            memory=client.info("memory"),transport=identity["transport"])
    except Exception as error:
        result.update(status="stopped",reason=str(error) if isinstance(error,(RuntimeError,ValueError)) else type(error).__name__,
                      admission_reason=getattr(error,"admission_reason",None),failure_feedback=admission.source.last_feedback)
    finally:
        result.update(source_dispatches=dispatcher.calls,status_counts=dispatcher.statuses,
                      admission_denials=dispatcher.denials)
        write(output,result)
        client.close()
    return result


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--ca-file",required=True)
    parser.add_argument("--sources",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.parent!=ROOT/"docs/evidence" or output.exists(): raise SystemExit("invalid_evidence_path")
    sources=json.loads(Path(args.sources).read_text())
    if sources.get("status")!="manifest_verified": raise SystemExit("verified_manifest_required")
    result=run(args.vercel_cli,args.ca_file,sources,output)
    print(json.dumps({"status":result["status"],"reason":result.get("reason"),"pairs":len(result["pairs"])}))
    if result["status"]=="stopped": raise SystemExit(1)
