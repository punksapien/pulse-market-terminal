"""Independent Draft2020-12 validation with only locally registered URN refs."""
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from semantics import validate_snapshot
from snapshot_spec import SNAPSHOT
from live_spec import validate_live

ROOT=Path(__file__).resolve().parents[2]


def registry():
    resources=[]
    schemas={}
    for path in sorted((ROOT/"docs/contracts/schema").glob("*.schema.json")):
        schema=json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)
        if schema["$id"] in schemas: raise ValueError("duplicate_schema_id")
        schemas[schema["$id"]]=schema
        resources.append((schema["$id"],Resource.from_contents(schema)))
    return Registry().with_resources(resources),schemas


def run():
    local,_=registry()
    fixtures=json.loads((ROOT/"docs/contracts/fixtures/metric_fields.v1.json").read_text())
    checks=0
    for case in fixtures["cases"]:
        validator=Draft202012Validator(case["schema"],registry=local)
        for status in ("valid","unavailable"):
            validator.validate(case[status]); checks+=1
        invalid={**case["unavailable"],"value":"0"}
        if validator.is_valid(invalid): raise AssertionError("unavailable_zero_accepted")
        invalid={**case["valid"],"value":0.1}
        if validator.is_valid(invalid): raise AssertionError("numeric_float_accepted")
        invalid={**case["valid"],"renamed_field":"x"}
        if validator.is_valid(invalid): raise AssertionError("unknown_property_accepted")
        checks+=3
    print(json.dumps({"status":"passed","field_checks":checks,"network":False}))
    documents=0
    for path in sorted((ROOT/"docs/contracts/fixtures").glob("snapshot.*.json")):
        value=json.loads(path.read_text())
        Draft202012Validator({"$ref":SNAPSHOT},registry=local).validate(value)
        validate_snapshot(value)
        documents+=1
    print(json.dumps({"status":"passed","snapshot_fixtures":documents,"network":False}))
    main=json.loads((ROOT/"docs/contracts/fixtures/snapshot.partial.json").read_text())
    live_count=0
    for path in sorted((ROOT/"docs/contracts/fixtures").glob("live_*.json")):
        value=json.loads(path.read_text())
        kind="funding" if path.name.startswith("live_funding") else "price"
        Draft202012Validator({"$ref":"urn:pulse:schema:v1:live_"+kind},registry=local).validate(value)
        if ".mapping_mismatch." in path.name:
            try: validate_live(value,main["universe"],main["mapping_version"])
            except ValueError as exc:
                if str(exc)!="mapping_mismatch": raise
            else: raise AssertionError("foreign_mapping_accepted")
        else: validate_live(value,main["universe"],main["mapping_version"])
        live_count+=1
    print(json.dumps({"status":"passed","live_fixtures_including_expected_refusals":live_count,"network":False}))


if __name__=="__main__": run()
