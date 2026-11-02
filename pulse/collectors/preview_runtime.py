"""ADR-0099: one bounded, fenced Preview worker run: collect -> compose -> publish.

Hand-triggered (Vercel crons do not run on Preview). Resumable: every completed pair is durable in
WorkerState, so a cold start publishes only when the whole fixed cohort has been collected once.
No viewer path imports this module; reads never call a source. Staging only (pulse:stg:*)."""
import os
import sys
import threading
import time

from .binance_transport import AdmissionDenied
from .funding_inputs import info_index
from .preview_compose import prepare, prepare_price, prepare_funding
from .preview_pair import collect, collect_hot, collect_ls_tip, collect_funding,collect_references,collect_detail
from .preview_history import prepare_histories
from .preview_registry import load as load_registry, bootstrap as bootstrap_registry
from .preview_sources import Sources, DispatchPacer
from .preview_batch import collect_batch
from pulse.store.artifact_publisher import ArtifactPublisher
from pulse.store.runtime_document import RuntimeDocument
from pulse.store.worker_state import WorkerState,measured_io
from pulse.store.production_scope import connection_url, scope_client

PUBLISH_RESERVE_S = 18      # ADR-0126: snapshot plus lazy history compression/staging before the55s deadline
PRICE_PERIOD_MS = 2000      # live price pointer refresh target; source latency adds to it
FUNDING_PERIOD_MS = 5000     # separate15s envelope; never waits for slow snapshot preparation
LOOP_MARGIN_S = 8           # do not start another cycle unless a whole one plausibly fits
CYCLE_WINDOW_S = 20         # later cycles: collect window is this minus the source dispatcher's 10 s safety margin
PAIR_REFRESH_MS = 45000     # a pair refreshed within this window is skipped (steady state: about once a minute)
COINDCX = "https://public.coindcx.com"
SCOPE = "vercel-bom1-c1"


def connect(env):
    """Preview-only Redis connection; plaintext only under the recorded staging exception."""
    import redis
    url = connection_url(env)
    options = {"ssl_cert_reqs": "required", "ssl_check_hostname": True} if url.startswith("rediss://") else {}
    client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=5, max_connections=40, retry_on_timeout=False, **options)
    return scope_client(client, env)


def fetch_bulk(sources):
    """CoinDCX realtime bulk (price, funding estimate, volume). One request for the whole universe."""
    value = sources.request("/market_data/v3/current_prices/futures/rt", {}, host=COINDCX)
    if type(value) is not dict or type(value.get("prices")) is not dict or type(value.get("ts")) is not int:
        raise ValueError("bulk_shape")
    return value


def fetch_funding_metadata(sources):
    """Binance fundingInfo and premiumIndex (all symbols); provider Date header is the source time."""
    info_payload = sources.request("/fapi/v1/fundingInfo", {}, family=True)
    info = {"data": info_index(info_payload), "source_ms": sources.last_source_ms}
    premium_payload = sources.request("/fapi/v1/premiumIndex", {}, weight=10)
    if type(premium_payload) is not list or len(premium_payload) > 4096:
        raise ValueError("premium_shape")
    premium = {"data": {row["symbol"]: row for row in premium_payload if type(row) is dict and isinstance(row.get("symbol"), str)},
               "source_ms": sources.last_source_ms}
    if info["source_ms"] is None or premium["source_ms"] is None:
        raise ValueError("funding_metadata_provider_time_missing")
    return info, premium


class _EndPass(Exception):
    """Controlled early end of a pass (for example after rebuilding the registry); not an error."""


def _reason(exc):
    return "admission:" + exc.admission_reason if isinstance(exc, AdmissionDenied) and getattr(exc, "admission_reason", None) else str(exc)[:80]


