"""Read-only controlled8connection diagnostic; no scripts/data writes/exchanges."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse_cohort_sources import write
from pulse.store.expansion_guard import guard


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--cohort",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.parent!=ROOT/"docs/evidence" or output.exists(): raise SystemExit("invalid_evidence_path")
    cohort=json.loads(Path(args.cohort).read_text())
    if cohort.get("reason")!="cohort_503_projection_pressure_stop": raise SystemExit("projection_stop_evidence_required")
    import redis
    client,_=connect_staging(args.vercel_cli,approved_plaintext=True)
    result={"exchange_calls":0,"redis_data_writes":0,"deletions":0,"trials":[]}
    def sample():
        value=client.info("memory")
        guard(value,approved_plan_fallback=True)
        return value
    try:
        for trial in range(2):
            baseline=[sample() for _ in range(3)]
            pool=redis.ConnectionPool(connection_class=client.connection_pool.connection_class,max_connections=8,
                **client.connection_pool.connection_kwargs)
            barrier=threading.Barrier(9)
            release=threading.Event()
            def hold():
                connection=pool.get_connection()
                try:
                    connection.send_command("PING")
                    if connection.read_response()!=b"PONG": raise RuntimeError("ping_refused")
                    barrier.wait(timeout=10)
                    release.wait(timeout=15)
                finally: pool.release(connection)
            with ThreadPoolExecutor(max_workers=8) as executor:
                tasks=[executor.submit(hold) for _ in range(8)]
                barrier.wait(timeout=10)
                warm=[]
                try:
                    for _ in range(3):
                        warm.append(sample()); time.sleep(1)
                finally: release.set()
                for task in tasks: task.result()
            pool.disconnect()
            time.sleep(1)
            after=[sample() for _ in range(3)]
            result["trials"].append({"baseline":baseline,"eight_extra_connections":warm,"after_close":after})
            write(output,result)
        frames=[key for key in cohort["keys"] if key.endswith(":frame")]
        existing=[]
        for key in frames:
            if not key.startswith("pulse:stg:"): raise ValueError("scope_refused")
            size=client.memory_usage(key)
            if size is not None: existing.append({"key":key,"bytes":size})
        result.update(status="diagnostic_complete_not_capacity_approval",existing_frames=existing,
                      existing_frame_bytes=sum(row["bytes"] for row in existing))
    finally:
        write(output,result); client.close()
    print(json.dumps({"status":result["status"],"existing_frames":len(result["existing_frames"]),
                      "existing_frame_bytes":result["existing_frame_bytes"]}))
