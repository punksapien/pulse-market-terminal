"""Canonical identity shared by worker code and offline contract tooling."""
from copy import deepcopy
import hashlib
import json


def digest(value):
    return "sha256:"+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),
                                      ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def generation(snapshot):
    value=deepcopy(snapshot)
    value.pop("generation",None)
    for row in value["rows"]: row["references"].pop("generation_id",None)
    return digest(value)


def seal_fixture(snapshot):
    value=deepcopy(snapshot)
    mapping=digest(value["universe"])
    value["mapping_version"]=mapping
    for row in value["rows"]:
        row["funding_change"]["mapping_version"]=mapping
        row["references"]["mapping_version"]=mapping
    identifier=generation(value)
    value["generation"]=identifier
    for row in value["rows"]: row["references"]["generation_id"]=identifier
    return value
