"""Offline pinned-schema comparison; no exchange/Redis calls or relaxed checks."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))


def measure(value):
    from jsonschema import Draft202012Validator
    from pulse.wire.validation import validators,snapshot
    validators.cache_clear()
    v=validators()["urn:pulse:schema:v1:snapshot"]
    original=Draft202012Validator(v.schema,registry=v._registry)
    result={}
    for name,fn in (("original_schema",lambda:original.validate(value)),
            ("cached_cold_full",lambda:snapshot(value)),("cached_warm_full",lambda:snapshot(value))):
        started=time.perf_counter();fn();result[name]=round((time.perf_counter()-started)*1000,2)
    return dict(rows=len(value["rows"]),timing_ms=result,
        limits="Local CPU only; cached timings include uncached semantic checks, original is schema only.")


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("snapshot",type=Path,nargs="?",default=ROOT/"docs/contracts/fixtures/snapshot.valid.24H.json")
    a=p.parse_args();print(json.dumps(measure(json.loads(a.snapshot.read_text())),indent=2))
