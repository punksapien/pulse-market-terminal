"""ADR-0081 wire schema and deterministic non-market payload corpus."""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
import random

from spec import DRAFT, TIME, TOKEN, obj
from snapshot_spec import snapshot_fixture, CATEGORIES, HORIZONS
from canonical import seal_fixture
from semantics import fields


def wire_schema():
    any_json = {}
    vector = {"oneOf": [
        {"type":"array","prefixItems":[{"const":"c"},any_json],"minItems":2,"maxItems":2},
        {"type":"array","prefixItems":[{"const":"d"},{"type":"array"}],"minItems":2,"maxItems":2},
        {"type":"array","prefixItems":[{"const":"s"},any_json,{"type":"array","items":{
            "type":"array","prefixItems":[{"type":"integer","minimum":0},any_json],"minItems":2,"maxItems":2}}],"minItems":3,"maxItems":3}]}
    path={"type":"array","minItems":1,"maxItems":32,"items":{"type":"string","minLength":1,"maxLength":128}}
    presence={"type":["array","null"],"items":{"type":"integer","minimum":0,"maximum":8191},"uniqueItems":True}
    column={"oneOf":[{"type":"array","prefixItems":[path,presence,{"const":"j"},vector],"minItems":4,"maxItems":4},
        {"type":"array","prefixItems":[path,presence,{"const":"f"},vector,vector,vector,vector],"minItems":7,"maxItems":7}]}
    result=obj({"schema":{"const":"pulse.columns.v1"},"part":{"enum":["core","volatility","ls","funding","funding_change","oi","references"]},
        "count":{"type":"integer","minimum":1,"maximum":8192},"mapping_version":TOKEN,"generation":TOKEN,
        "horizon":{"enum":list(HORIZONS)},"generated_ms":TIME,"expires_ms":TIME,
        "metadata":{"type":"array","maxItems":100000,"items":{"type":"object"}},
        "groups":{"type":"array","maxItems":100000,"items":{"type":"object"}},
        "columns":{"type":"array","maxItems":1024,"items":column},"inputs":{"type":"object"},
        "header":{"type":"object"}},required=["schema","part","count","mapping_version","generation","horizon","generated_ms","expires_ms","metadata","groups","columns","inputs"])
    result["allOf"]=[{"if":{"properties":{"part":{"const":"core"}}},"then":{"required":["header"]},
                      "else":{"not":{"required":["header"]}}}]
    return {"$schema":DRAFT,"$id":"urn:pulse:schema:v1:columns",**result}


