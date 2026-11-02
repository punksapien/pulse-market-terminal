"""ADR-0111 bounded research only: verified archives -> proposed L2 report, never live policy."""
import argparse
import bisect
from concurrent.futures import ThreadPoolExecutor,as_completed
import csv
from datetime import datetime,timedelta,timezone
from decimal import Decimal,DecimalException,localcontext
import hashlib
import io
import json
from pathlib import Path
import ssl
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request,build_opener,HTTPSHandler,HTTPRedirectHandler
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse.collectors.preview_cohort import PAIRS

DAY=86400000
START=int(datetime(2026,9,1,tzinfo=timezone.utc).timestamp()*1000)
TRAIN_END=START+20*DAY
HOLD_START=START+21*DAY
END=START+30*DAY

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

class Archive:
    def __init__(self,ca_file,cache,max_seconds,manifest=None):
        context=ssl.create_default_context()
        if ca_file: context.load_verify_locations(cafile=ca_file)
        self.context=context;self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.deadline=time.monotonic()+max_seconds;self.lock=threading.Lock();self.stop=threading.Event()
        self.calls=0;self.bytes=0;self.last=0;self.evidence=[]
        self.manifest=manifest

    def get(self,url,limit):
        with self.lock:
            if self.stop.is_set() or time.monotonic()>=self.deadline or self.calls>=3200 or self.bytes>=150000000:
                raise RuntimeError("archive_budget_stop")
            delay=max(0,self.last+1/16-time.monotonic())
            if delay: time.sleep(delay)
            self.last=time.monotonic();self.calls+=1
        opener=build_opener(NoRedirect,HTTPSHandler(context=self.context))
        try:
            with opener.open(Request(url,headers={"User-Agent":"CoinDCX-Pulse-Research/0.1"}),timeout=8) as response:
                data=response.read(limit+1)
        except HTTPError as error:
            if error.code in (403,418,429,451): self.stop.set();raise RuntimeError("archive_provider_stop") from None
            if error.code==404: return None
            raise RuntimeError("archive_http_failure") from None
        if len(data)>limit: self.stop.set();raise ValueError("archive_response_bound")
        with self.lock:
            self.bytes+=len(data)
            if self.bytes>150000000:self.stop.set();raise RuntimeError("archive_byte_budget")
        return data

    def rows(self,symbol,kind,date):
        period="monthly" if kind=="fundingRate" else "daily"
        name=f"{symbol}-{kind}-{date}.zip"
        url=f"https://data.binance.vision/data/futures/um/{period}/{kind}/{symbol}/{name}"
        if self.manifest is not None:
            if name not in self.manifest:return None
            parts=[self.manifest[name],name]
        else:
            check=self.get(url+".CHECKSUM",1024)
            if check is None:return None
            parts=check.decode("ascii").split()
        if len(parts)!=2 or len(parts[0])!=64 or parts[1]!=name: raise ValueError("archive_checksum_shape")
        target=self.cache/name
        if self.manifest is not None and (not target.exists() or target.stat().st_size>2000000):return None
        data=target.read_bytes() if target.exists() and target.stat().st_size<=2000000 else self.get(url,2000000)
        if data is None:return None
        actual=hashlib.sha256(data).hexdigest()
        if actual!=parts[0]: raise ValueError("archive_checksum_mismatch")
        target.write_bytes(data)
        with self.lock:self.evidence.append({"file":name,"sha256":actual,"bytes":len(data)})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info=archive.infolist()
            if len(info)!=1 or info[0].file_size>2000000 or not info[0].filename.endswith(".csv"):
                raise ValueError("archive_zip_bound")
            body=archive.read(info[0])
        rows=list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        if len(rows)>10000:raise ValueError("archive_row_bound")
        return rows

def decimal(value):
    result=Decimal(value)
    if not result.is_finite() or abs(result)>Decimal("1e30"):raise ValueError("archive_decimal")
    return result

