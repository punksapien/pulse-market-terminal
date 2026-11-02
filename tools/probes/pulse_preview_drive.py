"""ADR-0106: explicit finite Preview worker + stream driver; no secret files/argv."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
import ssl

PATHS = ("/api/pulse_worker", "/api/pulse_stream")
BRANCH = "feat/preview-stream-bridge"
BRANCHES = (BRANCH,"fix/preview-detail-data")
CALL_RESERVE_S = 75


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def summary(path, status, body):
    """Never log arbitrary provider text, request headers, URLs or credentials."""
    result = {"path": path, "http": status}
    for key in ("status", "reason", "detail", "error_type", "transport_error_type", "transport_endpoint", "stopped", "stage", "price_error", "funding_error", "live_stopped"):
        value = body.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_:]{1,100}", value):
            result[key] = value
    for key in ("schedule_attempted","schedule_due","schedule_overdue_max_ms","bootstrap_completed","collected", "hot_collected", "hot_skipped", "hot_elapsed_ms", "hot_poll_gap_max_ms", "funding_checked", "funding_updated", "funding_elapsed_ms", "ls_tip_collected", "ls_tip_skipped", "ls_tip_elapsed_ms", "oldest_oi_source_ms", "newest_oi_source_ms", "pairs", "source_dispatches", "elapsed_ms", "publishes", "price_publishes", "funding_publishes", "funding_max_publish_gap_ms", "source_timeout_ms", "source_elapsed_ms", "ticks", "errors"):
        value = body.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    for key in ("detail_collected","history_publishes","history_elapsed_ms"):
        if type(body.get(key)) is int and body[key]>=0:result[key]=body[key]
    if type(body.get("job_time_ms")) is dict:
        result["job_time_ms"]={name:{k:v for k,v in values.items() if k in ("attempts","sum_ms","max_ms")
            and type(v) is int and v>=0} for name,values in body["job_time_ms"].items()
            if isinstance(name,str) and re.fullmatch(r"[a-z0-9_]{1,32}",name) and type(values) is dict}
    if type(body.get("failed")) is dict and body["failed"]:
        from collections import Counter
        result["failed_count"]=len(body["failed"])
        result["failure_reasons"]=dict(Counter(reason if isinstance(reason,str) and
            re.fullmatch(r"[a-zA-Z0-9_:]{1,100}",reason) else "redacted"
            for reason in body["failed"].values()))
    return result


def run_lane(path, deadline, post, stop, *, now=time.monotonic, wait=None, emit=print):
    if path not in PATHS:
        raise ValueError("driver_path_refused")
    wait = stop.wait if wait is None else wait
    calls = busy = failures = maintenance_restarts = 0
    while not stop.is_set() and deadline - now() >= CALL_RESERVE_S:
        # One in-flight request per lane. Never restart a timed-out request in parallel.
        try:
            status, body = post(path, min(70, deadline - now()))
        except Exception:
            stop.set()
            emit({"path": path, "reason": "driver_transport_failed"})
            break
        calls += 1
        emit(summary(path, status, body))
        if status == 409 and body.get("status") == "lease_busy":
            busy += 1
            delay = body.get("retry_after_ms", 5000)
            delay = min(5000, max(1000, delay)) / 1000 if type(delay) is int else 5
        elif (status==200 and body.get("status")=="incomplete"
              and body.get("stopped")=="admission:budget_pruned_retry" and maintenance_restarts<3):
            maintenance_restarts+=1
            delay=1
        elif (status == 200 and body.get("status") in ("published", "incomplete", "completed")
              and body.get("stopped") in (None,"time_budget")):
            failures = 0
            maintenance_restarts=0
            delay = 0.25
        else:
            failures += 1
            # ADR-0128: an optional socket refusal must not stop healthy REST publication.
            if path=="/api/pulse_stream" and status==503 and body.get("reason")=="upstream_refused":
                emit({"path":path,"reason":"polling_fallback"})
                break
            # Fail closed: don't hammer a failed publisher or a source circuit.
            stop.set()
            break
        wait(min(delay, max(0, deadline - now())))
    return {"path": path, "calls": calls, "busy": busy, "failures": failures}


def main():
    from preview_redis_access import api, PROJECT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--ca-file", help="additional public corporate CA; certificate verification remains enabled")
    args = parser.parse_args()
    if not CALL_RESERVE_S <= args.duration_seconds <= 3600:
        parser.error("duration must be 75..3600 seconds; no infinite mode")
    from urllib.parse import urlsplit
    url = urlsplit(args.url)
    if url.scheme != "https" or not url.hostname or not url.hostname.endswith(".vercel.app") or url.username or url.password or url.port or url.query or url.fragment or url.path not in ("", "/"):
        parser.error("Preview origin required")
    deployment = api(args.vercel_cli, f"/v13/deployments/{url.hostname}")
    if (deployment.get("projectId") != PROJECT or deployment.get("target") == "production"
            or deployment.get("readyState") != "READY" or deployment.get("meta", {}).get("githubCommitRef") not in BRANCHES):
        raise SystemExit("driver_preview_scope_refused")
    project = api(args.vercel_cli, f"/v9/projects/{PROJECT}")
    bypass = next(k for k, v in project.get("protectionBypass", {}).items() if v.get("scope") == "automation-bypass")
    secret = subprocess.run(["security", "find-generic-password", "-a", "pulse-preview", "-s",
                             "pulse-preview-stream-bridge-worker", "-w"], capture_output=True, text=True, check=True).stdout.strip()
    context = ssl.create_default_context()
    if args.ca_file:
        context.load_verify_locations(cafile=args.ca_file)
    opener = build_opener(NoRedirect, HTTPSHandler(context=context))
    origin = "https://" + url.hostname

    def post(path, timeout):
        request = Request(origin + path, data=b"", method="POST", headers={
            "Authorization": "Bearer " + secret, "x-vercel-protection-bypass": bypass})
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("driver_response_bound")
            body = json.loads(raw)
            if type(body) is not dict:
                raise ValueError("driver_response_shape")
            return response.code, body

    # No daemonisation: terminal/process lifetime bounds the driver as well as the deadline.
    stop = threading.Event()
    deadline = time.monotonic() + args.duration_seconds
    lock = threading.Lock()

    def emit(value):
        with lock:
            print(json.dumps(value, separators=(",", ":")), flush=True)

    emit({"event": "driver_started", "duration_seconds": args.duration_seconds,
          "sha": deployment.get("meta", {}).get("githubCommitSha")})
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_lane, path, deadline, post, stop, emit=emit) for path in PATHS]
        try:
            for future in futures:
                emit(future.result())
        except KeyboardInterrupt:
            stop.set()  # in-flight calls finish within their existing timeout; never start another
    emit({"event": "driver_stopped"})


if __name__ == "__main__":
    main()
