"""ADR-0085 strictly bounded Preview artifact reads; no write methods."""
import json
import re
from .staging_policy import staging_key

HASH=re.compile(r"^sha256:[a-f0-9]{64}$")
MAX_DESCRIPTOR_BYTES=16384
MAX_BLOB_BYTES=1114112


def descriptor_valid(value, *, logical_hash=None, mapping_version=None):
    if not isinstance(value,dict) or set(value)!={"schema","logical_sha256","raw_bytes","mapping_version","generation","variants"}:
        raise ValueError("invalid_artifact_descriptor")
    if value["schema"]!="pulse.artifact.v1" or not isinstance(value["logical_sha256"],str) or not HASH.fullmatch(value["logical_sha256"]):
        raise ValueError("invalid_artifact_descriptor")
    if logical_hash is not None and value["logical_sha256"]!=logical_hash: raise ValueError("artifact_hash_mismatch")
    if mapping_version is not None and value["mapping_version"]!=mapping_version: raise ValueError("artifact_mapping_mismatch")
    if not isinstance(value["mapping_version"],str) or not HASH.fullmatch(value["mapping_version"]): raise ValueError("invalid_artifact_mapping")
    if value["generation"] is not None and (not isinstance(value["generation"],str) or not HASH.fullmatch(value["generation"])):
        raise ValueError("invalid_artifact_generation")
    if type(value["raw_bytes"]) is not int or not 1<=value["raw_bytes"]<=1048576: raise ValueError("artifact_size_refused")
    variants=value["variants"]
    if not isinstance(variants,dict) or not {"br11","gzip"}<=variants.keys() or variants.keys()-{"br11","br5","gzip","identity"}:
        raise ValueError("invalid_artifact_variants")
    for name,variant in variants.items():
        if not isinstance(variant,dict) or set(variant)!={"sha256","bytes","encoding"} or not isinstance(variant["sha256"],str) or not HASH.fullmatch(variant["sha256"]):
            raise ValueError("invalid_artifact_variant")
        if type(variant["bytes"]) is not int or not 1<=variant["bytes"]<=MAX_BLOB_BYTES or variant["encoding"]!=("br" if name.startswith("br") else name):
            raise ValueError("invalid_artifact_variant")
    if "identity" in variants and (variants["identity"]["sha256"]!=value["logical_sha256"] or variants["identity"]["bytes"]!=value["raw_bytes"]):
        raise ValueError("identity_descriptor_mismatch")
    return value


class PublishedArtifacts:
    def __init__(self,client,run_id,mapping_version):
        if not isinstance(run_id,str) or not re.fullmatch(r"[a-f0-9]{32}",run_id) or not isinstance(mapping_version,str) or not HASH.fullmatch(mapping_version):
            raise ValueError("invalid_publication_scope")
        self.client=client; self.mapping_version=mapping_version
        self._prefix="pulse:stg:{"+run_id+"}:publication:"

    def key(self,kind,identifier):
        if kind not in ("artifact","blob") or not isinstance(identifier,str) or not HASH.fullmatch(identifier):
            raise ValueError("invalid_artifact_identifier")
        return staging_key(self._prefix+kind+":"+identifier[7:])

    def descriptor(self,identifier):
        body=self.client.getrange(self.key("artifact",identifier),0,MAX_DESCRIPTOR_BYTES)
        if not isinstance(body,bytes) or not body or len(body)>MAX_DESCRIPTOR_BYTES: raise ValueError("artifact_descriptor_missing_or_large")
        return descriptor_valid(json.loads(body),logical_hash=identifier,mapping_version=self.mapping_version)

    def blob(self,identifier):
        body=self.client.getrange(self.key("blob",identifier),0,MAX_BLOB_BYTES)
        if not isinstance(body,bytes) or not body or len(body)>MAX_BLOB_BYTES: raise ValueError("artifact_blob_missing_or_large")
        return body
