"""ADR-0082 worker-only compression and exact-byte content addresses."""
import gzip
import hashlib
from .compact import raw

MAX_RAW_BYTES=1_048_576


def byte_digest(value):
    return "sha256:"+hashlib.sha256(value).hexdigest()


def prepare(document, *, brotli_compress, include_fast=False,include_identity=True):
    """Pure worker step. Store all blobs before promoting its descriptor."""
    body=raw(document)
    if len(body)>MAX_RAW_BYTES:
        raise ValueError("wire_document_too_large")
    if type(include_identity) is not bool:raise ValueError("wire_identity_policy")
    variants={"gzip":gzip.compress(body,compresslevel=6,mtime=0),
              "br11":brotli_compress(body,11)}
    if include_identity:variants["identity"]=body
    if include_fast: variants["br5"]=brotli_compress(body,5)
    blobs={}
    descriptor={"schema":"pulse.artifact.v1","logical_sha256":byte_digest(body),
        "raw_bytes":len(body),"mapping_version":document["mapping_version"],
        "generation":document.get("generation"),"variants":{}}
    for name,data in variants.items():
        if not isinstance(data,bytes) or not data or len(data)>MAX_RAW_BYTES+65536:
            raise ValueError("invalid_compressed_bytes")
        identifier=byte_digest(data)
        blobs[identifier]=data
        descriptor["variants"][name]={"sha256":identifier,"bytes":len(data),
            "encoding":"br" if name.startswith("br") else name}
    return descriptor,blobs


def manifest(snapshot, descriptors):
    from .compact import PARTS
    if set(descriptors)!=set(PARTS): raise ValueError("incomplete_publication")
    for descriptor in descriptors.values():
        if descriptor["mapping_version"]!=snapshot["mapping_version"] or descriptor["generation"]!=snapshot["generation"]:
            raise ValueError("mixed_publication_generation")
    return {"schema":"pulse.manifest.v1",**{k:snapshot[k] for k in
        ("mapping_version","generation","horizon","generated_ms","expires_ms")},
        "parts":descriptors}
