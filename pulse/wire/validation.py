"""ADR-0093 exact packaged contract gates; no remote JSON-schema resolution."""
from functools import lru_cache
import json
from pathlib import Path
from .semantics import validate_snapshot, validate_live


@lru_cache(maxsize=1)
def validators():
    from jsonschema import Draft202012Validator
    from jsonschema.validators import extend
    from .schema_cache import SuccessfulReferences
    from referencing import Registry,Resource
    schemas=[json.loads(p.read_text()) for p in Path(__file__).with_name("schemas").glob("*.schema")]
    local=Registry().with_resources((s["$id"],Resource.from_contents(s)) for s in schemas)
    cached=extend(Draft202012Validator,{"$ref":SuccessfulReferences(Draft202012Validator.VALIDATORS["$ref"])})
    return {s["$id"]:cached(s,registry=local) for s in schemas}


def snapshot(value):
    from jsonschema.exceptions import ValidationError
    try:validators()["urn:pulse:schema:v1:snapshot"].validate(value)
    except ValidationError as exc:
        # Only schema-owned property names and bounded row indices, never values.
        import re
        path=list(exc.absolute_path)
        safe=[str(p) for p in path[:8] if (type(p) is int and 0<=p<1024)
            or (type(p) is str and re.fullmatch(r"[a-z_0-9]{1,32}",p))]
        raise ValueError(("snapshot_schema:"+":".join(safe))[:100]) from None
    validate_snapshot(value)


def live(value, universe, mapping_version):
    kind={"pulse.live_price.v1":"price","pulse.live_funding.v1":"funding"}.get(value.get("schema"))
    if kind is None: raise ValueError("unknown_live_schema")
    validators()["urn:pulse:schema:v1:live_"+kind].validate(value)
    validate_live(value,universe,mapping_version)
