"""Operator driver: wait for the Preview build of a commit, then POST the worker until it publishes.
Uses `vercel curl` (protection bypass) and the secret from a private file; never prints the secret."""
import argparse
import json
import os
import subprocess
import sys
import time

PROJECT = "prj_Iu3ENBLVf3uLtfVjshOISANFzrPN"
SCOPE = "punksapien-projects"


def vc(cli, *args, input_text=None, timeout=200):
    env = dict(os.environ, NODE_OPTIONS="--use-system-ca")
    return subprocess.run(["node", cli, *args], capture_output=True, text=True, env=env, timeout=timeout, input=input_text)


def wait_ready(cli, sha, limit=300):
    end = time.time() + limit
    while time.time() < end:
        out = vc(cli, "api", f"/v6/deployments?projectId={PROJECT}&limit=8&target=preview", "--scope", SCOPE, "--raw").stdout
        try:
            for d in json.loads(out)["deployments"]:
                if d.get("meta", {}).get("githubCommitSha", "").startswith(sha):
                    if d["state"] == "READY":
                        return d["url"]
                    if d["state"] in ("ERROR", "CANCELED"):
                        raise SystemExit("build " + d["state"])
        except (ValueError, KeyError):
            pass
        time.sleep(10)
    raise SystemExit("build not ready in time")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--vercel-cli", required=True)
    p.add_argument("--secret-file", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--keep", action="store_true", help="continue after the first publish (keeps the pointers fresh)")
    a = p.parse_args()
    url = wait_ready(a.vercel_cli, a.sha)
    print("deployment", url, flush=True)
    secret = open(a.secret_file).read().strip()
    for n in range(1, a.runs + 1):
        started = time.time()
        out = vc(a.vercel_cli, "curl", "/api/pulse_worker", "--deployment", url, "--yes", "--scope", SCOPE, "--",
                 "-s", "-X", "POST", "-H", "Authorization: Bearer " + secret, "-w", "\nHTTP %{http_code}").stdout
        body, _, code = out.rpartition("\nHTTP ")
        try:
            r = json.loads(body[body.index("{"):])
        except ValueError:
            print(n, "http", code.strip(), body[-200:].strip(), flush=True)
            time.sleep(a.pause)
            continue
        brief = {k: r.get(k) for k in ("status", "collected", "skipped", "stopped", "cycles", "publishes", "price_publishes", "price_error", "registry", "source_dispatches", "elapsed_ms", "reason", "error_type", "detail") if k in r}
        brief["failed"] = r.get("failed") or {}
        brief["pending"] = len(r.get("pending_pairs", []))
        if "channels" in r:
            brief["channels"], brief["artifacts"] = r["channels"], r["artifacts"]
        print(n, "http", code.strip(), json.dumps(brief), f"{time.time() - started:.0f}s", flush=True)
        if r.get("status") == "published" and not a.keep:
            break
        time.sleep(a.pause)


if __name__ == "__main__":
    main()
