"""Fetch Preview-only worker secret and existing protection token in memory; no Redis credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import secrets

from preview_redis_access import api, PROJECT

parser = argparse.ArgumentParser()
parser.add_argument("cli")
parser.add_argument("url")
parser.add_argument("--create-branch-secret", action="store_true")
parser.add_argument("--browser", action="store_true")
args = parser.parse_args()
cli, url = args.cli, args.url
service = "pulse-preview-stream-bridge-worker"
branch = "feat/preview-stream-bridge"
account = "pulse-preview"
if args.create_branch_secret:
    # Separate branch override: never rotate the existing integration/production worker secret.
    secret = secrets.token_urlsafe(48)
    saved = subprocess.run(["security","add-generic-password","-U","-a",account,"-s",service,"-w",secret],
                           capture_output=True,timeout=15)
    if saved.returncode:
        raise SystemExit("keychain_secret_setup_failed")
    p = subprocess.run(["node","--use-system-ca",cli,"env","add","PULSE_WORKER_SECRET","preview",branch,"--sensitive"],
                       input=secret,text=True,capture_output=True,cwd="/tmp/pulse-market-terminal",timeout=30)
    if p.returncode:
        raise SystemExit("branch_secret_setup_failed")
    print("Branch-only worker secret configured; retained in macOS Keychain, no secret printed.")
    raise SystemExit(0)
deployment = api(cli, f"/v13/deployments/{url.removeprefix('https://')}")
if deployment.get("projectId") != PROJECT or deployment.get("target") == "production" or deployment.get("meta", {}).get("githubCommitRef") != branch:
    raise SystemExit("preview_deployment_scope_refused")
project = api(cli, f"/v9/projects/{PROJECT}")
bypass = next(k for k, v in project.get("protectionBypass", {}).items() if v.get("scope") == "automation-bypass")
secret = subprocess.run(["security","find-generic-password","-a",account,"-s",service,"-w"],capture_output=True,text=True,check=True).stdout.strip()
result = subprocess.run(["node", "--use-system-ca", str(Path(__file__).with_name("preview_stream_once.cjs"))],
    input=json.dumps({"url":url,"bypass":bypass,"secret":secret,"browser":args.browser}),text=True,env=os.environ,timeout=140)
raise SystemExit(result.returncode)
