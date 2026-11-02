"""One fresh diagnostic cycle after operator browser evidence, never retries."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_cohort_sources import write
from pulse.collectors.binance_transport import fetch
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.shared_admission import RollingLimit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.parent != ROOT/"docs/evidence" or output.exists():
        raise SystemExit("invalid_evidence_path")
    client, identity = connect_staging(args.vercel_cli, approved_plaintext=True)
    result = {"production_access": False, "deletions": 0, "status": "running"}
    admission = BinanceAdmission(client, "operator-office-c1-20261026",
        {"history": RollingLimit(700, 300000), "weight": RollingLimit(1680, 60000)},
        approved_plan_fallback=True, approved_bootstrap=True)
    try:
        seconds, micros = client.time()
        now = seconds*1000+micros//1000
        cooldown = client.hget(admission.keys[-1], "open_until")
        result.update(observed_ms=now, prior_cooldown_ms=int(cooldown or 0))
        if int(cooldown or 0) > now:
            raise RuntimeError("existing_circuit_still_open_no_dispatch")
        fetch("/fapi/v1/openInterest", {"symbol": "BTCUSDT"}, admission,
              {"history": 0, "weight": 1}, args.ca_file)
        result["heartbeat"] = dict(admission.last_feedback)
        active = fetch("/exchange/v1/derivatives/futures/data/active_instruments",
            {"margin_currency_short_name[]": "USDT"}, admission, {"history": 1, "weight": 0},
            args.ca_file, host="https://api.coindcx.com")
        if type(active) is not list or any(not isinstance(p, str) for p in active):
            raise ValueError("active_source_shape")
        result.update(status="active_public_request_succeeded", active=active, count=len(active),
                      feedback=dict(admission.last_feedback))
    except Exception as error:
        result.update(status="stopped", reason=str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__,
                      admission_reason=getattr(error, "admission_reason", None),
                      source_path=getattr(error, "source_path", None),
                      source_host=getattr(error, "source_host", None),
                      response_metadata=getattr(error, "response_metadata", None),
                      feedback=admission.last_feedback)
    finally:
        write(output, result)
        client.close()
    print(json.dumps({k:v for k,v in result.items() if k not in ("active", "heartbeat")}))
    if result["status"] == "stopped":
        raise SystemExit(1)
