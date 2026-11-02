"""Offline ADR-0044 evidence. Print JSON; never access Redis/exchanges or write files."""

import gzip
import hashlib
import json
import math
import platform
import random
import statistics
import sys
import subprocess
import time
import tracemalloc
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pulse.store.bucket_ring import BucketRing
from pulse.store.memory_store import MemoryStore


def timing(samples):
    ordered = sorted(samples)
    return {"n": len(samples), "p50_us": round(statistics.median(ordered) / 1000, 3),
            "sample_p95_us": round(ordered[math.ceil(.95 * len(ordered)) - 1] / 1000, 3)}


def operation_benchmark(capacity, ring_class=BucketRing, guarded=True):
    ring = ring_class(60000, capacity)
    writes, reads = [], []
    for i in range(20000):
        start = time.perf_counter_ns()
        if guarded:
            ring.write(i * 60000, (float(i),), now_ms=i * 60000)
        else:
            ring.write(i * 60000, (float(i),))
        writes.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        assert ring.baseline(i * 60000, 60000) == (i * 60000, float(i))
        reads.append(time.perf_counter_ns() - start)
    scrubs = []
    for _ in range(30):
        start = time.perf_counter_ns()
        ring.scrub()
        scrubs.append(time.perf_counter_ns() - start)
    return {"slots": capacity, "packed_bytes": ring.nbytes, "write": timing(writes),
            "lookup": timing(reads), "full_scrub": timing(scrubs)}


def codecs():
    result = [("zlib6", lambda raw: zlib.compress(raw, 6), zlib.decompress),
              ("gzip6", lambda raw: gzip.compress(raw, compresslevel=6, mtime=0), gzip.decompress)]
    try:
        import brotli
    except ImportError:
        return result, None
    for quality in (5, 9, 11):
        result.append((f"br{quality}", lambda raw, q=quality: brotli.compress(raw, quality=q), brotli.decompress))
    return result, getattr(brotli, "__version__", "unknown")


def main():
    available, brotli_version = codecs()
    inputs = {}
    ring = BucketRing(60000, 1536)
    rng = random.Random(44)
    rows = []
    for i in range(1536):
        # Synthetic irregular floats; not a claim about production compressibility.
        row = (1792800000000 + i * 60000, rng.uniform(1, 100000))
        ring.write(row[0], row[1:], now_ms=row[0])
        rows.append(row)
    inputs["synthetic_ring_packed"] = ring.to_bytes()
    inputs["same_synthetic_records_json"] = json.dumps(rows, separators=(",", ":")).encode()
    archived = ROOT / "api/volatility_lw_report_60.json"
    original = archived.read_bytes()
    inputs["archived_legacy_1h_compact"] = json.dumps(json.loads(original), separators=(",", ":"), ensure_ascii=False).encode()
    results = []
    for name, raw in inputs.items():
        for codec, encode, decode in available:
            enc, dec = [], []
            for _ in range(5):
                start = time.perf_counter_ns()
                blob = encode(raw)
                enc.append(time.perf_counter_ns() - start)
                start = time.perf_counter_ns()
                restored = decode(blob)
                dec.append(time.perf_counter_ns() - start)
                assert restored == raw
            results.append({"input": name, "raw_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                            "codec": codec, "compressed_bytes": len(blob), "encode": timing(enc), "decode": timing(dec)})
    tracemalloc.start()
    store = MemoryStore(21000000)
    store.claim(1)
    for pair in range(50):
        for metric, step, slots, columns in (("refs", 60000, 1536, 1), ("oi", 60000, 1536, 1),
                                            ("global", 300000, 320, 3), ("top", 300000, 320, 3)):
            store.create_ring(f"pulse:c0:{metric}:{pair}", step, slots, columns, 1)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    prior_source = subprocess.run(["git", "show", "fd00175:pulse/store/bucket_ring.py"],
                                  cwd=ROOT, capture_output=True, text=True, check=True).stdout
    prior_namespace = {}
    exec(compile(prior_source, "fd00175:bucket_ring.py", "exec"), prior_namespace)
    prototype = prior_namespace["BucketRing"]
    sizes = {}
    for name, ring_class in (("header_crc_one_mask", BucketRing), ("prototype_slot_crc_two_masks", prototype)):
        per_pair = 2 * ring_class(60000, 1536, 1).nbytes + 2 * ring_class(300000, 320, 3).nbytes
        sizes[name] = {"per_pair": per_pair, "pairs50": 50 * per_pair, "pairs503": 503 * per_pair}
    sizes["rfc_a"] = {"per_pair": 70352, "pairs50": 3517600, "pairs503": 35387056}
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                      "zlib": zlib.ZLIB_RUNTIME_VERSION, "brotli": brotli_version, "seed": 44,
                      "archived_original_sha256": hashlib.sha256(original).hexdigest(),
                      "operations": [operation_benchmark(n) for n in (320, 1536, 8192)],
                      "prior_prototype_operations_same_interpreter": operation_benchmark(1536, prototype, False),
                      "layout_bytes": sizes,
                      "temporary_cold_oi_ring_bytes_per_pair": BucketRing.nbytes_for(300000, 320),
                      "memory_50_pairs": {"raw_ring_bytes": store.used_bytes, "python_traced_current": current,
                                           "python_traced_peak": peak, "not_redis_memory": True},
                      "codecs": results}, indent=2))


if __name__ == "__main__":
    main()
