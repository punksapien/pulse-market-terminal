"""Serve pulse-preview.html and the REAL Preview reader (pulse.http.wire_reader) from golden wire fixtures.
For browser verification only: an in-memory store stands in for Redis; content is fictional and its timestamps are shifted to now.
Run: PYTHONPATH=<repo> <python with brotli> tools/frontend/serve_real_reader.py [port]   (prints 'READY <port>')"""
import hashlib
import json
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import brotli

from pulse.http.wire_reader import response
from pulse.wire.compact import PARTS, decode, raw
from pulse.wire.publication import manifest, prepare

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = {p: json.loads((ROOT / "docs/contracts/fixtures/wire" / f"{p}.json").read_text()) for p in PARTS}
LOCK = threading.Lock()
BLOBS, DESCRIPTORS, CHANNELS = {}, {}, {}
STATE = {"built": 0, "horizon": None, "mapping": GOLDEN["core"]["mapping_version"], "logical": None}


def shift(value, delta):
    if isinstance(value, list):
        return [shift(v, delta) for v in value]
    if isinstance(value, dict):
        return {k: shift(v, delta) for k, v in value.items()}
    return value + delta if type(value) is int and value >= 1_500_000_000_000 else value


def brotli_compress(body, quality):
    return brotli.compress(body, quality=quality)


ALIGNED_KEYS = {"reference_minute_ms", "start_ms", "built_at_ms", "valid_until_ms"}


def shift_keyed(value, delta, aligned, key=None):
    """Inside an atomic JSON value the dict keys are visible: ladder minutes keep minute alignment."""
    if isinstance(value, list):
        return [shift_keyed(v, delta, aligned, key) for v in value]
    if isinstance(value, dict):
        return {k: shift_keyed(v, delta, aligned, k) for k, v in value.items()}
    if type(value) is int and value >= 1_500_000_000_000:
        return value + (aligned if key in ALIGNED_KEYS else delta)
    return value


def shift_vector(vector, delta, aligned, is_aligned):
    amount = aligned if is_aligned else delta
    kind = vector[0]
    f = (lambda v: shift_keyed(v, delta, aligned)) if not is_aligned else (lambda v: v + amount if type(v) is int else v)
    if kind == "c":
        return ["c", f(vector[1])]
    if kind == "d":
        return ["d", [f(v) for v in vector[1]]]
    return ["s", f(vector[1]), [[i, f(v)] for i, v in vector[2]]]


def shift_references(doc, delta, aligned):
    """Reference part: only the ladder's own minutes move by a whole number of minutes; every other
    timestamp (field times, envelope, inputs) moves by the exact delta so identities stay equal across parts."""
    doc = shift(doc, delta)  # exact shift of everything, then undo for the aligned ladder columns
    out = json.loads(json.dumps(GOLDEN["references"]))
    for key in ("metadata", "groups", "inputs"):
        out[key] = doc[key]
    out["generated_ms"], out["expires_ms"] = doc["generated_ms"], doc["expires_ms"]
    columns = []
    for column in out["columns"]:
        path = column[0]
        is_aligned = path[-1] in ALIGNED_KEYS
        new = list(column)
        for i in range(3, len(column)):
            new[i] = shift_vector(column[i], delta, aligned, is_aligned) if i == 3 else shift_vector(column[i], delta, aligned, False) if False else column[i]
        columns.append(new)
    out["columns"] = columns
    return out


def publish(horizon, now_ms):
    delta = now_ms - 1000 - GOLDEN["core"]["generated_ms"]
    aligned = delta // 60000 * 60000
    generation = "sha256:" + hashlib.sha256(str(now_ms).encode()).hexdigest()
    parts, descriptors = {}, {}
    for name, doc in GOLDEN.items():
        doc = shift_references(doc, delta, aligned) if name == "references" else shift(doc, delta)
        doc["generation"], doc["horizon"] = generation, horizon
        if name == "references":
            # the ladder echoes the logical generation it belongs to; keep the echo equal to the new generation
            for column in doc["columns"]:
                if column[0][-1] == "generation_id":
                    column[3] = ["c", generation]
        if name != "core":
            # the envelope clock is identical across parts; only the ladder's own minutes are aligned
            for key in ("generated_ms", "expires_ms"):
                doc[key] = shift(GOLDEN["core"][key], delta)
        if "header" in doc:
            doc["header"]["generation"], doc["header"]["horizon"] = generation, horizon
        parts[name] = doc
        descriptor, blobs = prepare(doc, brotli_compress=brotli_compress)
        BLOBS.update(blobs)
        DESCRIPTORS[descriptor["logical_sha256"]] = descriptor
        descriptors[name] = descriptor
    base = {k: parts["core"][k] for k in ("mapping_version", "generation", "horizon", "generated_ms", "expires_ms")}
    value = manifest(base, descriptors)
    body = raw(value)
    CHANNELS["snapshot:" + horizon.lower() + ":manifest"] = {"payload": body, "digest": hashlib.sha256(body).hexdigest(),
        "source_ms": base["generated_ms"], "expires_ms": base["expires_ms"]}
    STATE.update(built=now_ms, horizon=horizon, logical=decode(parts))