def varied_snapshot(n=504, *, independent_versions=False):
    """Seed 81, varied values/source versions/times/missing fields; NOT real data."""
    rng=random.Random(81)
    result=snapshot_fixture(); result["rows"]=[]; result["universe"]=[]; result["inputs"]={}
    for i in range(n):
        h=(1,4,8)[i%3]
        row=deepcopy(snapshot_fixture(hours=h,identifier=i)["rows"][0])
        symbol="TEST"+str(i)
        result["universe"].append({"id":i,"pair_id":"B-"+symbol+"_USDT","symbol":symbol+"USDT","funding_hours":h})
        for f in fields(row):
            if f["status"]!="available" or f["unit"] in ("state","text","boolean"): continue
            if f["unit"] in ("fraction","ratio","quantity","USDT","score","log_ratio","pp"):
                f["value"]=str(Decimal(rng.randrange(1,1000000))/1000000)
        # Vary source identity with high-entropy versions, shared within each group.
        groups={}
        for key,node in row.items():
            source={"price":"ticker","changes":"ticker","volume":"ticker","signal":"scorer",
                "signal_since":"scorer","context":"scorer","outlook":"scorer","volatility":"scorer",
                "funding_change":"settled","funding":"funding","oi":"oi","ls":"ls"}.get(key,key)
            name=f"{source}:{i}"
            # Primary case assumes the composer materialises a content-addressed
            # source batch (all original pair records) before publication. This is
            # an explicit producer requirement, not evidence it exists already.
            # Stress mode retains independent per-response versions, losslessly.
            nonce = rng.randbytes(32)
            version_material = nonce if independent_versions else source.encode()
            entry={"version":"sha256:"+hashlib.sha256(version_material).hexdigest(),
                "source_ms":result["generated_ms"]-1000,"verified_at_ms":result["generated_ms"],
                "expires_ms":row["funding_change"]["expires_ms"],"definition_version":source+".v1"}
            if source not in groups: groups[source]=entry
            for f in fields(node):
                f["input_refs"]=[name]
            if key=="signal_since": node["input_refs"]=[name]
            if list(fields(node)) or key=="signal_since": result["inputs"][name]=groups[source]
        for key,node in row.items():
            offset=0 if key=="funding_change" else (i%23)*1000
            for f in fields(node):
                f["asof_ms"]-=offset
                if key=="funding": f["expires_ms"]=min(f["expires_ms"],f["asof_ms"]+120000) if f["expires_ms"]<row["funding"]["nft"] else f["expires_ms"]
                if "baseline_asof_ms" in f: f["baseline_asof_ms"]-=offset
        row["signal_since"]["asof_ms"]-=(i%23)*1000
        row["signal_since"]["at_ms"]=row["signal_since"]["asof_ms"]-10000
        row["references"]["fallback"]["source_ms"]=row["price"]["asof_ms"]
        for horizon,window in zip(HORIZONS,(3600000,14400000,86400000)):
            row["references"]["fallback"]["references"][horizon]["reference_minute_ms"]=(row["price"]["asof_ms"]-window)//60000*60000
        code=list(CATEGORIES)[i%len(CATEGORIES)]
        row["signal"]["code"]["value"]=code; row["signal"]["category"]["value"]=CATEGORIES[code]
        row["signal_since"]["state"]=code
        row["volatility"]["score"]=deepcopy(row["volatility"]["windows"]["1H"])
        row["context"]["rsi_14"]["value"]=str(rng.randrange(1,100))
        p=str(Decimal(rng.randrange(1,100000000))/1000)
        row["price"]["value"]=p; row["references"]["fallback"]["price"]=p
        for points in row["references"]["ladder"]["slots"]:
            for point in points: point["open_price"]=str(Decimal(rng.randrange(1,100000000))/1000)
        for horizon,point in row["references"]["fallback"]["references"].items():
            base=str(Decimal(rng.randrange(1,100000000))/1000); point["open_price"]=base
            with localcontext() as ctx:
                ctx.prec=34
                row["changes"][horizon]["value"]=str((Decimal(p)-Decimal(base))/Decimal(base))
        last=Decimal(rng.randrange(-1000,1000))/1000000
        cycle=row["funding_change"]
        cycle["settled_last"]["value"]=str(last)
        basis=str(abs(last*10000*8/h))
        cycle["settled_last"]["level_basis"]=basis; cycle["basis_bps"]["value"]=basis
        row["volatility"]["direction"]["value"]=("Stable","Surging","Rising","Falling","Contracting")[i%5]
        for horizon,f in row["volatility"]["windows"].items():
            f.update(classification_status="classified",classification_version="volatility.v1",level=i%4+1)
        row["volatility"]["score"]=deepcopy(row["volatility"]["windows"]["1H"])
        if i%11==0:
            f=row["funding"]["cumulative_7d"]
            f.update(status="unavailable",reason="insufficient_history",value=None,asof_ms=None,expires_ms=None,input_refs=[])
        result["rows"].append(row)
    if not independent_versions:
        # Real digest of the synthetic grouped witnesses, not a constant label.
        # The real producer must retain the corresponding source-batch artifacts.
        batches={}
        for row in result["rows"]:
            for f in fields(row):
                for name in f["input_refs"]:
                    source=name.split(":",1)[0]
                    batches.setdefault(source,[]).append({"id":row["id"],**{k:v for k,v in f.items() if k!="input_refs"}})
        versions={source:"sha256:"+hashlib.sha256(json.dumps(batch,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
                  for source,batch in batches.items()}
        for name,entry in result["inputs"].items():
            entry["version"]=versions[name.split(":",1)[0]]
    return seal_fixture(result)