def observations(metrics,funding,symbol,diagnostics=None):
    diagnostics={} if diagnostics is None else diagnostics
    series={key:[] for key in ("long_short","funding","funding_change","oi_1h","oi_4h","oi_24h")}
    unique={}
    for row in metrics:
        if row["symbol"]!=symbol:raise ValueError("archive_symbol")
        t=int(datetime.strptime(row["create_time"],"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()*1000)
        if not START<=t<END:continue
        if t%300000:raise ValueError("archive_bucket")
        values=[]
        for column,positive in (("sum_open_interest",False),("count_long_short_ratio",True)):
            try:
                number=decimal(row[column])
                if number<0 or (positive and number==0):raise ValueError("archive_nonpositive")
            except (ValueError,KeyError,DecimalException):
                diagnostics[column+"_unavailable"]=diagnostics.get(column+"_unavailable",0)+1
                number=None
            values.append(number)
        value=tuple(values)
        if t in unique and unique[t]!=value:raise ValueError("archive_conflict")
        unique[t]=value
    times=sorted(unique)
    for t in times:
        oi,ratio=unique[t]
        if ratio is not None:
            with localcontext() as ctx:
                ctx.prec=34
                series["long_short"].append((t,float(abs(ratio.ln())),300000))
        if oi is None:continue
        for key,window in (("oi_1h",3600000),("oi_4h",14400000),("oi_24h",86400000)):
            at=bisect.bisect_right(times,t-window)-1
            if at<0 or t-window-times[at]>300000:continue
            baseline=unique[times[at]][0]
            if baseline is not None and baseline>0:series[key].append((t,float(abs((oi-baseline)/baseline)),300000))
    events={}
    for row in funding:
        t=int(row["calc_time"]);h=int(row["funding_interval_hours"]);rate=decimal(row["last_funding_rate"])
        if not START<=t<END:continue
        if h not in (1,4,8):raise ValueError("archive_funding_interval")
        if t in events and events[t]!=(h,rate):raise ValueError("archive_funding_conflict")
        events[t]=(h,rate)
    prior=None
    for t,(h,rate) in sorted(events.items()):
        series["funding"].append((t,float(abs(rate*8/h-Decimal("0.0001"))*10000),h*3600000))
        if prior and prior[1]==h and abs(t-prior[0]-h*3600000)<=5000:
            series["funding_change"].append((t,float(abs((rate-prior[2])*10000*8/h)),h*3600000))
        prior=(t,h,rate)
    return series

def quantiles(values):
    ordered=sorted(values)
    return [ordered[min(len(ordered)-1,int(q*(len(ordered)-1)))] for q in (.5,.8,.95)] if ordered else None

def occupancy(rows,cuts):
    count=[0]*4;churn=edges=0;last=None
    for t,value,step in rows:
        band=bisect.bisect_right(cuts,value);count[band]+=1
        if last and abs(t-last[0]-step)<=5000:
            edges+=1;churn+=band!=last[1]
        last=(t,band)
    return {"count":len(rows),"bands":count,"contiguous_edges":edges,"band_changes":churn}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca-file");parser.add_argument("--cache-dir",required=True)
    parser.add_argument("--out",required=True);parser.add_argument("--max-seconds",type=int,default=600)
    parser.add_argument("--offline-manifest");parser.add_argument("--manifest-sha256")
    args=parser.parse_args()
    if not 1<=args.max_seconds<=600:parser.error("max-seconds must be1..600")
    manifest=None
    if args.offline_manifest:
        raw=Path(args.offline_manifest).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=args.manifest_sha256:raise ValueError("offline_manifest_digest")
        manifest={r["file"]:r["sha256"] for r in json.loads(raw)["files"]}
    store=Archive(args.ca_file,args.cache_dir,args.max_seconds,manifest);results={};excluded={}
    def pair(meta):
        symbol=meta["symbol"];metrics=[];days=0;missing=[]
        for i in range(30):
            date=(datetime(2026,9,1)+timedelta(days=i)).strftime("%Y-%m-%d")
            try:rows=store.rows(symbol,"metrics",date)
            except (OSError,ValueError,zipfile.BadZipFile):rows=None
            if rows is None:missing.append(date)
            else:metrics.extend(rows);days+=1
        funding=store.rows(symbol,"fundingRate","2026-09")
        diagnostics={};series=observations(metrics,funding or [],symbol,diagnostics)
        return {"days":days,"missing_or_invalid_days":missing,"funding_file":funding is not None,
                "field_rejections":diagnostics,"series":series}
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs={pool.submit(pair,m):m["symbol"] for m in PAIRS}
        for future in as_completed(jobs):
            symbol=jobs[future]
            try:results[symbol]=future.result();print(json.dumps({"pair":symbol,"days":results[symbol]["days"]}),flush=True)
            except Exception as error:excluded[symbol]=type(error).__name__
    anchors={"long_short":[float(Decimal(x).ln()) for x in ("1.5","1.8","3")],"funding":[2,4,9],"funding_change":[1,5,10]}
    report={"status":"proposal_only_not_activated","adr":"ADR-0111","period":"2026-09-01/2026-09-30",
            "split_days":{"train":20,"purge":1,"holdout":9},"calls":store.calls,"compressed_bytes":store.bytes,
            "pairs_requested":len(PAIRS),"pairs_processed":len(results),"excluded":excluded,"metrics":{},
            "offline_manifest_sha256":args.manifest_sha256 if args.offline_manifest else None,
            "coverage":{s:{k:v for k,v in r.items() if k!="series"} for s,r in results.items()},
            "limitations":["single older market regime","settled funding is a proxy for estimates","5min OI proxy, not own60s",
                            "archive/API timestamp mapping not independently reverified here","event time, not historical arrival times",
                            "pooled quantiles can overweight pairs with denser funding schedules; compare anchors and interval strata before approval"],
            "files":sorted(store.evidence,key=lambda e:e["file"])}
    for metric in ("long_short","funding","funding_change","oi_1h","oi_4h","oi_24h"):
        train=[v for r in results.values() for t,v,step in r["series"][metric] if t<TRAIN_END]
        policies={"training_quantiles":quantiles(train)}
        if metric in anchors:policies["research_anchors"]=anchors[metric]
        entry={"train_observations":len(train),"candidates":{}}
        for name,cuts in policies.items():
            if cuts is None:continue
            out={"cuts":cuts,"strictly_increasing":all(a<b for a,b in zip(cuts,cuts[1:])),
                 "positive_cuts":all(x>0 for x in cuts),"train":[],"holdout":[]}
            for symbol,r in sorted(results.items()):
                for split in ("train","holdout"):
                    rows=[point for point in r["series"][metric] if (point[0]<TRAIN_END if split=="train" else point[0]>=HOLD_START)]
                    out[split].append({"symbol":symbol,**occupancy(rows,cuts)})
            entry["candidates"][name]=out
        report["metrics"][metric]=entry
    Path(args.out).write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False)+"\n")
    print(json.dumps({"report":args.out,"pairs":len(results),"excluded":len(excluded),"calls":store.calls,"bytes":store.bytes}),flush=True)

if __name__=="__main__":main()