def publish_live(now_ms):
    logical = STATE["logical"]
    rows = []
    for row in logical["rows"]:
        fc = row["funding_change"]
        efr = str(Decimal(fc["settled_rate"]) + Decimal("0.00003"))
        rows.append({"id": row["id"], "status": "available", "reason": None, "h": fc["h"], "nft": fc["nft"], "fr": fc["settled_rate"],
                     "efr": efr, "source_ms": now_ms - 500, "expires_ms": fc["nft"], "input_refs": ["fixture:live:funding"]})
    doc = {"schema": "pulse.live_funding.v1", "mapping_version": STATE["mapping"], "sequence": now_ms, "generated_ms": now_ms,
           "expires_ms": now_ms + 60000, "rows": rows, "inputs": {"fixture:live:funding": {"version": "fixture.live.v1", "source_ms": now_ms - 500,
           "verified_at_ms": now_ms, "expires_ms": now_ms + 60000, "definition_version": "fixture.live.v1"}}}
    descriptor, blobs = prepare(doc, brotli_compress=brotli_compress)
    BLOBS.update(blobs)
    body = raw(descriptor)
    CHANNELS["live_funding_v1"] = {"payload": body, "digest": hashlib.sha256(body).hexdigest(), "source_ms": now_ms, "expires_ms": now_ms + 15000}


def publish_live_price(now_ms):
    logical = STATE["logical"]
    start = logical["rows"][0]["references"]["ladder"]["start_ms"]
    source = max(now_ms - 500, start + 1)
    rows = [{"id": row["id"], "status": "available", "reason": None, "source_ms": source, "expires_ms": source + 60000,
             "input_refs": ["fixture:live:price"], "price": "103.125"} for row in logical["rows"]]
    doc = {"schema": "pulse.live_price.v1", "mapping_version": STATE["mapping"], "sequence": now_ms, "generated_ms": now_ms,
           "expires_ms": now_ms + 15000, "rows": rows, "inputs": {"fixture:live:price": {"version": "fixture.live.v1", "source_ms": source,
           "verified_at_ms": None, "expires_ms": source + 120000, "definition_version": "live.price.v1"}}}
    descriptor, blobs = prepare(doc, brotli_compress=brotli_compress)
    BLOBS.update(blobs)
    body = raw(descriptor)
    CHANNELS["live_price_v1"] = {"payload": body, "digest": hashlib.sha256(body).hexdigest(), "source_ms": now_ms, "expires_ms": now_ms + 15000}


class Reader:
    def __init__(self, name):
        self.name = name

    def read(self, now_ms):
        record = CHANNELS.get(self.name)
        return {"status": "available", "reason": None, **record} if record else {"status": "unavailable", "reason": "document_missing"}


class Artifacts:
    mapping_version = STATE["mapping"]

    def descriptor(self, identifier):
        return DESCRIPTORS[identifier]

    def blob(self, identifier):
        return BLOBS.get(identifier)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            body = (ROOT / "pulse-preview.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path != "/api/pulse":
            self.send_error(404)
            return
        query = parse_qs(url.query, keep_blank_values=True)
        now_ms = int(time.time() * 1000)
        with LOCK:
            if "horizon" in query and (STATE["horizon"] != query["horizon"][0] or now_ms - STATE["built"] > 25000):
                publish(query["horizon"][0], now_ms)
            if STATE["logical"] is not None and query.get("live") == ["1"]:
                publish_live(now_ms)
            if STATE["logical"] is not None and query.get("live") == ["price"]:
                publish_live_price(now_ms)
            result = response(query, environment="preview", store_factory=Reader, artifacts_factory=Artifacts, now_ms=now_ms,
                              accept_encoding=self.headers.get("Accept-Encoding"), if_none_match=self.headers.get("If-None-Match"))
        self.send_response(result.status)
        for k, v in result.headers.items():
            self.send_header(k, v)
        if "Content-Length" not in result.headers:
            self.send_header("Content-Length", str(len(result.body or b"")))
        self.end_headers()
        if result.body:
            self.wfile.write(result.body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 0), Handler)
    print("READY", server.server_address[1], flush=True)
    server.serve_forever()
