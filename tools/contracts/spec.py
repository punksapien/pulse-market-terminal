"""ADR-0077: source of committed JSON Schema documents; stdlib-only generation."""
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.wire.decimal_text import DECIMAL_PATTERN

DRAFT="https://json-schema.org/draft/2020-12/schema"
COMMON="urn:pulse:schema:v1:common"
MAX_TIME=9007199254740991
NOW=1800000000000  # Fictional protocol fixtures; never market/source evidence.
HORIZONS=("1H","4H","24H")
DECIMAL={"type":"string","minLength":1,"maxLength":64,
         "pattern":DECIMAL_PATTERN}
TIME={"type":"integer","minimum":0,"maximum":MAX_TIME}
TOKEN={"type":"string","minLength":1,"maxLength":128,"pattern":r"^[A-Za-z0-9_.:-]+$"}
REASON={"type":"string","minLength":1,"maxLength":64,"pattern":r"^[a-z][a-z0-9_]*$"}
UNITS=["fraction","pp","ratio","percent","quantity","USDT","price","bps","score","log_ratio"]


def nullable(schema):
    return {"anyOf":[deepcopy(schema),{"type":"null"}]}


def obj(properties,required=None):
    return {"type":"object","additionalProperties":False,"properties":properties,
            "required":list(properties) if required is None else required}


def ref(name):
    return {"$ref":COMMON+"#/$defs/"+name}


def typed_field(unit,definition=None):
    properties={"unit":{"const":unit}}
    if definition: properties["definition_version"]={"const":definition}
    return {"allOf":[ref("numericField"),{"properties":properties}]}


def field_schema(value_schema,unit_schema):
    properties={"status":{"enum":["available","unavailable","settlement_pending","unsupported"]},
        "reason":nullable(REASON),"value":nullable(value_schema),"unit":unit_schema,
        "asof_ms":nullable(TIME),"expires_ms":nullable(TIME),
        "assurance":{"enum":[None,"consecutive_verified","window_consistent_provisional",
                               "estimate_minus_verified_settlement","legacy_v1"]},
        "definition_version":TOKEN,"input_refs":{"type":"array","items":TOKEN,"maxItems":8,"uniqueItems":True},
        "classification_status":{"enum":["uncalibrated","classified","unclassified","not_applicable"]},
        "level":{"enum":[None,1,2,3,4]},"level_basis":nullable(DECIMAL),
        "level_basis_unit":nullable({"enum":UNITS}),
        "classification_version":nullable(TOKEN),
        "direction":nullable({"type":"string","minLength":1,"maxLength":40}),
        "baseline_asof_ms":nullable(TIME),"baseline_error_ms":nullable(TIME),
        "actual_duration_ms":nullable(TIME),"mode":{"enum":[None,"own","cold-start"]}}
    required=[key for key in properties if key not in ("classification_version","direction","baseline_asof_ms",
              "baseline_error_ms","actual_duration_ms","mode")]
    schema=obj(properties,required)
    schema["allOf"]=[
        {"if":{"properties":{"status":{"const":"available"}}},
         "then":{"properties":{"value":value_schema,"reason":{"type":"null"},"asof_ms":TIME,
                                "expires_ms":TIME,"input_refs":{"minItems":1}}},
         "else":{"properties":{"value":{"type":"null"},"reason":REASON,
                               "level":{"type":"null"},"level_basis":{"type":"null"},
                               "level_basis_unit":{"type":"null"}}}},
        {"if":{"properties":{"classification_status":{"const":"classified"}}},
         "then":{"required":["classification_version"],"properties":{
             "status":{"const":"available"},"level":{"enum":[1,2,3,4]},"classification_version":TOKEN,
             },"oneOf":[{"properties":{"definition_version":{"const":definition},"unit":{"const":unit},
                 **({} if definition=="volatility.v1" else {"level_basis":DECIMAL,"level_basis_unit":{"const":basis}})}}
                 for definition,unit,basis in (("volatility.v1","fraction",None),("long_short.v1","ratio","log_ratio"),
                     ("funding.v1","fraction","bps"),("funding_change.v1","fraction","bps"),
                     ("open_interest.v1","fraction","fraction"))]},
         "else":{"properties":{"level":{"type":"null"}}}},
        {"if":{"properties":{"level_basis":{"type":"null"}}},
         "then":{"properties":{"level_basis_unit":{"type":"null"}}},
         "else":{"properties":{"status":{"const":"available"},"level_basis_unit":{"enum":UNITS}}}}
    ]
    return schema


def common_schema():
    return {"$schema":DRAFT,"$id":COMMON,"title":"Pulse v1 field primitives",
        "$defs":{"decimal":DECIMAL,"time":TIME,"token":TOKEN,"reason":REASON,
        "numericField":field_schema(DECIMAL,{"enum":UNITS}),
        "textField":field_schema({"type":"string","minLength":1,"maxLength":512},{"enum":["state","text"]}),
        "flagField":field_schema({"type":"boolean"},{"const":"boolean"})}}


def value_field(unit,value,definition,input_ref="fixture:source"):
    return {"status":"available","reason":None,"value":value,"unit":unit,"asof_ms":NOW-1000,
        "expires_ms":NOW+60000,"assurance":None,"definition_version":definition,"input_refs":[input_ref],
        "classification_status":"uncalibrated","level":None,"level_basis":None,"level_basis_unit":None}


def unavailable_field(unit,definition,reason="source_unavailable",status="unavailable"):
    result=value_field(unit,"0",definition)
    result.update(status=status,reason=reason,value=None,asof_ms=None,expires_ms=None,input_refs=[])
    return result


def field_fixtures():
    definitions={"volatility":("fraction","volatility.v1","0.01545"),
        "ls":("ratio","long_short.v1","1.5"),"funding":("fraction","funding.v1","0.0001"),
        "funding_change":("fraction","funding_change.v1","0.00005"),
        "oi":("fraction","open_interest.v1","0.12"),"price":("price","price_change.v1","100.25"),
        "price_change":("fraction","price_change.v1","0.02"),"volume":("USDT","volume.rt.v1","1000000")}
    cases=[]
    for metric,(unit,definition,value) in definitions.items():
        available=value_field(unit,value,definition)
        if metric=="funding_change":
            available.update(level_basis="0.5",level_basis_unit="bps")
        missing=unavailable_field(unit,definition,"insufficient_history" if metric in ("oi","funding_change") else "source_unavailable")
        cases.append({"metric":metric,"schema":typed_field(unit,definition),"valid":available,"unavailable":missing})
    return {"fixture_kind":"fictional_protocol_only","now_ms":NOW,"cases":cases}
