"""ADR-0068 resumable eight-worker OI/L/S operator bootstrap, never production."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_cohort_sources import write
from pulse_real_ls import header_refresh_needed, raw_reference as ls_reference, verify as verify_ls
from pulse_real_oi import raw_reference as oi_reference
from pulse.collectors.binance_oi import fetch_current, fetch_history
from pulse.collectors.binance_ls import fetch_history as fetch_ls, history_spec as ls_spec
from pulse.collectors.oi_history import history_spec, build_history_ring
from pulse.metrics.open_interest import OIObservation, WINDOW_MS
from pulse.metrics.oi_selection import change_with_baselines
from pulse.store.ls_reader import read_metrics
from pulse.store.metric_capsule import MetricCapsule, ACQUIRE, RENEW
from pulse.store.capsule_model import CapsuleSpec
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.shared_admission import RollingLimit
from pulse.store.expansion_guard import guard
from pulse.store.staging_policy import MeasuredCapacity
from pulse.store.cohort_memory import settled_sample, marginal_projection

SCOPE = "operator-office-c1-20261026"
CURSOR = """if string.sub(KEYS[1],1,10)~='pulse:stg:' or string.sub(KEYS[2],1,10)~='pulse:stg:' then return 0 end
if #ARGV[2]>65536 or redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return 0 end
redis.call('SET',KEYS[1],ARGV[2]); return 1"""


def source_admission(client):
    return BinanceAdmission(client, SCOPE, {"history": RollingLimit(700,300000),
        "weight": RollingLimit(1680,60000)}, approved_plan_fallback=True, approved_bootstrap=True)


class Dispatch:
    def __init__(self, client, ca_file):
        self.client, self.ca_file = client, ca_file
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.header = None
        self.last_dispatch = 0
        self.calls = 0
        self.statuses = {}
        self.denials = []
        self.heartbeat = source_admission(client)

    def clock(self):
        seconds, micros = self.client.time()
        return seconds*1000+micros//1000

    def admit(self, admission, costs):
        with self.lock:
            if self.stop.is_set():
                return [b"cohort_stopped"]
            # ADR-0073: pacing can cross a provider minute. Check afterwards,
            # with a 1s margin; Lua remains authoritative, denials never retry.
            delay = .2-(time.monotonic()-self.last_dispatch)
            if delay > 0:
                self.stop.wait(delay)
            now = self.clock()
            if now % 60000 >= 59000:
                self.stop.wait((60000-now % 60000)/1000+.1)
            if self.stop.is_set():
                return [b"cohort_stopped"]
            if header_refresh_needed(self.header, self.clock()):
                spec = CapsuleSpec("B-BTC_USDT","BTCUSDT","oi","quantity",60000,1536,900000,900000,True)
                fetch_current(spec, self.heartbeat, clock=self.clock, ca_file=self.ca_file)
                self.header = dict(self.heartbeat.last_feedback)
                self.calls += 1
                self.statuses[200] = self.statuses.get(200,0)+1
                self.last_dispatch = time.monotonic()
            # The heartbeat also consumes a dispatch in the bootstrap cap.
            delay = .2-(time.monotonic()-self.last_dispatch)
            if delay > 0:
                self.stop.wait(delay)
            if self.stop.is_set():
                return [b"cohort_stopped"]
            self.last_dispatch = time.monotonic()
            decision = admission.reserve(costs)
            if decision[0] != b"reserved":
                self.denials.append(decision[0].decode("ascii"))
                self.stop.set()
            else:
                self.calls += 1
            return decision

    def feedback(self, admission, status, headers):
        result = admission.feedback(status,headers)
        with self.lock:
            self.statuses[status] = self.statuses.get(status,0)+1
            if result["used_weight_1m"] > 0:
                self.header = result
            if status in (0,403,418,429,451) or status >= 500:
                self.stop.set()
        return result


class WorkerAdmission:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.source = source_admission(dispatcher.client)

    def reserve(self,costs):
        return self.dispatcher.admit(self.source,costs)

    def feedback(self,status,headers):
        return self.dispatcher.feedback(self.source,status,headers)


def pair_specs(row):
    pair,symbol = row["pair_id"],row["symbol"]
    return (CapsuleSpec(pair,symbol,"oi","quantity",60000,1536,900000,900000,True),
            history_spec(pair,symbol),ls_spec(pair,symbol,"global"),ls_spec(pair,symbol,"top"))


def ingest_pair(row, client, dispatcher, run_id):
    admission = WorkerAdmission(dispatcher)
    stores = tuple(MetricCapsule(client,run_id,spec,approved_plan_fallback=True) for spec in pair_specs(row))
    raw = {}
    current = fetch_current(stores[0].spec,admission,clock=dispatcher.clock,ca_file=dispatcher.ca_file,
                            evidence_sink=lambda v:raw.update(current=v))
    histories = [fetch_history(stores[1].spec,admission,clock=dispatcher.clock,ca_file=dispatcher.ca_file,
                              evidence_sink=lambda v:raw.update(oi=v))]
    for series,store in zip(("global","top"),stores[2:]):
        histories.append(fetch_ls(store.spec,admission,clock=dispatcher.clock,ca_file=dispatcher.ca_file,
                                  evidence_sink=lambda v,s=series:raw.update({s:v})))
    if dispatcher.stop.is_set():
        raise RuntimeError("cohort_stopped_before_write")
    build_history_ring(histories[0],stores[1].spec,now_ms=dispatcher.clock())
    stores[0].acquire(); stores[0].initialise()
    if stores[0].write(current)[0] not in (b"written",b"duplicate"):
        raise RuntimeError("cohort_current_write_refused")
    for store,points in zip(stores[1:],histories):
        store.acquire(); store.initialise()
        for offset in range(0,len(points),16):
            if dispatcher.stop.is_set():
                raise RuntimeError("cohort_stopped_during_ingress")
            store.write_many(points[offset:offset+16])
    view = stores[0].read()
    if view.get("source_ms") != current.source_ms or view.get("values") != current.values:
        raise RuntimeError("cohort_current_roundtrip_mismatch")
    changes = {}
    for horizon,window in WINDOW_MS.items():
        own,cold = (store.baseline(current.source_ms-window,tolerance)
                    for store,tolerance in zip(stores[:2],(60000,300000)))
        def convert(view):
            if view["status"] == "available":
                return OIObservation(view["source_ms"],view["values"][0],row["pair_id"])
            if view["reason"] not in ("baseline_missing","discarded_baseline_candidate",
                                      "exact_timestamp_conflict","quarantined"):
                raise RuntimeError("cohort_baseline_refused:"+view["reason"])
            return None
        value = change_with_baselines(OIObservation(current.source_ms,current.values[0],row["pair_id"]),
            convert(own),convert(cold),horizon=horizon,now_ms=dispatcher.clock())
        expected = oi_reference(raw["current"],raw["oi"],horizon)
        if value.mode == "cold-start" and (value.status != expected["status"] or value.status == "available" and (
                value.baseline_asof_ms != int(expected["baseline"]["timestamp"]) or not math.isclose(
                value.value_fraction,float(expected["value_fraction_exact"]),rel_tol=1e-12,abs_tol=1e-15))):
            raise RuntimeError("cohort_oi_raw_mismatch")
        changes[horizon] = {"metric":asdict(value),"raw_witness":expected,"own_selection":own,"cold_selection":cold}
    now = dispatcher.clock()
    ls = read_metrics(*stores[2:],now_ms=now)
    expected_ls = ls_reference({s:raw[s] for s in ("global","top")},now_ms=now)
    compared = verify_ls(ls,expected_ls)
    memory = client.info("memory")
    guard(memory,additional_bytes=8*65536,approved_plan_fallback=True)
    return {"symbol":row["symbol"],"oi":changes,"ls_witness":expected_ls,
            "ls_available_values":compared,"ls":{
                "series":{s:{"ratio":asdict(v["ratio"]),"evolution":{
                    h:asdict(e["metric"]) for h,e in v["evolution"].items()}}
                    for s,v in ls["series"].items()},"divergence":asdict(ls["divergence"])},
            "raw_current":raw["current"],"raw_sha256":{k:hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest()
                for k,v in raw.items()},"ring_ttls":[client.ttl(s.frame) for s in stores],"memory":memory}


def resume_result(prior_path, manifest_digest):
    raw = prior_path.read_bytes()
    result = json.loads(raw)
    if result.get("manifest_digest") != manifest_digest:
        raise ValueError("cursor_manifest_mismatch")
    result.setdefault("prior_invocations",[]).append({
        "artifact":prior_path.name,"sha256":hashlib.sha256(raw).hexdigest(),
        **{key:result.get(key) for key in ("status","reason","source_dispatches","status_counts","admission_denials")}})
    for stale in ("reason","admission_reason","cursor_persist_failed"):
        result.pop(stale,None)
    return result


def run(cli,ca_file,source_file,output,*,resume=False,resume_from=None):
    import redis
    sources = json.loads(source_file.read_text())
    if sources.get("status") != "manifest_verified":
        raise ValueError("verified_manifest_required")
    manifest = sources["manifest"]
    unsigned = {k:v for k,v in manifest.items() if k != "digest"}
    if hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(",",":")).encode()).hexdigest() != manifest["digest"]:
        raise ValueError("manifest_digest_mismatch")
    prior_path = resume_from or output
    result = resume_result(prior_path,manifest["digest"]) if resume else {
        "run_id":uuid.uuid4().hex,"pairs":[],"stages":[]}
    result["settled_stages"] = []
    gate, identity = connect_staging(cli,approved_plaintext=True)
    pool = redis.ConnectionPool(connection_class=gate.connection_pool.connection_class,max_connections=10,
                                **gate.connection_pool.connection_kwargs)
    client = redis.Redis(connection_pool=pool)
    gate.close()
    run_id = result["run_id"]
    prefix = "pulse:stg:{"+run_id+"}:cohort:"
    cursor,lease,fence = (prefix+s for s in ("cursor","lease","fence"))
    result.update(status="running",manifest_digest=manifest["digest"],workers=8,production_access=False,
                  deletions=0,transport=identity["transport"],keys=[cursor,lease,fence])
    for row in manifest["pairs"]:
        for spec in pair_specs(row):
            store = MetricCapsule(client,run_id,spec,approved_plan_fallback=True)
            result["keys"].extend((store.frame,store.lease,store.fence))
    write(output,result)
    dispatcher = Dispatch(client,ca_file)
    owner = None
    lease_thread = None
    def checkpoint():
        write(output,result)
        payload = json.dumps({"manifest_digest":manifest["digest"],"completed":[r["symbol"] for r in result["pairs"]],
                              "status":result["status"]})
        if client.eval(CURSOR,2,cursor,lease,owner,payload) != 1:
            raise RuntimeError("cohort_cursor_fenced_out")
    def renew():
        while not dispatcher.stop.wait(8):
            try:
                if client.eval(RENEW,1,lease,owner) != 1:
                    raise RuntimeError("cohort_job_lease_lost")
            except Exception:
                dispatcher.stop.set()
                return
    try:
        guard(client.info("memory"),additional_bytes=8*65536,approved_plan_fallback=True)
        owner = client.eval(ACQUIRE,2,lease,fence,uuid.uuid4().hex)
        if not owner:
            raise RuntimeError("cohort_job_lease_busy")
        saved = client.get(cursor)
        if saved:
            saved = json.loads(saved)
            if not resume or saved.get("manifest_digest") != manifest["digest"]:
                raise RuntimeError("cohort_cursor_identity_refused")
            local = {r["symbol"] for r in result["pairs"]}
            if not set(saved["completed"]) <= local:
                raise RuntimeError("cohort_cursor_evidence_missing")
            result["pairs"] = [r for r in result["pairs"] if r["symbol"] in saved["completed"]]
        elif resume:
            raise RuntimeError("cohort_cursor_lost")
        lease_thread = threading.Thread(target=renew,daemon=True)
        lease_thread.start()
        result.setdefault("memory_baseline",client.info("memory"))
        checkpoint()
        with ThreadPoolExecutor(max_workers=8) as executor:
            for stage in (5,10,25,50):
                done = {r["symbol"] for r in result["pairs"]}
                todo = [r for r in manifest["pairs"][:stage] if r["symbol"] not in done]
                if not todo and stage != len(done):
                    continue
                started = time.perf_counter()
                futures = {executor.submit(ingest_pair,row,client,dispatcher,run_id):row for row in todo}
                for future in as_completed(futures):
                    try:
                        report = future.result()
                    except Exception:
                        dispatcher.stop.set()
                        for queued in futures:
                            queued.cancel()
                        raise
                    result["pairs"].append(report)
                    checkpoint()
                    print(json.dumps({"progress":"pair_verified","completed":len(result["pairs"]),
                                      "symbol":report["symbol"],"used_memory":report["memory"]["used_memory"]}),flush=True)
                def wait(seconds):
                    if dispatcher.stop.wait(seconds):
                        raise RuntimeError("cohort_stopped_during_memory_settle")
                settled = settled_sample(client,wait=wait,clock=dispatcher.clock)
                memory = settled["samples"][-1]
                base = result["memory_baseline"]["used_memory"]
                projected = base+max(2.6*sum(s.nbytes for s in pair_specs(manifest["pairs"][0])),
                                    max(0,memory["used_memory"]-base)/stage)*503
                actual_projection = base+(projected-base)*max(503,manifest["eligible_count"])/503
                measured = MeasuredCapacity.from_info(memory,provider_usable_bytes=125000000,
                                                       approved_plan_fallback=True)
                capacity = MeasuredCapacity(measured.ceiling_bytes,int(actual_projection))
                settled["pairs"] = stage
                settled["marginal"] = marginal_projection(result["settled_stages"],settled,
                    universe=manifest["eligible_count"],ceiling=measured.ceiling_bytes)
                result["settled_stages"].append(settled)
                result["stages"].append({"pairs":stage,"memory":memory,"elapsed_seconds":time.perf_counter()-started,
                    "raw_capsule_bytes":stage*sum(s.nbytes for s in pair_specs(manifest["pairs"][0])),
                    "projection_503_current_metrics_bytes":int(projected),
                    "projection_full_universe_bytes":int(actual_projection),"projection_pressure":capacity.pressure()})
                checkpoint()
                print(json.dumps({"progress":"settled_stage","pairs":stage,
                    "used_memory_median":settled["used_memory_median"],
                    "old_projection_diagnostic_only":int(actual_projection),
                    "marginal":settled["marginal"]}),flush=True)
                if settled["marginal"]["stop"]:
                    raise RuntimeError("cohort_marginal_projection_90_percent_stop")
        result["status"] = "oi_ls_fifty_verified_not_full_metric"
    except Exception as error:
        result.update(status="stopped",reason=str(error) if isinstance(error,(RuntimeError,ValueError)) else type(error).__name__,
                      admission_reason=getattr(error,"admission_reason",None))
    finally:
        dispatcher.stop.set()
        if lease_thread is not None:
            lease_thread.join(timeout=3)
        result.update(source_dispatches=dispatcher.calls,status_counts=dispatcher.statuses,admission_denials=dispatcher.denials)
        try:
            try:
                result["memory_final"] = client.info("memory")
            except Exception:
                result["memory_final"] = {"status":"unavailable"}
            if owner:
                try:
                    checkpoint()
                except Exception:
                    result["cursor_persist_failed"] = True
                    result["status"] = "stopped"
                    result.setdefault("reason","cohort_cursor_persist_failed")
                    write(output,result)
            else:
                write(output,result)
        finally:
            client.close(); pool.disconnect()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--ca-file",required=True)
    parser.add_argument("--sources",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--resume-from",help="Preserve an earlier evidence file and reuse its fenced cursor")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    prior = Path(args.resume_from).resolve() if args.resume_from else None
    if (output.parent != ROOT/"docs/evidence"
            or (prior is not None and (not args.resume or prior == output or not prior.is_file()
                                      or prior.parent != ROOT/"docs/evidence" or output.exists()))
            or (prior is None and output.exists() != args.resume)):
        raise SystemExit("invalid_evidence_resume_path")
    result = run(args.vercel_cli,args.ca_file,Path(args.sources),output,resume=args.resume,resume_from=prior)
    print(json.dumps({"status":result["status"],"reason":result.get("reason"),"pairs":len(result["pairs"])}))
    if result["status"] == "stopped":
        raise SystemExit(1)
