"""Generate protocol schemas/fixtures, or --check committed bytes. No network."""
import argparse
import json
from pathlib import Path
from spec import common_schema, field_fixtures
from snapshot_spec import snapshot_schema, snapshot_fixture, make_unavailable
from copy import deepcopy
from canonical import seal_fixture
from wire_spec import wire_schema
from publication_spec import artifact_schema,manifest_schema
from live_spec import live_schema,live_fixtures
from pairing_vectors import pairing_vectors
from classified_fixture import classified_fixture,all_classified_fixture

ROOT=Path(__file__).resolve().parents[2]


def outputs():
    result={"docs/contracts/schema/common.schema.json":common_schema(),
            "docs/contracts/fixtures/metric_fields.v1.json":field_fixtures(),
            "docs/contracts/schema/snapshot.schema.json":snapshot_schema()}
    result["pulse/wire/schemas/common.schema"]=common_schema()
    result["pulse/wire/schemas/snapshot.schema"]=snapshot_schema()
    result["docs/contracts/schema/columns.schema.json"]=wire_schema()
    result["docs/contracts/schema/artifact.schema.json"]=artifact_schema()
    result["docs/contracts/schema/manifest.schema.json"]=manifest_schema()
    for kind in ("funding","price"):
        result["docs/contracts/schema/live_"+kind+".schema.json"]=live_schema(kind)
        result["pulse/wire/schemas/live_"+kind+".schema"]=live_schema(kind)
    for horizon in ("1H","4H","24H"):
        fixture=snapshot_fixture(horizon)
        result["docs/contracts/fixtures/snapshot.valid."+horizon+".json"]=fixture
    result["docs/contracts/fixtures/snapshot.unavailable.json"]=make_unavailable(snapshot_fixture())
    partial=snapshot_fixture(hours=8)
    for i,hours in enumerate((4,1),1):
        row=snapshot_fixture(hours=hours,identifier=i)["rows"][0]
        meta={"id":i,"pair_id":"B-"+("ETH" if i==1 else "DOGE")+"_USDT",
              "symbol":("ETH" if i==1 else "DOGE")+"USDT","funding_hours":hours}
        partial["rows"].append(row); partial["universe"].append(meta)
    for key in ("cumulative_7d",):
        f=partial["rows"][2]["funding"][key]
        f.update(status="unavailable",reason="insufficient_history",value=None,asof_ms=None,expires_ms=None,input_refs=[])
    partial["rows"][2]["funding_change"]["settled_7d"].update(status="unavailable",reason="insufficient_history",
        value=None,asof_ms=None,expires_ms=None,input_refs=[])
    result["docs/contracts/fixtures/snapshot.partial.json"]=partial
    expired=deepcopy(snapshot_fixture())
    def age(node):
        if isinstance(node,dict):
            for key,value in node.items():
                if (key.endswith("_ms") or key=="nft") and type(value) is int:
                    if key not in ("baseline_error_ms","actual_duration_ms"): node[key]-=180000
                else: age(value)
        elif isinstance(node,list):
            for value in node: age(value)
    age(expired)
    result["docs/contracts/fixtures/snapshot.expired.json"]=expired
    for name in result:
        if name.startswith("docs/contracts/fixtures/snapshot."):
            result[name]=seal_fixture(result[name])
    import sys
    sys.path.insert(0,str(ROOT))
    from pulse.wire.compact import encode
    for key,value in encode(result["docs/contracts/fixtures/snapshot.partial.json"]).items():
        result["docs/contracts/fixtures/wire/"+key+".json"]=value
    for key,value in live_fixtures(result["docs/contracts/fixtures/snapshot.partial.json"]).items():
        result["docs/contracts/fixtures/"+key]=value
    result["docs/contracts/fixtures/current_delta.boundaries.json"]=pairing_vectors()
    result["docs/contracts/fixtures/snapshot.classified_stable.json"]=classified_fixture()
    result["docs/contracts/fixtures/snapshot.classified_all_l2.json"]=all_classified_fixture()
    for key,value in encode(result["docs/contracts/fixtures/snapshot.classified_all_l2.json"]).items():
        result["docs/contracts/fixtures/wire_classified_all_l2/"+key+".json"]=value
    for key,value in encode(result["docs/contracts/fixtures/snapshot.classified_stable.json"]).items():
        result["docs/contracts/fixtures/wire_classified_stable/"+key+".json"]=value
    return result


def main(check=False):
    failed=[]
    for name,value in outputs().items():
        target=ROOT/name
        data=json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+"\n"
        if check:
            if not target.is_file() or target.read_text()!=data: failed.append(name)
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(data)
    if failed: raise SystemExit("contract_generation_drift: "+", ".join(failed))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check",action="store_true")
    main(parser.parse_args().check)
