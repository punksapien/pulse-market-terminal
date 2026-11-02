"""ADR-0082 artifact and generation-manifest schemas."""
from copy import deepcopy
from spec import DRAFT,obj,TIME

HASH={"type":"string","pattern":"^sha256:[a-f0-9]{64}$"}


def artifact_schema():
    variant=obj({"sha256":HASH,"bytes":{"type":"integer","minimum":1,"maximum":1114112},
                 "encoding":{"enum":["br","gzip","identity"]}})
    variants=obj({name:deepcopy(variant) for name in ("br11","br5","gzip","identity")},required=["br11","gzip"])
    for name,entry in variants["properties"].items():
        entry["properties"]["encoding"]={"const":"br" if name.startswith("br") else name}
    return {"$schema":DRAFT,"$id":"urn:pulse:schema:v1:artifact",**obj({"schema":{"const":"pulse.artifact.v1"},
        "logical_sha256":HASH,"raw_bytes":{"type":"integer","minimum":1,"maximum":1048576},
        "mapping_version":HASH,"generation":{"anyOf":[HASH,{"type":"null"}]},"variants":variants})}


def manifest_schema():
    return {"$schema":DRAFT,"$id":"urn:pulse:schema:v1:manifest",**obj({"schema":{"const":"pulse.manifest.v1"},
        "mapping_version":HASH,"generation":HASH,"horizon":{"enum":["1H","4H","24H"]},
        "generated_ms":TIME,"expires_ms":TIME,"parts":obj({part:{"$ref":"urn:pulse:schema:v1:artifact"}
            for part in ("core","volatility","ls","funding","funding_change","oi","references")})})}
