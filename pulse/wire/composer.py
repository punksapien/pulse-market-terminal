"""ADR-0089 validated worker composition; returns bytes, never publishes on read."""
from copy import deepcopy
from dataclasses import dataclass
from .compact import encode,decode,raw
from .identity import digest,generation
from .publication import prepare,manifest,byte_digest
from .live_price import clock


@dataclass(frozen=True)
class Publication:
    channel: str
    mapping_version: str
    generated_ms: int
    expires_ms: int
    input_revisions: tuple
    pointer: bytes
    descriptors: tuple
    blobs: tuple

    @property
    def allocation_bytes(self):
        return len(self.pointer)+sum(len(b) for _,b in self.descriptors)+sum(len(b) for _,b in self.blobs)


def revisions(vector):
    if (type(vector) is not dict or not 1<=len(vector)<=65536
            or any(not isinstance(k,str) or not 1<=len(k)<=128 or type(v) is not int
                   or not 1<=v<=9007199254740991 for k,v in vector.items())):
        raise ValueError("invalid_input_revisions")
    return tuple(sorted(vector.items()))


def compose(value,*,validate,brotli_compress,input_revisions,now_ms,include_identity=True):
    """Full validator is mandatory; real source-to-row adapters precede this stage."""
    clock(now_ms)
    if not callable(validate) or not callable(brotli_compress): raise ValueError("composer_dependencies_required")
    pinned=revisions(input_revisions)
    value=deepcopy(value)
    validate(value)
    source,expiry=value["generated_ms"],value["expires_ms"]
    clock(source);clock(expiry)
    if source>now_ms+5000 or not source<expiry or now_ms>=expiry:
        raise ValueError("composer_expired_or_future")
    if value.get("schema")=="pulse.snapshot.v1":
        if value["mapping_version"]!=digest(value["universe"]) or value["generation"]!=generation(value):
            raise ValueError("composer_identity_mismatch")
        documents=encode(value)
        if raw(decode(documents))!=raw(value): raise ValueError("composer_roundtrip_mismatch")
        channel="snapshot:"+value["horizon"].lower()+":manifest"
    elif value.get("schema") in ("pulse.live_price.v1","pulse.live_funding.v1"):
        if expiry>source+15000: raise ValueError("live_envelope_expiry")
        documents={"live":value}
        channel="live_price_v1" if value["schema"]=="pulse.live_price.v1" else "live_funding_v1"
    else: raise ValueError("unknown_composer_schema")
    descriptors={};blobs={};by_hash={}
    for name,doc in documents.items():
        descriptor,encoded=prepare(doc,brotli_compress=brotli_compress,include_identity=include_identity)
        descriptors[name]=descriptor
        by_hash[descriptor["logical_sha256"]]=raw(descriptor)
        for key,body in encoded.items():
            if key!=byte_digest(body) or (key in blobs and blobs[key]!=body):
                raise ValueError("composer_blob_integrity")
            blobs[key]=body
    pointer=raw(manifest(value,descriptors) if "live" not in documents else descriptors["live"])
    if len(pointer)>131072: raise ValueError("composer_pointer_limit")
    return Publication(channel,value["mapping_version"],source,expiry,pinned,pointer,
        tuple(sorted(by_hash.items())),tuple(sorted(blobs.items())))
