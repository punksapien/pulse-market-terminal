"""Operator run of the Preview worker pass from this machine (same code as api/pulse_worker.py).
Identity-gated Preview Redis, operator egress scope, staging only. Repeats until published or --runs is spent."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pulse_staging_gate import connect_staging
from pulse.collectors import preview_runtime
from pulse.collectors.preview_sources import Sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vercel-cli", required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--pause", type=float, default=5.0)
    args = parser.parse_args()
    for n in range(args.runs):
        client, _ = connect_staging(args.vercel_cli, approved_plaintext=True)
        deps = dict(connect=lambda env: client,
                    Sources=lambda c, scope, deadline: Sources(c, scope="operator-preview-c1", deadline=deadline, ca_file=args.ca_file))
        try:
            result = preview_runtime.run(run_id=args.run_id, mapping_version=args.mapping, duration_ms=55000, env={}, deps=deps)
        except Exception as exc:
            result = {"status": "failed", "error_type": type(exc).__name__, "reason": str(exc)[:120]}
        print(json.dumps({"run": n + 1, **result}), flush=True)
        if result.get("status") in ("published",) or result.get("status") == "failed":
            break
        time.sleep(args.pause)


if __name__ == "__main__":
    main()
