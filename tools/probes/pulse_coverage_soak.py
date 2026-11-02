"""Bounded read-only coverage soak; failures remain evidence, never become a pass."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def summarize(report):
    fields=report["all_fields"]
    gaps={name:{"fresh":value["fresh_at_response"],"rows":value["rows"],
                "reasons":value["reasons"]} for name,value in fields.items()
          if value["fresh_at_response"]!=value["rows"]}
    return dict(horizon=report["horizon"],generation=report["generation"],
        pairs=report["pairs"],field_groups=len(fields),gaps=gaps,
        live_returns=report["live_returns"],references=report["references"],
        status="coverage_complete" if fields and not gaps and report["pairs"]==350 else "coverage_gaps")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url",required=True);p.add_argument("--vercel-cli",required=True)
    p.add_argument("--ca-file",required=True);p.add_argument("--out-dir",required=True)
    p.add_argument("--minutes",type=int,default=30);p.add_argument("--interval",type=int,default=120)
    a=p.parse_args()
    if not 1<=a.minutes<=120 or not 60<=a.interval<=300:p.error("bounded duration/interval required")
    out=Path(a.out_dir);out.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();deadline=started+a.minutes*60;cycle=0
    while time.monotonic()<deadline:
        target=started+cycle*a.interval
        while time.monotonic()<min(target,deadline):time.sleep(min(1,min(target,deadline)-time.monotonic()))
        if time.monotonic()>=deadline:break
        for h in ("1H","4H","24H"):
            cmd=[sys.executable,str(Path(__file__).with_name("pulse_snapshot_audit.py")),
                "--url",a.url,"--vercel-cli",a.vercel_cli,"--ca-file",a.ca_file,"--horizon",h]
            try:
                r=subprocess.run(cmd,capture_output=True,text=True,timeout=90)
                if r.returncode:raise ValueError("audit_failed")
                report=json.loads(r.stdout)
                (out/f"{cycle:03d}-{h}.json").write_text(json.dumps(report,indent=2)+"\n")
                summary=summarize(report)
                print(json.dumps(dict(cycle=cycle,elapsed_s=round(time.monotonic()-started),**summary)),flush=True)
            except (subprocess.TimeoutExpired,ValueError,KeyError):
                print(json.dumps(dict(cycle=cycle,horizon=h,status="audit_failed")),flush=True)
        cycle+=1
    print(json.dumps(dict(status="observation_complete_not_acceptance",cycles=cycle,
        elapsed_s=round(time.monotonic()-started))),flush=True)


if __name__=="__main__":main()
