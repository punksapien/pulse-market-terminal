"""ADR-0056: exactly one source call on the verified isolated Preview DB."""
import argparse
import hashlib
import json
import struct
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse.collectors.one_pair_oi import fetch_current_oi
from pulse.store.one_pair_oi import OnePairStore, RING_BYTES, CAPSULE_BYTES
from pulse.store.bucket_ring import BucketRing
from preview_redis_access import connect,state


def run(client,ca_file):
    before=state(client)
    if before["used_memory_bytes"]+100000>=21000000:
        raise RuntimeError("c1_memory_gate_denied")
    source_calls=0
    store=OnePairStore(client)
    store.acquire()
    store.initialise()
    initial=store.read()
    if initial["reason"]!="no_observation":
        raise RuntimeError("c1_empty_ring_not_unavailable")
    def reserve():
        nonlocal source_calls
        if not store.reserve_source():
            return False
        source_calls+=1
        return True
    started=time.perf_counter_ns()
    point=fetch_current_oi(reserve=reserve,ca_file=ca_file)
    source_ms=(time.perf_counter_ns()-started)/1e6
    started=time.perf_counter_ns()
    result=store.write(point)
    write_ms=(time.perf_counter_ns()-started)/1e6
    if result[0]!=b"written":
        raise RuntimeError("c1_live_write_refused_"+result[0].decode("ascii"))
    server_ms=int(result[1])
    frame=client.get(store.frame)  # Evidence only; runtime read uses bounded ranges.
    oracle=BucketRing(60000,1536)
    oracle.write(point.source_ms,(point.quantity,),now_ms=server_ms)
    if frame[:RING_BYTES]!=oracle.to_bytes() or len(frame)!=CAPSULE_BYTES:
        raise RuntimeError("c1_live_ring_python_parity_failed")
    read=store.read()
    if read["status"]!="available" or read["source_ms"]!=point.source_ms or read["source_quantity"]!=point.source_quantity or read["quantity"]!=point.quantity or read["version"]!=1:
        raise RuntimeError("c1_derived_inconsistent")
    if int.from_bytes(frame[RING_BYTES+128:],"little")!=1:
        raise RuntimeError("c1_coverage_inconsistent")
    if frame[RING_BYTES+9]!=1:
        raise RuntimeError("c1_dirty_not_marked")
    if store.write(point)[0]!=b"duplicate" or client.get(store.frame)!=frame:
        raise RuntimeError("c1_retry_not_idempotent")
    future=replace(point,source_ms=point.source_ms+10**12)
    if store.write(future)[0]!=b"future_timestamp" or client.get(store.frame)!=frame:
        raise RuntimeError("c1_future_mutated_state")
    if store.reserve_source():
        raise RuntimeError("c1_source_budget_not_bounded")
    # A fault clone is never an advertised metric. No existing keys are removed.
    clone=store.prefix+"partial-fault"
    damaged=bytearray(frame)
    damaged[RING_BYTES+8]=ord("W")
    client.set(clone,bytes(damaged),ex=900)
    if store.read(frame=clone)["reason"]!="quarantined":
        raise RuntimeError("c1_partial_publish_not_refused")
    if store.write(point,frame=clone)[0]!=b"partial_write_quarantined" or client.get(clone)!=bytes(damaged):
        raise RuntimeError("c1_partial_frame_mutated")
    old_owner=store.owner
    client.pexpire(store.lease,1)
    time.sleep(.05)
    store.acquire()
    if store.write(point,owner=old_owner)[0]!=b"fenced_out" or client.get(store.frame)!=frame:
        raise RuntimeError("c1_old_owner_mutated_state")
    if store.write(point)[0]!=b"duplicate":
        raise RuntimeError("c1_new_owner_retry_failed")
    for _ in range(3):
        if store.read()!=read:
            raise RuntimeError("c1_read_inconsistent")
    return {"execution":"operator_to_isolated_preview_redis_not_hosted_vercel",
            "symbol":"BTCUSDT","metric":"open_interest_quantity","source_calls":source_calls,
            "source_fetch_ms":source_ms,"write_roundtrip_ms":write_ms,"source_age_at_commit_ms":server_ms-point.source_ms,
            "derived":read,"ring_bytes":RING_BYTES,"capsule_bytes":CAPSULE_BYTES,
            "ring_sha256":hashlib.sha256(frame[:RING_BYTES]).hexdigest(),"mapping_version":"btc-oi-quantity-v1",
            "python_ring_byte_parity":True,"derived_coverage_dirty_consistent":True,
            "retry_idempotent":True,"future_refused_without_mutation":True,
            "partial_frame_refused":True,"old_owner_refused":True,"replacement_owner_retry":True,
            "read_source_calls":0,"key_ttl_max_s":900,"new_keys_max":5,"new_payload_max_bytes":100000,
            "before":before,"after":state(client),
            "not_checked":["hosted_bom1_collector","60s_cadence","actual_process_kill","global_quota_and_memory_admission","OI_change_history","50_pairs"]}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--ca-file")
    args=parser.parse_args()
    client=None
    try:
        client=connect(args.vercel_cli)
        print(json.dumps(run(client,args.ca_file),indent=2))
    except Exception as error:
        reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__
        print(json.dumps({"status":"failed","reason":reason,"production_accessed":False,"not_checked":"one-pair success not established"}))
        raise SystemExit(1) from None
    finally:
        if client is not None:
            client.close()
