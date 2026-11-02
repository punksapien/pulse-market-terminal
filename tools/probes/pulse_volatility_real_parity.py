"""Recorded real-candle v1 parity on all14pairs per resolution; zero network."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from pulse.metrics.volatility import evaluate_v1
from pulse.legacy.scoring import LWConfig
from pulse.legacy.vol_core import VolatilityConfig


def compare(actual, expected):
    mismatches, values = [], 0
    if actual.keys() != expected.keys():
        mismatches.append("row_keys")
    for key in actual.keys() & expected.keys():
        if actual[key].keys() != expected[key].keys():
            mismatches.append(key+":field_keys")
        for field in actual[key].keys() & expected[key].keys():
            got, want = actual[key][field], expected[key][field]
            values += 1
            if isinstance(want, float):
                equal = isinstance(got, (int, float)) and (
                    math.isclose(got, want, rel_tol=1e-12) or math.isnan(want) and math.isnan(got))
            else:
                equal = type(got) is type(want) and got == want
            if not equal:
                mismatches.append(key+":"+field)
    return values, mismatches


def run(fixtures):
    hashes = {}
    for line in (fixtures/"candles.sha256").read_text().splitlines():
        digest, filename = line.split()
        if Path(filename).name != filename:
            raise ValueError("invalid_fixture_path")
        actual = hashlib.sha256((fixtures/"candles"/filename).read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError("fixture_checksum_mismatch")
        hashes[filename] = digest
    if len(hashes) != 42:
        raise ValueError("fixture_count_mismatch")
    rows, timings = {}, {}
    for resolution, horizon in (("60", "1H"), ("240", "4H"), ("1D", "24H")):
        candles = {}
        for filename in sorted(hashes):
            if filename.endswith("_"+resolution+"_f.csv"):
                pair = re.match(r"(B-[A-Z0-9]+_USDT)_", filename).group(1)
                candles[pair] = pd.read_csv(fixtures/"candles"/filename)
        if len(candles) != 14:
            raise ValueError("cross_section_count_mismatch")
        config = LWConfig(base=VolatilityConfig(resolution=resolution), scale_horizon=horizon)
        started = time.perf_counter()
        _, summary, _ = evaluate_v1(candles, config)
        timings[resolution] = time.perf_counter()-started
        for row in summary:
            rows[resolution+":"+row["instrument"]] = {k:v for k,v in row.items()
                if isinstance(v, (int, float, str, bool, type(None)))}
    expected = json.loads((fixtures/"expected_v1_pandas3.0.6_numpy2.5.4_py3.14.9.json").read_text())
    values, mismatches = compare(rows, expected["rows"])
    return {"status": "passed" if not mismatches else "failed", "python": sys.version.split()[0],
            "numpy": np.__version__, "pandas": pd.__version__, "reference_versions": {
                k:expected[k] for k in ("python", "numpy", "pandas")},
            "rows": len(rows), "compared_values": values, "mismatches": mismatches,
            "source_checksums": hashes, "timings_seconds": timings,
            "exchange_calls": 0, "redis_access": False, "rel_tol": 1e-12}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.parent != ROOT/"docs/evidence" or output.exists():
        raise SystemExit("invalid_evidence_path")
    result = run(Path(args.fixtures))
    output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k != "source_checksums"}))
    if result["status"] != "passed":
        raise SystemExit(1)
