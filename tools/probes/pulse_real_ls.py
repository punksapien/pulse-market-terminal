"""ADR-0065 bounded real L/S canaries on new Pulse staging only; no publication."""
import argparse
from dataclasses import asdict
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_real_oi import ensure_lease
from pulse.collectors.binance_ls import fetch_history, history_spec
from pulse.collectors.binance_oi import fetch_current
from pulse.metrics.long_short import WINDOW_MS, SERIES_TTL_MS
from pulse.store.ls_reader import read_metrics
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.bucket_ring import BucketRing
from pulse.store.capsule_model import CapsuleSpec
from pulse.store.metric_capsule import MetricCapsule
from pulse.store.shared_admission import RollingLimit
from pulse.store.expansion_guard import guard

CANARIES = ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "1000PEPEUSDT", "XAUUSDT")


def header_refresh_needed(feedback, now_ms):
    if feedback is None or feedback.get("status") != 200 or feedback.get("used_weight_1m", -1) < 1:
        return True
    stamp = feedback.get("provider_date_ms", -1)
    # Advisory only: Lua still performs the authoritative check before charging.
    return stamp < 0 or stamp//60000 != now_ms//60000 or now_ms-stamp >= 55000


def raw_reference(raw, *, now_ms):
    """Independent raw-row selection and 60-digit arithmetic; no metric helpers."""
    result = {"series": {}}
    for series, rows in raw.items():
        latest = max(rows, key=lambda r: int(r["timestamp"]), default=None)
        current = latest if latest and now_ms < int(latest["timestamp"]) + SERIES_TTL_MS[series] else None
        item = {"ratio": None, "evolution": {}}
        if current:
            item.update(ratio=Decimal(current["longShortRatio"]), asof_ms=int(current["timestamp"]))
        for horizon, window in WINDOW_MS.items():
            selection = None
            if current:
                target = int(current["timestamp"]) - window
                eligible = [r for r in rows if 0 <= target - int(r["timestamp"]) <= 300000]
                selection = max(eligible, key=lambda r: int(r["timestamp"]), default=None)
            value = None
            if selection:
                with localcontext() as context:
                    context.prec = 60
                    value = 100 * (Decimal(current["longAccount"]) - Decimal(selection["longAccount"]))
            item["evolution"][horizon] = {"value_pp": value, "baseline": selection}
        result["series"][series] = item
    global_rows = {int(r["timestamp"]): r for r in raw["global"]}
    top_rows = {int(r["timestamp"]): r for r in raw["top"]}
    common = [t for t in global_rows.keys() & top_rows.keys() if now_ms < t + 900000]
    result["divergence"] = {"value_pp": None}
    if common:
        stamp = max(common)
        with localcontext() as context:
            context.prec = 60
            value = 100 * (Decimal(top_rows[stamp]["longAccount"]) - Decimal(global_rows[stamp]["longAccount"]))
        result["divergence"] = {"value_pp": value, "asof_ms": stamp,
                                "global_row": global_rows[stamp], "top_row": top_rows[stamp]}
    return result


def verify(computed, expected):
    count = 0
    for series in ("global", "top"):
        metric = computed["series"][series]["ratio"]
        reference = expected["series"][series]
        if metric.value != reference["ratio"]:
            raise RuntimeError("ls_raw_ratio_mismatch")
        if metric.value is not None:
            if metric.asof_ms != reference["asof_ms"]:
                raise RuntimeError("ls_raw_ratio_time_mismatch")
            count += 1
        for horizon in WINDOW_MS:
            value = computed["series"][series]["evolution"][horizon]["metric"]
            witness = reference["evolution"][horizon]
            if value.value != witness["value_pp"]:
                raise RuntimeError("ls_raw_evolution_mismatch")
            if value.value is not None:
                if value.comparison_asof_ms != int(witness["baseline"]["timestamp"]):
                    raise RuntimeError("ls_raw_baseline_time_mismatch")
                count += 1
    metric = computed["divergence"]
    if metric.value != expected["divergence"]["value_pp"]:
        raise RuntimeError("ls_raw_divergence_mismatch")
    if metric.value is not None:
        if metric.asof_ms != expected["divergence"]["asof_ms"]:
            raise RuntimeError("ls_raw_divergence_time_mismatch")
        count += 1
    return count


