"""Protected Preview browser check; credentials stay in memory and child stdin."""
import argparse,json,os,subprocess
from pathlib import Path
from urllib.parse import urlsplit
from preview_redis_access import api,PROJECT

def run(cli,origin):
    url=urlsplit(origin)
    if url.scheme!="https" or not url.hostname or not url.hostname.endswith(".vercel.app") or url.path not in ("","/") or url.username or url.password or url.query or url.fragment:
        raise ValueError("preview_origin")
    deployment=api(cli,"/v13/deployments/"+url.hostname)
    if deployment.get("projectId")!=PROJECT or deployment.get("target")=="production" or deployment.get("readyState")!="READY" or deployment.get("meta",{}).get("githubCommitRef") not in ("fix/preview-detail-data","feat/preview-stream-bridge"):
        raise ValueError("preview_scope")
    project=api(cli,f"/v9/projects/{PROJECT}")
    token=next(k for k,v in project.get("protectionBypass",{}).items() if v.get("scope")=="automation-bypass")
    completed=subprocess.run(["node",str(Path(__file__).with_name("pulse_detail_browser.cjs"))],
        input=json.dumps(dict(origin=origin.rstrip("/"),token=token)),capture_output=True,text=True,env=os.environ.copy(),timeout=180)
    for body in (completed.stdout,completed.stderr):
        for line in body.splitlines():
            try:value=json.loads(line)
            except ValueError:continue
            if type(value) is dict and "status" in value:print(json.dumps(value))
    return completed.returncode

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--vercel-cli",required=True);parser.add_argument("--url",required=True)
    args=parser.parse_args()
    try:raise SystemExit(run(args.vercel_cli,args.url))
    except Exception as exc:
        print(json.dumps(dict(status="browser_probe_failed",error_type=type(exc).__name__)))
        raise SystemExit(1) from None
