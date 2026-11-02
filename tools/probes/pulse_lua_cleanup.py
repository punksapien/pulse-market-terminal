"""Exact synthetic timing manifest only, using unchanged ADR-0063 deletion gate."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import pulse_cleanup_manifest as cleanup
from pulse_cohort_sources import write

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.parent!=ROOT/"docs/evidence" or output.exists():raise SystemExit("invalid_evidence_path")
    name="pulse_lua_budget_2026-10-26.json"
    evidence=json.loads((ROOT/"docs/evidence"/name).read_text())
    if evidence.get("synthetic") is not True or any(not any(marker in key for marker in
        (":B-BENCH_USDT:",":timing-cursor:","{quota-timing-")) for key in evidence["keys"]):
        raise SystemExit("synthetic_manifest_refused")
    cleanup.FILES=(name,)
    result=cleanup.run(args.vercel_cli,apply=True,approved_plaintext=True)
    write(output,result)
    print(json.dumps({"status":result["status"],"deleted":result.get("deleted"),"missing":result.get("missing")}))
    if result["status"]!="cleanup_complete":raise SystemExit(1)
