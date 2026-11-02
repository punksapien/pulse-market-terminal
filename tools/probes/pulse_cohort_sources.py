"""ADR-0068 fresh, admitted public source manifest; Preview control state only."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_real_ls import header_refresh_needed
from pulse.collectors.binance_transport import fetch
from pulse.collectors.cohort import select
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.shared_admission import RollingLimit


def write(path, data):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, default=str, indent=2)+"\n")
    temporary.replace(path)


def run(cli, ca_file, output):
    client, identity = connect_staging(cli, approved_plaintext=True)
    result = {"status": "running", "production_access": False, "deletions": 0, "calls": []}
    admission = BinanceAdmission(client, "operator-office-c1-20261026",
        {"history": RollingLimit(700, 300000), "weight": RollingLimit(1680, 60000)},
        approved_plan_fallback=True, approved_bootstrap=True)
    last_dispatch = 0
    last_weighted = None
    def request(path, parameters, *, host="https://fapi.binance.com", weight=0):
        nonlocal last_dispatch, last_weighted
        seconds, micros = client.time()
        if weight == 0 and header_refresh_needed(last_weighted, seconds*1000+micros//1000):
            request("/fapi/v1/openInterest", {"symbol": "BTCUSDT"}, weight=1)
        # Discovery is serial and <=5 requests/s across both providers.
        delay = .2-(time.monotonic()-last_dispatch)
        if delay > 0:
            time.sleep(delay)
        last_dispatch = time.monotonic()
        result["pending_request"] = {"path": path, "parameters": parameters, "host": host}
        write(output, result)
        body = fetch(path, parameters, admission, {"history": int(weight == 0), "weight": weight},
                     ca_file, max_response=4*1024*1024, host=host, decimal_numbers=True)
        if weight:
            last_weighted = dict(admission.last_feedback)
        result["calls"].append({"path": path, "parameters": parameters, "host": host,
            "feedback": dict(admission.last_feedback), "sha256": hashlib.sha256(
                json.dumps(body, default=str, sort_keys=True).encode()).hexdigest()})
        write(output, result)
        return body
    try:
        request("/fapi/v1/openInterest", {"symbol": "BTCUSDT"}, weight=1)
        exchange = request("/fapi/v1/exchangeInfo", {}, weight=1)
        active = request("/exchange/v1/derivatives/futures/data/active_instruments",
                         {"margin_currency_short_name[]": "USDT"}, host="https://api.coindcx.com")
        ticker = request("/market_data/v3/current_prices/futures/rt", {}, host="https://public.coindcx.com")
        seconds, micros = client.time()
        manifest = select(active, ticker, exchange, now_ms=seconds*1000+micros//1000)
        result["manifest"] = manifest
        result["source_shapes"] = {"active": len(active), "exchange": len(exchange["symbols"]),
                                    "ticker_keys": list(ticker)[:5]}
        for pair in manifest["pairs"]:
            raw = request("/exchange/v1/derivatives/futures/data/instrument",
                {"pair": pair["pair_id"], "margin_currency_short_name": "USDT"},
                host="https://api.coindcx.com")
            instrument = raw.get("instrument", raw)
            if instrument.get("pair") != pair["pair_id"]:
                raise ValueError("cohort_instrument_identity")
            pair["funding_hours"] = instrument.get("funding_frequency")
            hours = pair["funding_hours"]
            if isinstance(hours, (str, Decimal)) and Decimal(hours).is_finite() and Decimal(hours) == int(Decimal(hours)):
                pair["funding_hours"] = int(Decimal(hours))
            pair["coindcx_instrument"] = instrument
            write(output, result)
        intervals = {row["symbol"]: row["funding_hours"] for row in manifest["pairs"]}
        if intervals["BTCUSDT"] != 8 or sum(intervals.get(s) == 1 for s in
                ("ARKUSDT", "BMTUSDT", "ERAUSDT", "IOSTUSDT", "IRYSUSDT", "ORDERUSDT", "TUSDT")) < 3:
            raise ValueError("cohort_current_interval_coverage_missing")
        if any(type(h) is not int or h not in (1, 4, 8) for h in intervals.values()):
            raise ValueError("cohort_interval_unsupported")
        manifest.pop("digest")
        manifest["digest"] = hashlib.sha256(json.dumps(manifest, default=str, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest()
        result.update(status="manifest_verified", memory=client.info("memory"),
                      transport=identity["transport"], quota_keys=admission.keys)
    except Exception as error:
        result.update(status="stopped", reason=str(error) if isinstance(error, (ValueError, RuntimeError))
                      else type(error).__name__, admission_reason=getattr(error, "admission_reason", None),
                      failure_feedback=admission.last_feedback)
    finally:
        write(output, result)
        client.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.parent != ROOT/"docs/evidence" or output.exists():
        raise SystemExit("invalid_evidence_path")
    result = run(args.vercel_cli, args.ca_file, output)
    print(json.dumps({"status": result["status"], "reason": result.get("reason"),
                      "calls": len(result["calls"]), "admission_reason": result.get("admission_reason")}))
    if result["status"] != "manifest_verified":
        raise SystemExit(1)
