"""ADR-0133 bounded hosted all-pair scheduler verification; Preview only."""
import argparse
import json
import re
import ssl
import subprocess
from concurrent.futures import ThreadPoolExecutor
import time
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,HTTPSHandler
from urllib.error import HTTPError

from preview_redis_access import api,PROJECT
from pulse_preview_drive import NoRedirect,summary

BRANCH="feat/all-pair-scheduler"


def recovery(status,body,consecutive):
    """ADR-0139: retry a transport stop only in a later bounded invocation."""
    if type(consecutive) is not int or not 0<=consecutive<=2:raise ValueError("recovery_count")
    if status==200 and body.get("stopped") in (None,"time_budget"):
        return True,0,0
    if status==200 and body.get("stopped")=="source_transport_failed" and consecutive<2:
        return True,consecutive+1,60
    return False,consecutive,0


def run(cli,url,*,runs,ca_file=None,publish=False,setup_release=False,publish_only=False,cleanup_retired=False):
    if type(runs) is not int or not 1<=runs<=20:raise ValueError("hosted_runs_bound")
    if setup_release and (runs!=1 or publish):raise ValueError("hosted_release_bound")
    if publish_only and (publish or setup_release):raise ValueError("hosted_publish_bound")
    if cleanup_retired and (runs!=1 or publish or publish_only or setup_release):raise ValueError("hosted_cleanup_bound")
    parsed=urlsplit(url)
    if (parsed.scheme!="https" or not parsed.hostname or not parsed.hostname.endswith(".vercel.app")
            or parsed.username or parsed.password or parsed.port or parsed.path not in ("","/")
            or parsed.query or parsed.fragment):raise ValueError("hosted_origin_refused")
    deployed=api(cli,"/v13/deployments/"+parsed.hostname)
    if (deployed.get("projectId")!=PROJECT or deployed.get("target")=="production"
            or deployed.get("readyState")!="READY"
            or deployed.get("meta",{}).get("githubCommitRef")!=BRANCH):
        raise ValueError("hosted_preview_identity_refused")
    envs=api(cli,f"/v10/projects/{PROJECT}/env?target=preview")["envs"]
    entries=[e for e in envs if e.get("key")=="PULSE_WORKER_SECRET"
        and e.get("target")==["preview"] and e.get("gitBranch")==BRANCH]
    if len(entries)!=1:raise ValueError("hosted_secret_scope_refused")
    secret=subprocess.run(["security","find-generic-password","-a","pulse-preview",
        "-s","pulse-preview-stream-bridge-worker","-w"],capture_output=True,text=True,
        timeout=10,check=True).stdout.strip()
    if not 32<=len(secret)<=256 or not secret.isascii():raise ValueError("hosted_secret_refused")
    project=api(cli,f"/v9/projects/{PROJECT}")
    bypass=next(k for k,v in project.get("protectionBypass",{}).items()
        if v.get("scope")=="automation-bypass")
    context=ssl.create_default_context()
    if ca_file:context.load_verify_locations(cafile=ca_file)
    opener=build_opener(NoRedirect,HTTPSHandler(context=context))
    deadline=time.monotonic()+runs*75;results=[];consecutive=0;deletions=0
    def call(path):
        request=Request("https://"+parsed.hostname+path,data=b"",method="POST",
            headers={"Authorization":"Bearer "+secret,"x-vercel-protection-bypass":bypass})
        try:response=opener.open(request,timeout=70)
        except HTTPError as error:response=error
        with response:
            data=response.read(65537)
            if len(data)>65536:raise ValueError("hosted_response_bound")
            return response.code,json.loads(data)
    for _ in range(runs):
        if time.monotonic()+70>deadline:break
        started=time.monotonic()
        if cleanup_retired:
            status,body=call("/api/pulse_retired_state")
            item=summary("/api/pulse_retired_state",status,body)
            for name in ("used_memory_before","used_memory_after","deletions","exchange_calls"):
                if type(body.get(name)) is int:item[name]=body[name]
            if type(body.get("cleanup")) is list and len(body["cleanup"])==2:item["cleanup"]=body["cleanup"]
            deletions=item.get("deletions",0)
            results.append(item);print(json.dumps(item,separators=(",",":")),flush=True)
            break
        if publish_only:
            status,body=call("/api/pulse_publish")
            item=summary("/api/pulse_publish",status,body)
            for name in ("pending_pairs","pending_refresh_pairs"):
                if type(body.get(name)) is int:item[name]=body[name]
            for name in ("pending_symbols","pending_refresh_symbols"):
                if type(body.get(name)) is list:item[name]=[s for s in body[name][:10] if isinstance(s,str) and re.fullmatch(r'[A-Z0-9]{1,64}',s)]
            if type(body.get("pending_details")) is list:item["pending_details"]=body["pending_details"][:10]
            if type(body.get("state_io")) is dict:item["state_io"]={k:v for k,v in body["state_io"].items() if type(v) is int and v>=0}
            if type(body.get("horizons")) is list:item["horizons"]=[h for h in body["horizons"] if h in ("1H","4H","24H")]
            if type(body.get("compose_phase_ms")) is list:item["compose_phase_ms"]=body["compose_phase_ms"][:3]
            results.append(item);print(json.dumps(item,separators=(",",":")),flush=True)
            if status!=200:break
            if runs==1:break
            delay=max(.5,60-(time.monotonic()-started))
            if time.monotonic()+delay+70>deadline:break
            time.sleep(delay)
            continue
        if setup_release:
            status,body=call("/api/pulse_release")
            item=summary("/api/pulse_release",status,body)
            for name in ("pairs","volume_source_ms","copied_records","pending_records"):
                if type(body.get(name)) is int:item[name]=body[name]
            for name in ("mapping_version","ranking_sha256"):
                value=body.get(name)
                if isinstance(value,str) and value.startswith("sha256:") and len(value)==71:item[name]=value
            if body.get("release_status") in ("release_ready","release_seeding"):
                item["release_status"]=body["release_status"]
            if type(body.get("release_universe")) is list and len(body["release_universe"])==350:
                item["release_universe"]=body["release_universe"]
            if type(body.get("cutoff")) is dict:item["cutoff"]=body["cutoff"]
            results.append(item);print(json.dumps(item,separators=(",",":")),flush=True)
            break
        if publish:
            with ThreadPoolExecutor(max_workers=2) as pool:
                worker=pool.submit(call,"/api/pulse_worker")
                publisher=pool.submit(call,"/api/pulse_publish")
                status,body=worker.result();pub_status,pub_body=publisher.result()
            pub_item=summary("/api/pulse_publish",pub_status,pub_body)
            if type(pub_body.get("horizons")) is list:
                pub_item["horizons"]=[h for h in pub_body["horizons"] if h in ("1H","4H","24H")]
            if type(pub_body.get("pending_pairs")) is int:pub_item["pending_pairs"]=pub_body["pending_pairs"]
            if type(pub_body.get("pending_refresh_pairs")) is int:pub_item["pending_refresh_pairs"]=pub_body["pending_refresh_pairs"]
            if type(pub_body.get("state_io")) is dict:pub_item["state_io"]={k:v for k,v in pub_body["state_io"].items() if type(v) is int and v>=0}
            results.append(pub_item);print(json.dumps(pub_item,separators=(",",":")),flush=True)
        else:status,body=call("/api/pulse_worker")
        item=summary("/api/pulse_worker",status,body)
        for name in ("scheduled_jobs","memory","quota","state_io"):
            if type(body.get(name)) is dict:
                item[name]={k:v for k,v in body[name].items()
                    if isinstance(k,str) and (type(v) in (int,float) or v is None)}
        if body.get("region") in ("bom1","sin1","unknown"):item["region"]=body["region"]
        if type(body.get("bootstrap_first")) is bool:item["bootstrap_first"]=body["bootstrap_first"]
        if type(body.get("history_budget_paused")) is bool:item["history_budget_paused"]=body["history_budget_paused"]
        if body.get("denied_source") in ("coindcx","binance"):item["denied_source"]=body["denied_source"]
        results.append(item)
        print(json.dumps(item,separators=(",",":")),flush=True)
        proceed,consecutive,quiet=recovery(status,body,consecutive)
        if not proceed or (publish and pub_status!=200):break
        delay=max(quiet,.5,60-(time.monotonic()-started))
        if time.monotonic()+delay+70>deadline:break
        time.sleep(delay)
    return {"preview_url":"https://"+parsed.hostname,"sha":deployed["meta"]["githubCommitSha"],
        "production_access":False,"deletions":deletions,"runs":results}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True);parser.add_argument("--url",required=True)
    parser.add_argument("--runs",type=int,default=3);parser.add_argument("--ca-file")
    parser.add_argument("--publish",action="store_true")
    parser.add_argument("--setup-release",action="store_true",help="Exactly one bounded Preview-only release handover")
    parser.add_argument("--publish-only",action="store_true",help="Source-free publisher only; never starts another collector")
    parser.add_argument("--cleanup-retired",action="store_true",help="Only the two explicit approved retired staging state hashes")
    args=parser.parse_args()
    result=run(args.vercel_cli,args.url,runs=args.runs,ca_file=args.ca_file,publish=args.publish,setup_release=args.setup_release,publish_only=args.publish_only,cleanup_retired=args.cleanup_retired)
    print(json.dumps({"evidence":result},separators=(",",":")))
