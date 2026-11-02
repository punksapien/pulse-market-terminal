"""ADR-0143: deterministic, offline release membership; no exchange/store I/O."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .universe_registry import PAIR, MAX_PAIRS
from pulse.wire.identity import digest

RELEASE_SIZE = 350
IDENTITY = "pulse.release.top350.v1"


def capsule_run(env, run_id):
    """Preview may reuse raw staging history, never production state or identities."""
    value=env.get("PULSE_CAPSULE_RUN_ID",run_id)
    if value != run_id:
        import re
        if (env.get("VERCEL_ENV")!="preview" or env.get("PULSE_RELEASE_TOP350")!="1" or
                not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{32}",value)):
            raise ValueError("release_capsule_scope_refused")
    return value


def load(client,run_id,mapping,*,now_ms):
    """Load immutable membership, not a new volume ranking on each invocation."""
    import json
    from pulse.store.runtime_document import RuntimeDocument
    record=RuntimeDocument(client,run_id,"source:release350",IDENTITY).read(now_ms=now_ms)
    if record["status"]!="available":raise RuntimeError("release_manifest_unavailable")
    value=json.loads(record["payload"])
    rows=value.get("universe")
    if (value.get("policy")!="top350.quote_24h_usdt.v1" or type(rows) is not list or
            len(rows)!=RELEASE_SIZE or value.get("mapping_version")!=mapping or digest(rows)!=mapping):
        raise ValueError("release_manifest_identity")
    pairs=[]
    for index,row in enumerate(rows):
        if (type(row) is not dict or set(row)!={"id","pair_id","symbol","funding_hours"} or
                type(row["id"]) is not int or row["id"]!=index or
                not isinstance(row["pair_id"],str) or not PAIR.fullmatch(row["pair_id"]) or
                row["symbol"]!=PAIR.fullmatch(row["pair_id"])[1]+"USDT" or
                type(row["funding_hours"]) is not int or row["funding_hours"] not in (1,4,8)):
            raise ValueError("release_manifest_identity")
        pairs.append(row["pair_id"])
    if pairs!=sorted(set(pairs)):raise ValueError("release_manifest_identity")
    return rows


def bootstrap_refused(*args,**kwargs):
    raise RuntimeError("release_manifest_operator_required")


def freeze_top_volume(universe, bulk, *, now_ms):
    """Rank every eligible pair, refusing incomplete witnesses instead of guessing."""
    if (type(now_ms) is not int or now_ms < 0 or type(bulk) is not dict or
            type(bulk.get("ts")) is not int or
            not now_ms-60000 < bulk["ts"] <= now_ms+5000 or
            type(bulk.get("prices")) is not dict):
        raise ValueError("release_volume_clock")
    if type(universe) is not list or not RELEASE_SIZE <= len(universe) <= MAX_PAIRS:
        raise ValueError("release_universe_size")
    ranked=[]
    seen=set()
    for row in universe:
        if (type(row) is not dict or set(row)!={"id","pair_id","symbol","funding_hours"} or
                type(row["id"]) is not int or type(row["funding_hours"]) is not int or
                row["funding_hours"] not in (1,4,8) or
                not isinstance(row["pair_id"],str) or not PAIR.fullmatch(row["pair_id"]) or
                row["symbol"] != PAIR.fullmatch(row["pair_id"])[1]+"USDT" or
                row["pair_id"] in seen):
            raise ValueError("release_universe_identity")
        seen.add(row["pair_id"])
        point=bulk["prices"].get(row["pair_id"])
        if (type(point) is not dict or point.get("mkt") != row["symbol"] or
                type(point.get("v")) not in (str,int,Decimal)):
            raise ValueError("release_volume_missing_or_invalid")
        try:
            volume=Decimal(point["v"])
        except (InvalidOperation, ValueError):
            raise ValueError("release_volume_missing_or_invalid") from None
        if not volume.is_finite() or volume < 0:
            raise ValueError("release_volume_missing_or_invalid")
        ranked.append((volume,row["pair_id"],row))
    ranked.sort(key=lambda item:item[1])
    ranked.sort(key=lambda item:item[0],reverse=True)
    selected=sorted((deepcopy(item[2]) for item in ranked[:RELEASE_SIZE]),key=lambda row:row["pair_id"])
    for index,row in enumerate(selected):
        row["id"]=index
    witness=[dict(pair_id=pair,quote_24h_usdt=str(volume)) for volume,pair,_ in ranked]
    return dict(policy="top350.quote_24h_usdt.v1",source_ms=bulk["ts"],
                universe=selected,mapping_version=digest(selected),ranking=witness,
                ranking_sha256=digest(witness))
