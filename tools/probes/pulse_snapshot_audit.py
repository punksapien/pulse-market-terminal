"""Read-only Preview audit: immutable digests, real field coverage and prebuilt payload sizes."""
import argparse
from collections import Counter
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib,json,re,ssl,sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit,urlencode
from urllib.request import Request,HTTPSHandler,HTTPRedirectHandler,build_opener

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from pulse.wire.compact import decode
from pulse.wire.identity import digest

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def decode_response(body,encoding,maximum=1114112):
    if len(body)>maximum:raise ValueError("audit_response_bound")
    if encoding=="gzip":
        from pulse.wire.source_codec import gunzip
        return gunzip(body,maximum=maximum)
    if encoding=="br":
        import brotli
        decoder=brotli.Decompressor();chunks=[];size=0
        for offset in range(0,len(body),1024):
            part=decoder.process(body[offset:offset+1024]);size+=len(part)
            if size>maximum:raise ValueError("audit_response_bound")
            chunks.append(part)
        if not decoder.is_finished():raise ValueError("audit_encoding")
        return b''.join(chunks)
    if encoding!="identity":raise ValueError("audit_encoding")
    return body

def coverage(fields,generated,response_ms):
    available=[f for f in fields if f.get("status")=="available"]
    fresh=[f for f in available if response_ms is not None and f["expires_ms"]>response_ms]
    return {"rows":len(fields),"available_at_generation":len(available),
        "fresh_at_response":len(fresh) if response_ms is not None else None,
        "reasons":dict(Counter(f.get("reason") or "none" for f in fields if f.get("status")!="available")),
        "max_source_age_at_generation_ms":max((generated-f["asof_ms"] for f in available),default=None),
        "classified_at_generation":sum(f.get("classification_status")=="classified" for f in available)}


def response_clock(headers):
    try:return int(parsedate_to_datetime(headers["Date"]).timestamp()*1000)+int(headers.get("Age","0"))*1000
    except (ValueError,TypeError,KeyError,OverflowError):return None


def unavailable_examples(fields,universe,now_ms):
    result=[]
    for i,field in enumerate(fields):
        if field.get("status")=="available" and now_ms is not None and field["expires_ms"]>now_ms:continue
        result.append(dict(pair=universe[i]["pair_id"],reason=field.get("reason") or
            ("expired_at_response" if field.get("status")=="available" else "source_unavailable")))
        if len(result)==12:break
    return result


def all_fields(rows,generated,response_ms):
    """Audit every canonical metric record, not only the headline subset."""
    found={}
    def walk(value,path,index):
        if not isinstance(value,dict):return
        if {"status","unit","value"}<=value.keys():
            found.setdefault(path,{})[index]=value
            return
        for key,child in value.items():walk(child,path+(key,),index)
    for index,row in enumerate(rows):walk(row,(),index)
    return {".".join(path):coverage([values.get(i,{"status":"unavailable","reason":"not_published_in_row"})
        for i in range(len(rows))],generated,response_ms) for path,values in sorted(found.items())}


def live_returns(snapshot,document,now_ms):
    """Independently evaluate every published live numerator against its pinned ladder."""
    from pulse.metrics.price_change import PriceTick,CandleOpen,ReferenceLadder,price_change,WINDOW_MS
    counts={window:Counter() for window in WINDOW_MS}
    if now_ms is None:return {"status":"response_clock_unavailable"}
    live={r["id"]:r for r in document.get("rows",[])} if document else {}
    for row in snapshot["rows"]:
        point=live.get(row["id"]);refs=row["references"]
        if not point or point.get("status")!="available":
            for count in counts.values():count["numerator_unavailable"]+=1
            continue
        try:
            pair=snapshot["universe"][row["id"]]["pair_id"]
            value=refs["ladder"]
            slots=tuple(tuple(CandleOpen(pair,p["reference_minute_ms"],Decimal(p["open_price"]))
                if p["status"]=="available" else None for p in slot) for slot in value["slots"])
            ladder=ReferenceLadder(pair,refs["mapping_version"],refs["definition_version"],refs["generation_id"],
                value["built_at_ms"],value["start_ms"],slots)
            tick=PriceTick(pair,point["source_ms"],Decimal(point["price"]))
            for window in counts:
                result=price_change(tick,ladder,horizon=window,mapping_version=snapshot["mapping_version"],
                    definition_version="price_change.v1",generation_id=snapshot["generation"],
                    numerator_expires_at_ms=point["expires_ms"],now_ms=now_ms)
                counts[window]["available" if result.status=="available" else result.reason]+=1
        except (KeyError,ValueError,TypeError,ArithmeticError):
            for count in counts.values():count["invalid_published_reference"]+=1
    return {window:dict(count) for window,count in counts.items()}