def run(cli, *, symbols, stages, scope, run_id, ca_file, checkpoint):
    client, identity = connect_staging(cli, approved_plaintext=True)
    result = {"status": "running", "execution": "operator_to_staging_not_hosted", "run_id": run_id,
              "scope": scope, "keys": [], "pairs": [], "stages": [], "exchange_attempts": 0,
              "production_access": False, "deletions": 0, "staging_complete": False}
    stores = {}
    def clock():
        seconds, micros = client.time()
        return seconds*1000 + micros//1000
    try:
        result["memory_baseline"] = client.info("memory")
        guard(result["memory_baseline"], approved_plan_fallback=True)
        admission = BinanceAdmission(client, scope,
            {"history": RollingLimit(700, 300000), "weight": RollingLimit(1680, 60000)},
            approved_plan_fallback=True, approved_bootstrap=True)
        result["quota_keys"] = admission.keys
        header_feedback = None
        def refresh_header():
            nonlocal header_feedback
            if not header_refresh_needed(header_feedback, clock()):
                return
            heartbeat_spec = CapsuleSpec("B-BTC_USDT", "BTCUSDT", "oi", "quantity",
                                         60000, 1536, 900000, 900000, True)
            result["exchange_attempts"] += 1
            fetch_current(heartbeat_spec, admission, clock=clock, ca_file=ca_file)
            header_feedback = dict(admission.last_feedback)
            result.setdefault("heartbeats", []).append(header_feedback)
            checkpoint(result)
        for count in stages:
            started = time.perf_counter()
            # Same shared scope; each needed heartbeat is charged, never a reset.
            refresh_header()
            for symbol in symbols[:count]:
                pair = "B-" + symbol[:-4] + "_USDT"
                raw, headers = {}, {}
                if symbol not in stores:
                    stores[symbol] = tuple(MetricCapsule(client, run_id, history_spec(pair, symbol, series),
                                           approved_plan_fallback=True) for series in ("global", "top"))
                    for store in stores[symbol]:
                        result["keys"].extend((store.frame, store.lease, store.fence))
                    checkpoint(result)  # Exact manifest before first allocation/write.
                for store in stores[symbol]:
                    series = store.spec.metric[3:]
                    refresh_header()
                    result["exchange_attempts"] += 1
                    points = fetch_history(store.spec, admission, clock=clock, ca_file=ca_file,
                                           evidence_sink=lambda data, s=series: raw.update({s: data}))
                    headers[series] = dict(admission.last_feedback)
                    ring = BucketRing(store.spec.step_ms, store.spec.capacity, store.spec.columns)
                    validation_ms = clock()
                    for point in points:
                        ring.write(point.source_ms, point.values, now_ms=validation_ms)
                    ring.scrub()
                    ensure_lease(store)
                    for offset in range(0, len(points), 16):
                        store.write_many(points[offset:offset+16])
                now_ms = clock()
                computed = read_metrics(*stores[symbol], now_ms=now_ms)
                # Use one captured observation time for raw and stored expiry semantics.
                expected = raw_reference(raw, now_ms=now_ms)
                verified = verify(computed, expected)
                memory = client.info("memory")
                guard(memory, approved_plan_fallback=True)
                report = {"symbol": symbol, "pair_id": pair, "stage": count, "now_ms": now_ms,
                          "raw": raw, "headers": headers, "computed": {
                              "series": {s: {"ratio": asdict(v["ratio"]), "current": v["current"],
                                  "evolution": {h: {"metric": asdict(e["metric"]), "baseline": e["baseline"]}
                                                for h, e in v["evolution"].items()}}
                                         for s, v in computed["series"].items()},
                              "divergence": asdict(computed["divergence"])},
                          "raw_reference": expected, "verified_available_values": verified,
                          "memory": memory, "ring_ttls": [client.ttl(s.frame) for s in stores[symbol]]}
                result["pairs"].append(report)
                checkpoint(result)
                print(json.dumps({"progress": "ls_pair_verified", "symbol": symbol, "stage": count,
                                  "available_values": verified, "used_memory": memory["used_memory"]}), flush=True)
            memory = client.info("memory")
            guard(memory, approved_plan_fallback=True)
            result["stages"].append({"pairs": count, "elapsed_seconds": time.perf_counter()-started,
                                     "memory": memory, "raw_ring_bytes": sum(s.spec.nbytes
                                         for pair_stores in stores.values() for s in pair_stores)})
            checkpoint(result)
        result.update(status="real_ls_stages_verified", memory_final=client.info("memory"),
                      transport=identity["transport"], capacity_503_pairs="open")
    except Exception as error:
        result.update(status="stopped", reason=str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        if getattr(error, "admission_reason", None):
            result["admission_reason"] = error.admission_reason
        try:
            result["memory_final"] = client.info("memory")
        except Exception:
            result["memory_final"] = {"status": "unavailable"}
    finally:
        checkpoint(result)
        client.close()
    return result


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--scope", default="operator-office-c1-20261026")
    parser.add_argument("--run-id", default=uuid.uuid4().hex)
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    parser.add_argument("--stages", nargs="+", type=int, default=[1])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.output).resolve()
    if (path.parent != ROOT/"docs/evidence" or path.exists()
            or args.scope != "operator-office-c1-20261026"
            or any(s not in CANARIES for s in args.symbols) or len(set(args.symbols)) != len(args.symbols)
            or not args.stages or sorted(set(args.stages)) != args.stages
            or any(n < 1 or n > len(args.symbols) for n in args.stages)):
        raise SystemExit("invalid evidence path/canary stages")
    def checkpoint(result):
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result, default=json_default, indent=2)+"\n")
        temporary.replace(path)
    result = run(args.vercel_cli, symbols=args.symbols, stages=args.stages, scope=args.scope,
                 run_id=args.run_id, ca_file=args.ca_file, checkpoint=checkpoint)
    print(json.dumps({"status": result["status"], "reason": result.get("reason"),
                      "exchange_attempts": result["exchange_attempts"], "file": str(path)}))
    if result["status"] == "stopped":
        raise SystemExit(1)