def run(*, run_id, mapping_version, duration_ms, env=None, deps=None, max_cycles=None):
    """Returns {"status": "published"|"incomplete"|"lease_busy", ...}. Never raises provider text.

    A pass is a loop of cycles inside the 55 s budget. Cycle 1 may collect for most of the budget (cold start);
    later cycles are short (collect what is due, compose, publish) so the live price and funding pointers, which
    expire after 15 s, are refreshed several times per invocation instead of once."""
    env = os.environ if env is None else env
    if env.get("VERCEL_ENV")=="production" and env.get("PULSE_RELEASE_TOP350")!="1":
        raise ValueError("production_release350_required")
    mode=env.get("PULSE_UNIVERSE_MODE","cohort")
    from pulse.store.production_scope import dcx12_enabled
    dcx12=dcx12_enabled(env)
    if dcx12 and mode!="all":
        raise ValueError("preview_dcx_policy_scope")
    if mode not in ("cohort","all"):raise ValueError("worker_universe_mode")
    registry_load,registry_bootstrap=load_registry,bootstrap_registry
    if mode=="all":
        from .universe_registry import load as registry_load, bootstrap_step as registry_bootstrap
    from .release_universe import capsule_run
    data_run=capsule_run(env,run_id)
    if env.get("PULSE_RELEASE_TOP350")=="1":
        if mode!="all":raise ValueError("release_mode_refused")
        from .release_universe import load as registry_load, bootstrap_refused as registry_bootstrap
    d = dict(connect=connect, Sources=Sources, load_registry=registry_load, fetch_bulk=fetch_bulk,
             fetch_metadata=fetch_funding_metadata, bootstrap_registry=registry_bootstrap, collect=collect, prepare=prepare, prepare_price=prepare_price, sleep=time.sleep,
             collect_hot=collect_hot, collect_ls_tip=collect_ls_tip, collect_funding=collect_funding, collect_references=collect_references, collect_detail=collect_detail, prepare_histories=prepare_histories, prepare_funding=prepare_funding, Publisher=ArtifactPublisher, State=WorkerState, Document=RuntimeDocument, now=time.monotonic)
    d.update(deps or {})
    started = d["now"]()
    deadline = started + duration_ms / 1000
    client = d["connect"](env)
    pacer=DispatchPacer(binance_gap=.05 if mode=="all" else .15,coindcx_gap=.095 if dcx12 else .15)
    def new_sources(limit):
        value=d["Sources"](client,scope="vercel-bom1-all-v1" if mode=="all" else SCOPE,deadline=limit,
            **({"preview_dcx12":True} if dcx12 else {}))
        value.pacer=pacer
        return value
    publisher = d["Publisher"](client, run_id, mapping_version)
    try:
        try:
            publisher.acquire()
        except RuntimeError as exc:
            if str(exc) == "publication_lease_busy":
                return {"status": "lease_busy"}
            raise
        sources = new_sources(deadline - (2 if mode=="all" else PUBLISH_RESERVE_S))
        sources.check_lease = publisher.renew
        state = d["State"](publisher)
        result = {"status": "incomplete", "collected": 0, "skipped": 0, "failed": {}, "stopped": None, "cycles": 0,
                  "publishes": 0, "price_publishes": 0, "funding_publishes": 0}
        universe = None
        stage = "initialise"
        stop_ticks = threading.Event()
        tick_thread = [None]
        tick_sources_used = []
        fenced = []

        def price_loop():
            """Fast live path on its own thread and its own source dispatcher (separate pacing and deadline): one bulk
            request, a price-only package, one pointer promotion, about every two seconds. Never aborts the pass;
            a stale owner stops it and is re-raised by the main thread."""
            tick_sources = new_sources(deadline - 4)
            tick_sources_used.append(tick_sources)
            tick_sources.live = True
            tick_sources.live_deadline = deadline - 4
            funding_metadata = None
            next_funding = started
            funding_disabled = False
            last_funding_publish = None
            def check_tick_lease():
                if stop_ticks.is_set():
                    raise RuntimeError("worker_stopping")
                publisher.renew()
            tick_sources.check_lease = check_tick_lease
            while not stop_ticks.is_set() and d["now"]() < deadline - 4:
                started_tick = d["now"]()
                batch = None
                try:
                    batch = d["fetch_bulk"](tick_sources)
                    publisher.renew()
                    now_ms = tick_sources.clock()
                    package = d["prepare_price"](universe, publisher, batch, now_ms=now_ms,
                        **({"compressed_only":True} if mode=="all" else {}))
                    expected = {package.channel: d["Document"](client, run_id, package.channel, mapping_version).read(now_ms=now_ms)["revision"]}
                    publisher.publish([package], expected_revisions=expected)
                    result["price_publishes"] += 1
                    result.pop("price_error", None)
                except (AdmissionDenied, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    if str(exc) == "worker_stopping":
                        return
                    if str(exc) in ("publication_fenced_out", "worker_state_fenced_out"):
                        fenced.append(exc)
                        return
                    result["price_error"] = _reason(exc)
                    if str(exc) == "worker_source_budget_exhausted":
                        result.pop("price_error",None)
                        result["live_stopped"]="time_budget"
                        return
                    if isinstance(exc, AdmissionDenied):
                        return
                except Exception:
                    result["price_error"]="live_transport_failed"
                    return
                if batch is not None and not funding_disabled and d["now"]() >= next_funding:
                    try:
                        clock_ms=tick_sources.clock()
                        rollover=funding_metadata is not None and any(
                            type(funding_metadata[1]["data"].get(m["symbol"],{}).get("nextFundingTime")) is int
                            and funding_metadata[1]["data"][m["symbol"]]["nextFundingTime"]<=clock_ms for m in universe)
                        if funding_metadata is None or rollover:
                            funding_metadata = d["fetch_metadata"](tick_sources)
                        current_records = (state.read_funding_many([m["symbol"] for m in universe])
                            if mode=="all" else state.read_many([m["symbol"] for m in universe]))
                        publisher.renew()
                        now_ms = tick_sources.clock()
                        package = d["prepare_funding"](universe, current_records, publisher, batch, *funding_metadata, now_ms=now_ms,
                            **({"compressed_only":True} if mode=="all" else {}))
                        expected = {package.channel: d["Document"](client, run_id, package.channel, mapping_version).read(now_ms=now_ms)["revision"]}
                        publisher.publish([package], expected_revisions=expected)
                        result["funding_publishes"] += 1
                        published_at=time.monotonic()
                        if last_funding_publish is not None:
                            gap=int((published_at-last_funding_publish)*1000)
                            result["funding_max_publish_gap_ms"]=max(result.get("funding_max_publish_gap_ms",0),gap)
                        last_funding_publish=published_at
                        result.pop("funding_error", None)
                    except (AdmissionDenied, RuntimeError, ValueError, KeyError, TypeError) as exc:
                        if str(exc) == "worker_stopping": return
                        if str(exc) in ("publication_fenced_out", "worker_state_fenced_out"):
                            fenced.append(exc)
                            return
                        result["funding_error"] = _reason(exc)
                        if isinstance(exc, AdmissionDenied): funding_disabled = True
                        if str(exc) == "worker_source_budget_exhausted":
                            result.pop("funding_error",None)
                            result["live_stopped"]="time_budget"
                            return
                    except Exception:
                        result["funding_error"]="live_transport_failed"
                        funding_disabled=True
                    next_funding = started_tick + FUNDING_PERIOD_MS / 1000
                due=started_tick+PRICE_PERIOD_MS/1000
                if not funding_disabled: due=min(due,next_funding)
                stop_ticks.wait(max(0.05,due-d["now"]()))

        def start_ticks():
            if mode=="all" and env.get("PULSE_RELEASE_TOP350")=="1":return  # ADR-0173: independent live publisher owns these pointers.
            if tick_thread[0] is None and universe is not None and d.get("ticks", True):
                tick_thread[0] = threading.Thread(target=price_loop, name="pulse-price-ticks", daemon=True)
                tick_thread[0].start()

        initialised = False
        cycle = 0
        hot_seen=set()
        while True:
            cycle += 1
            bulk = info = premium = None
            records = None
            stop = False
            try:
                if not initialised:
                    sources.initialise()
                    try:
                        universe = d["load_registry"](client, run_id, mapping_version, now_ms=sources.clock())
                    except RuntimeError as exc:
                        if str(exc) != "registry_unavailable":
                            raise
                        # The sealed registry (cohort, native funding intervals) is valid for 24 h. When it has lapsed the
                        # pass rebuilds it from the sources with the whole budget; collection resumes on the next pass.
                        sources.deadline = deadline - 4
                        value = d["bootstrap_registry"](client, run_id, sources)
                        if value.get("status")=="incomplete":
                            result["registry"]="building"
                            result["registry_completed"]=value["completed"]
                            result["registry_total"]=value["total"]
                            raise _EndPass()
                        if value.get("mapping_version") != mapping_version:
                            raise ValueError("registry_mapping_conflict")   # an interval changed: the configured mapping must be updated
                        universe = value["universe"]
                        result["registry"] = "rebuilt"
                        initialised = True
                        raise _EndPass()
                    initialised = True
                start_ticks()   # the previous pass's price pointer may be close to expiry, so ticking starts at once
                stage = "shared_inputs"
                bulk = d["fetch_bulk"](sources)
                history_paused=False
                try:info, premium = d["fetch_metadata"](sources)
                except AdmissionDenied as exc:
                    if (mode!="all" or exc.admission_reason!="budget_exhausted:1"
                            or getattr(exc,"denied_source",None)!="binance"):raise
                    # ADR-0157: absence is explicit; never manufacture fresh funding metadata.
                    info,premium=({"data":{},"source_ms":0},{"data":{},"source_ms":0})
                    history_paused=True
                records = state.read_many([m["symbol"] for m in universe])
                if mode=="all":
                    # ADR-0133: independent overdue jobs replace all hot-first cohort phases.
                    from .all_pair_collect import run_due
                    stage="all_pair_collect"
                    records,scheduled=run_due(client,data_run,universe,records,state,publisher,sources,
                        new_sources,bulk,info,premium,mapping_version,now=d["now"],
                        deadline=sources.deadline,functions=d,history_paused=history_paused)
                    result.update(scheduled)
                    raise _EndPass()
                # ADR-0126: historical samples are independent of current fields; publish before slow work can starve them.
                try:
                    history_started=d["now"]()
                    history_now=sources.clock()
                    prior_history=d["Document"](client,run_id,"history:0",mapping_version).read(now_ms=history_now)
                    history_due=(prior_history.get("status")!="available" or
                        history_now-prior_history.get("source_ms",0)>=90000)
                    histories=(d["prepare_histories"](client,run_id,universe,records,publisher,now_ms=history_now)
                        if history_due and history_started<deadline-2 else [])
                    if histories:
                        expected={p.channel:d["Document"](client,run_id,p.channel,mapping_version).read(now_ms=sources.clock())["revision"] for p in histories}
                        publisher.publish(histories,expected_revisions=expected)
                        result["history_publishes"]=len(histories)
                    result["history_elapsed_ms"]=int((d["now"]()-history_started)*1000)
                except (ValueError,RuntimeError) as exc:
                    if str(exc) in ("publication_fenced_out","worker_state_fenced_out"):raise
                    result["history_error"]=_reason(exc)
                if d.get("parallel",True):
                    stage="hot_collect"
                    phase_start=d["now"]()
                    prior_hot_times={s:(r or {}).get("last_hot_ms") for s,r in records.items()}
                    def do_hot(meta,source,prior):
                        record=d["collect_hot"](client,run_id,{**meta,"mapping_version":mapping_version},source,prior)
                        state.write(meta["symbol"],record)
                        return record
                    hot=collect_batch(sorted(universe,key=lambda m:(records[m["symbol"]] or {}).get("last_hot_ms",0)),
                        records,collect=do_hot,make_source=lambda:new_sources(sources.deadline),
                        check_lease=publisher.renew,timestamp_field="last_hot_ms",refresh_ms=0 if cycle==1 else 45000)
                    records=hot["records"]
                    hot_seen.update(hot["collected_symbols"])
                    result["hot_collected"]=len(hot_seen);result["hot_skipped"]=hot["skipped"]
                    result["hot_elapsed_ms"]=result.get("hot_elapsed_ms",0)+int((d["now"]()-phase_start)*1000)
                    gaps=[r["last_hot_ms"]-prior_hot_times[s] for s,r in records.items()
                          if r and type(prior_hot_times[s]) is int and type(r.get("last_hot_ms")) is int
                          and r["last_hot_ms"]>prior_hot_times[s]]
                    if gaps: result["hot_poll_gap_max_ms"]=max(gaps)
                    result["metric_source_dispatches"]=result.get("metric_source_dispatches",0)+hot["source_dispatches"]
                    result["failed"].update(hot["failed"])
                    observations=[r["oi_source_ms"] for r in records.values() if r and type(r.get("oi_source_ms")) is int]
                    if observations:
                        result["oldest_oi_source_ms"]=min(observations)
                        result["newest_oi_source_ms"]=max(observations)
                    if hot["stopped"]:
                        result["stopped"]=hot["stopped"]
                        for key in ("transport_error_type","transport_endpoint","source_timeout_ms","source_elapsed_ms"):
                            if key in hot: result[key]=hot[key]
                        raise _EndPass()
                    stage="detail_collect"
                    detail_now=sources.clock()
                    due_detail=sorted([m for m in universe if detail_now-(records[m["symbol"]] or {}).get("detail_refreshed_ms",0)>=300000],
                        key=lambda m:(records[m["symbol"]] or {}).get("detail_refreshed_ms",0))
                    def do_detail(meta,source,prior):
                        record=d["collect_detail"](client,run_id,meta,source,prior)
                        state.write(meta["symbol"],record)
                        return record
                    details=collect_batch(due_detail,records,collect=do_detail,make_source=lambda:new_sources(sources.deadline),
                        check_lease=publisher.renew,refresh_ms=0)
                    records=details["records"]
                    result["detail_collected"]=details["collected"]
                    result["metric_source_dispatches"]+=details["source_dispatches"]
                    result["failed"].update(details["failed"])
                    if details["stopped"]:
                        result["stopped"]=details["stopped"]
                        raise _EndPass()
                    stage="reference_repair"
                    reference_now=sources.clock()
                    due_references=sorted([m for m in universe if not (records[m["symbol"]] or {}).get("opens") or reference_now>=(records[m["symbol"]] or {}).get("references_prefetch_until_ms",0)-120000],
                        key=lambda m:((records[m["symbol"]] or {}).get("references_prefetch_until_ms",0),m["symbol"]))
                    def do_references(meta,source,prior):
                        record=d["collect_references"](meta,source,bulk,prior)
                        state.write(meta["symbol"],record)
                        return record
                    repaired=collect_batch(due_references,records,collect=do_references,make_source=lambda:new_sources(sources.deadline),
                        check_lease=publisher.renew,refresh_ms=0)
                    records=repaired["records"]
                    result["references_repaired"]=repaired["collected"]
                    result["metric_source_dispatches"]+=repaired["source_dispatches"]
                    result["failed"].update(repaired["failed"])
                    if repaired["stopped"]:
                        result["stopped"]=repaired["stopped"]
                        raise _EndPass()
                    stage="funding_collect"
                    funding_start=d["now"]();funding_now=sources.clock()
                    before_funding={s:(r or {}).get("funding_refreshed_ms",0) for s,r in records.items()}
                    due_funding=[m for m in universe if not (records[m["symbol"]] or {}).get("funding_report")
                        or not (records[m["symbol"]] or {}).get("funding_history")
                        or (records[m["symbol"]] or {}).get("nft")!=(premium["data"].get(m["symbol"],{})).get("nextFundingTime")
                        or funding_now-before_funding[m["symbol"]]>=900000]
                    due_funding=[m for m in due_funding if funding_now-(records[m["symbol"]] or {}).get("funding_attempt_ms",0)>=300000
                        or (records[m["symbol"]] or {}).get("funding_attempt_nft")!=premium["data"].get(m["symbol"],{}).get("nextFundingTime")]
                    def do_funding(meta,source,prior):
                        record=d["collect_funding"](client,run_id,{**meta,"mapping_version":mapping_version},source,bulk,info,premium,prior)
                        state.write(meta["symbol"],record)
                        return record
                    funds=collect_batch(due_funding,records,collect=do_funding,make_source=lambda:new_sources(sources.deadline),
                        check_lease=publisher.renew,refresh_ms=0)
                    records=funds["records"]
                    result["funding_checked"]=result.get("funding_checked",0)+funds["collected"]
                    result["funding_updated"]=result.get("funding_updated",0)+sum(
                        (r or {}).get("funding_refreshed_ms",0)>before_funding[s] for s,r in records.items())
                    result["funding_elapsed_ms"]=result.get("funding_elapsed_ms",0)+int((d["now"]()-funding_start)*1000)
                    result["metric_source_dispatches"]+=funds["source_dispatches"]
                    result["failed"].update(funds["failed"])
                    if funds["stopped"]:
                        result["stopped"]=funds["stopped"]
                        raise _EndPass()
                    stage="ls_tip_collect"
                    tip_start=d["now"]()
                    def do_tip(meta,source,prior):
                        record=d["collect_ls_tip"](client,run_id,{**meta,"mapping_version":mapping_version},source,prior,periods=True)
                        state.write(meta["symbol"],record)
                        return record
                    tips=collect_batch(universe,records,collect=do_tip,make_source=lambda:new_sources(sources.deadline),
                        check_lease=publisher.renew,timestamp_field="ls_windows_refreshed_ms",refresh_ms=300000)
                    records=tips["records"]
                    result["ls_tip_collected"]=tips["collected"];result["ls_tip_skipped"]=tips["skipped"]
                    result["ls_tip_elapsed_ms"]=int((d["now"]()-tip_start)*1000)
                    result["metric_source_dispatches"]+=tips["source_dispatches"]
                    result["failed"].update(tips["failed"])
                    if tips["stopped"]:
                        result["stopped"]=tips["stopped"]
                        raise _EndPass()
                # least recently collected first, so a short pass still advances the whole cohort
                order = sorted(universe, key=lambda m: (records[m["symbol"]] or {}).get("last_collected_ms", 0))
                stage = "collect"
                if d.get("parallel",True):
                    def do_pair(meta,source,prior):
                        record=d["collect"](client,run_id,{**meta,"mapping_version":mapping_version},source,bulk,info,premium,prior,funding_already_checked=True)
                        state.write(meta["symbol"],record)
                        return record
                    batch=collect_batch(order,records,collect=do_pair,
                        make_source=lambda:new_sources(sources.deadline),check_lease=publisher.renew)
                    records=batch["records"]
                    result["collected"]+=batch["collected"]
                    result["skipped"]+=batch["skipped"]
                    result["failed"].update(batch["failed"])
                    result["metric_source_dispatches"]=result.get("metric_source_dispatches",0)+batch["source_dispatches"]
                    result["stopped"]=batch["stopped"]
                    for key in ("transport_error_type","transport_endpoint","source_timeout_ms","source_elapsed_ms"):
                        if key in batch: result[key]=batch[key]
                    stop=bool(batch["stopped"] and batch["stopped"]!="time_budget")
                    order=[]
                for meta in order:
                    if fenced: raise fenced[0]
                    prior = records[meta["symbol"]]
                    if prior and sources.clock() - prior.get("last_collected_ms", 0) < PAIR_REFRESH_MS:
                        result["skipped"] += 1
                        continue
                    try:
                        record = d["collect"](client, run_id, {**meta, "mapping_version": mapping_version}, sources, bulk, info, premium, prior)
                        state.write(meta["symbol"], record)
                        records[meta["symbol"]] = record
                        result["collected"] += 1
                    except AdmissionDenied as exc:
                        result["stopped"], stop = _reason(exc), True
                        break
                    except RuntimeError as exc:
                        if str(exc) == "worker_source_budget_exhausted":
                            result["stopped"] = "time_budget"
                            break
                        if str(exc) in ("publication_fenced_out", "worker_state_fenced_out"):
                            raise
                        result["failed"][meta["symbol"]] = str(exc)[:80]
                    except (ValueError, KeyError, TypeError) as exc:
                        # one pair's bad source never marks it complete and never stops the others
                        result["failed"][meta["symbol"]] = str(exc)[:80]
            except _EndPass:
                stop = True
            except (AdmissionDenied, RuntimeError, ValueError) as exc:
                if str(exc) in ("publication_fenced_out", "worker_state_fenced_out"):
                    raise  # a stale owner must stop at once, never publish
                result["stopped"], stop = _reason(exc), True
            # compose only when this cycle holds the shared inputs; an earlier failure leaves the last good pointers untouched
            if mode!="all" and bulk is not None and info is not None and records is not None:
                stage = "compose"
                publisher.renew()
                now_ms = sources.clock()
                packages, status = d["prepare"](client, run_id, universe, state, publisher, bulk, info, premium, now_ms=now_ms, cached=records)
                result.update(status)
                if packages:
                    stage = "promote"
                    expected = {p.channel: d["Document"](client, run_id, p.channel, mapping_version).read(now_ms=now_ms)["revision"] for p in packages}
                    outcome = publisher.publish(packages, expected_revisions=expected)
                    result.update(status="published", channels=outcome["channels"], artifacts=outcome["artifacts"])
                    result["publishes"] += 1
            result["cycles"] = cycle
            remaining = deadline - d["now"]()
            if (stop or result["status"] != "published" or (max_cycles is not None and cycle >= max_cycles)
                    or remaining < PUBLISH_RESERVE_S + LOOP_MARGIN_S):
                break
            # later cycles collect only what is due, in a short window, and publish again
            sources.deadline = d["now"]() + CYCLE_WINDOW_S
            result["stopped"] = None
        # ADR-0112: hand off immediately after publication, not after an artificial idle tail.
        if tick_thread[0] is not None:
            if mode=="all" and result.get("stopped") in (None,"time_budget"):
                # ADR-0134: a minute cron needs the fast lane through its bounded final window.
                while tick_thread[0].is_alive() and d["now"]()<deadline-4 and not fenced:
                    tick_thread[0].join(timeout=min(1,max(0,deadline-4-d["now"]())))
            stop_ticks.set()
            tick_thread[0].join(timeout=8)
            if fenced: raise fenced[0]
        result["shared_source_dispatches"] = sources.calls
        result["live_source_dispatches"] = sum(s.calls for s in tick_sources_used)
        result["source_dispatches"] = sources.calls+result.get("metric_source_dispatches",0)+result["live_source_dispatches"]
        result["elapsed_ms"] = int((d["now"]() - started) * 1000)
        if mode=="all":
            result["state_io"]=measured_io(state)
            result["region"]=env.get("VERCEL_REGION","unknown")
            memory=client.info("memory")
            result["memory"]={k:memory.get(k) for k in
                ("used_memory","used_memory_rss","mem_fragmentation_ratio","used_memory_peak")}
            admission=getattr(sources,"binance",None)
            if admission is not None:
                totals=client.hmget(admission.keys[0],["total:1","total:2"])
                result["quota"]={"history_charged":int(totals[0] or 0),
                    "weight_charged":int(totals[1] or 0),"history_cap":admission.limits["history"].cap}
            if dcx12:
                ledger=sources.dcx.ledger
                totals=client.hmget(ledger.keys[0],["total:1","total:2"])
                result["quota"].update(coindcx_minute_charged=int(totals[0] or 0),
                    coindcx_second_charged=int(totals[1] or 0),coindcx_minute_cap=720,
                    coindcx_second_cap=12,coindcx_transition_us=getattr(ledger,"transition_us",None))
        if result["publishes"] == 0:
            result["status"] = "incomplete"
        return result
    except Exception as exc:
        exc.worker_stage = locals().get("stage", "initialise")
        raise
    finally:
        # ADR-0106: stop and drain the live thread even if compose/promotion raises.
        primary = sys.exception()
        if "stop_ticks" in locals():
            stop_ticks.set()
            if tick_thread[0] is not None:
                tick_thread[0].join(timeout=10)
        try:
            publisher.release()
        except Exception:
            if primary is None:
                raise
        finally:
            try:
                client.close()
            except Exception:
                pass