def main():
    from preview_redis_access import api,PROJECT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True);parser.add_argument("--url",required=True)
    parser.add_argument("--ca-file");parser.add_argument("--out")
    parser.add_argument("--horizon",choices=("1H","4H","24H"),default="24H")
    args=parser.parse_args();url=urlsplit(args.url)
    if (url.scheme!="https" or not url.hostname or not url.hostname.endswith(".vercel.app") or url.username
            or url.password or url.port or url.path not in ("","/") or url.query or url.fragment):raise ValueError("audit_url")
    deployment=api(args.vercel_cli,"/v13/deployments/"+url.hostname)
    if (deployment.get("projectId")!=PROJECT or deployment.get("target")=="production" or deployment.get("readyState")!="READY"
            or deployment.get("meta",{}).get("githubCommitRef") not in ("feat/preview-stream-bridge","fix/preview-detail-data","feat/all-pair-scheduler")):raise ValueError("audit_scope")
    project=api(args.vercel_cli,f"/v9/projects/{PROJECT}")
    bypass=next(k for k,v in project.get("protectionBypass",{}).items() if v.get("scope")=="automation-bypass")
    context=ssl.create_default_context()
    if args.ca_file:context.load_verify_locations(cafile=args.ca_file)
    opener=build_opener(NoRedirect,HTTPSHandler(context=context));origin="https://"+url.hostname
    def get(query):
        request=Request(origin+"/api/pulse?"+urlencode(query),headers={"Accept-Encoding":"br, gzip","x-vercel-protection-bypass":bypass})
        try:response=opener.open(request,timeout=10)
        except HTTPError as error:raise RuntimeError("audit_reader_http_"+str(error.code)) from None
        with response:
            body=response.read(1114113)
            if len(body)>1114112:raise ValueError("audit_response_bound")
            headers=response.headers
            body=decode_response(body,headers.get("Content-Encoding","identity"))
        return body,headers
    raw,headers=get({"horizon":args.horizon});manifest=json.loads(raw)
    if manifest.get("horizon")!=args.horizon:raise ValueError("audit_horizon_mismatch")
    observed=response_clock(headers)
    live_reports={};live_documents={};live_clocks={}
    for name,query in (("live_funding",{"live":"1"}),("live_price",{"live":"price"})):
        try:
            body,live_headers=get(query);live=json.loads(body)
            live_documents[name]=live
            live_clocks[name]=response_clock(live_headers)
            live_reports[name]={"rows":len(live["rows"]),"available":sum(r["status"]=="available" for r in live["rows"]),
                "reasons":dict(Counter(r.get("reason") or "none" for r in live["rows"] if r["status"]!="available"))}
        except RuntimeError as error:live_reports[name]={"status":str(error)}
    parts={}
    for name,descriptor in manifest["parts"].items():
        body,part_headers=get({"artifact":descriptor["logical_sha256"]})
        stamp=response_clock(part_headers)
        if stamp is not None:observed=max(observed or stamp,stamp)
        if "sha256:"+hashlib.sha256(body).hexdigest()!=descriptor["logical_sha256"]:raise ValueError("audit_digest")
        parts[name]=json.loads(body)
    snapshot=decode(parts)
    if digest(snapshot["universe"])!=snapshot["mapping_version"]:raise ValueError("audit_mapping")
    rows=snapshot["rows"];generated=snapshot["generated_ms"]
    targets={"oi_quantity":[r["oi"]["quantity"] for r in rows],"ls_global":[r["ls"]["global_ratio"] for r in rows],
        "funding_native":[r["funding"]["native_estimate"] for r in rows],
        "funding_change_settled":[r["funding_change"]["settled_last"] for r in rows],
        "volatility":[r["volatility"]["score"] for r in rows],"price":[r["price"] for r in rows]}
    targets["ls_top"]=[r["ls"]["top_ratio"] for r in rows]
    for h in ("1h","4h","24h"):targets["oi_"+h]=[r["oi"]["change_"+h] for r in rows]
    for window in ("1H","4H","24H"):
        targets["ls_period_"+window]=[r["ls"].get("period_ratios",{}).get(window,{"status":"unavailable","reason":"period_not_published"}) for r in rows]
    for window in ("1d","7d"):
        targets["funding_"+window]=[r["funding"]["cumulative_"+window] for r in rows]
        targets["funding_change_"+window]=[r["funding_change"]["settled_"+window] for r in rows]
    report={"status":"audited_not_all_fields_passed","runtime":deployment["meta"]["githubCommitSha"],
        "horizon":snapshot["horizon"],
        "generation":snapshot["generation"],"generated_ms":generated,"response_clock_ms":observed,"pairs":len(rows),
        "immutable_digests_verified":len(parts),"exchange_calls":0,
        "period_ratio_witnesses":[{"pair":snapshot["universe"][r["id"]]["pair_id"],
            "ratios":{w:r["ls"].get("period_ratios",{}).get(w,{}).get("value") for w in ("1H","4H","24H")}} for r in rows[:5]],
        "fields":{name:coverage(fields,generated,observed) for name,fields in targets.items()},
        "unavailable_examples":{name:unavailable_examples(fields,snapshot["universe"],observed) for name,fields in targets.items()},
        "all_fields":all_fields(rows,generated,observed),
        "live_returns":live_returns(snapshot,live_documents.get("live_price"),live_clocks.get("live_price")),
        "payload_bytes":{name:{key:d["bytes"] for key,d in part["variants"].items()} for name,part in manifest["parts"].items()}}
    report["references"]={"available_ladders":sum(r["references"]["status"]=="available" for r in rows),
        "complete_ladders":sum(r["references"]["status"]=="available" and all(p["status"]=="available"
            for slot in r["references"]["ladder"]["slots"] for p in slot) for r in rows),
        "without_snapshot_fallback":sum(r["references"]["status"]=="available" and r["references"]["fallback"] is None for r in rows)}
    report.update(live_reports)
    output=json.dumps(report,indent=2,sort_keys=True)+"\n"
    if args.out:Path(args.out).write_text(output)
    print(output)

if __name__=="__main__":main()
