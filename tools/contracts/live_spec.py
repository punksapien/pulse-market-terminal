"""ADR-0083 live wire documents; fictional fixtures only."""
from copy import deepcopy
from spec import DRAFT,NOW,TIME,TOKEN,DECIMAL,REASON,obj,nullable
from snapshot_spec import ID,HOURS


def live_schema(kind):
    props={"id":ID,"status":{"enum":["available","unavailable","settlement_pending","unsupported"]},
        "reason":nullable(REASON),"source_ms":nullable(TIME),"expires_ms":nullable(TIME),
        "input_refs":{"type":"array","maxItems":8,"uniqueItems":True,"items":TOKEN}}
    optional=[]
    if kind=="funding":
        props.update(h=nullable(HOURS),nft=nullable(TIME),fr=nullable(DECIMAL),efr=nullable(DECIMAL))
        # ADR-0116: optional so earlier producers stay valid; semantic validation recomputes and compares
        props.update(level={"enum":[1,2,3,4]},classification_version=TOKEN)
        optional=["level","classification_version"]
    else:
        props["price"]=nullable(DECIMAL)
        props["volume"]=obj({"status":{"enum":["available","unavailable"]},"reason":nullable(REASON),
            "value":nullable(DECIMAL),"unit":{"const":"USDT"},"source_ms":nullable(TIME),"expires_ms":nullable(TIME)})
        optional=["volume"]
    row=obj(props,[k for k in props if k not in optional])
    available={"reason":{"type":"null"},"source_ms":TIME,"expires_ms":TIME,"input_refs":{"minItems":1}}
    available.update({k:v for k,v in ({"h":HOURS,"nft":TIME,"fr":DECIMAL,"efr":DECIMAL} if kind=="funding" else {"price":DECIMAL}).items()})
    unavailable={"reason":REASON,"efr" if kind=="funding" else "price":{"type":"null"}}
    row["allOf"]=[{"if":{"properties":{"status":{"const":"available"}}},
                    "then":{"properties":available},"else":{"properties":unavailable}}]
    return {"$schema":DRAFT,"$id":"urn:pulse:schema:v1:live_"+kind,**obj({
        "schema":{"const":"pulse.live_"+kind+".v1"},"mapping_version":TOKEN,
        "sequence":TIME,"generated_ms":TIME,"expires_ms":TIME,
        "rows":{"type":"array","minItems":1,"maxItems":8192,"items":row},
        "inputs":{"type":"object","minProperties":1,"maxProperties":65536,"propertyNames":TOKEN,
            "additionalProperties":obj({"version":TOKEN,"source_ms":TIME,"verified_at_ms":nullable(TIME),
                "expires_ms":TIME,"definition_version":TOKEN})}})}


def live_fixtures(main):
    result={}
    for kind in ("funding","price"):
        rows=[]; source=NOW-500
        for row in main["rows"]:
            r={"id":row["id"],"status":"available","reason":None,"source_ms":source,
               "expires_ms":source+(120000 if kind=="funding" else 60000),"input_refs":["fixture:live:"+kind]}
            if kind=="funding":
                f=row["funding_change"]
                r.update(h=f["h"],nft=f["nft"],fr=f["settled_rate"],efr="0.0002")
                r["expires_ms"]=min(r["expires_ms"],r["nft"])
            else: r["price"]="103.125"
            rows.append(r)
        live={"schema":"pulse.live_"+kind+".v1","mapping_version":main["mapping_version"],"sequence":1,
              "generated_ms":NOW,"expires_ms":NOW+15000,"rows":rows,
              "inputs":{"fixture:live:"+kind:{"version":"fixture.live.v1","source_ms":source,
                  "verified_at_ms":None,"expires_ms":source+120000,"definition_version":"live."+kind+".v1"}}}
        result["live_"+kind+".valid.json"]=live
        if kind=="price":
            inside=deepcopy(live)
            inside.update(generated_ms=NOW+5000,expires_ms=NOW+20000,sequence=2)
            for r in inside["rows"]: r.update(source_ms=NOW+4000,expires_ms=NOW+64000)
            for record in inside["inputs"].values(): record.update(source_ms=NOW+4000,expires_ms=NOW+124000)
            result["live_price.in_ladder.json"]=inside
        bad=deepcopy(live)
        for r in bad["rows"]:
            r.update(status="unavailable",reason="source_unavailable",source_ms=None,expires_ms=None,input_refs=[])
            r["efr" if kind=="funding" else "price"]=None
        result["live_"+kind+".unavailable.json"]=bad
        stale=deepcopy(live); stale["generated_ms"]-=180000; stale["expires_ms"]-=180000
        for r in stale["rows"]: r["source_ms"]-=180000; r["expires_ms"]-=180000
        for source_record in stale["inputs"].values():
            source_record["source_ms"]-=180000; source_record["expires_ms"]-=180000
        result["live_"+kind+".stale.json"]=stale
        foreign=deepcopy(live); foreign["mapping_version"]="sha256:"+"0"*64
        result["live_"+kind+".mapping_mismatch.json"]=foreign
        if kind=="funding":
            due=deepcopy(live)
            for r in due["rows"]: r.update(nft=NOW+1000,expires_ms=NOW+1000)
            result["live_funding.due_cycle.json"]=due
    return result


def validate_live(value, universe, mapping_version):
    """Call after JSON Schema. Time validity here is at generation, not receipt."""
    from semantics import require,price
    from decimal import Decimal
    generated=value["generated_ms"]
    require(value["mapping_version"]==mapping_version,"mapping_mismatch")
    require(generated<value["expires_ms"]<=generated+15000,"live_envelope_expiry")
    require(len(value["rows"])==len(universe),"live_dictionary_length")
    for record in value["inputs"].values():
        require(record["source_ms"]<=generated+5000 and record["source_ms"]<record["expires_ms"],"live_input_time")
    for i,(row,meta) in enumerate(zip(value["rows"],universe)):
        require(row["id"]==meta["id"]==i,"live_dictionary_id")
        if row["status"]!="available": continue
        require(row["source_ms"]<=generated+5000 and row["source_ms"]<row["expires_ms"]
                and generated<row["expires_ms"],"live_source_expired_at_generation")
        require(all(k in value["inputs"] for k in row["input_refs"]),"live_unknown_input")
        require(row["expires_ms"]<=min(value["inputs"][k]["expires_ms"] for k in row["input_refs"]),"live_freshens_input")
        if "price" in row: price(row["price"])
        else:
            require(row["h"]==meta["funding_hours"],"live_interval_mismatch")
            require(row["expires_ms"]<=min(row["source_ms"]+120000,row["nft"]),"live_funding_expiry")
        for key in (("price",) if "price" in row else ("fr","efr")):
            require(Decimal(row[key]).as_tuple().exponent>=-60,"decimal_scale_unsupported")
