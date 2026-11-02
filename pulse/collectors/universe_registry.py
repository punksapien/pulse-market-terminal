"""ADR-0131: admitted, resumable full-universe registry; no import-time I/O."""
import re

from .funding_inputs import hours
from pulse.store.runtime_document import RuntimeDocument, document
from pulse.wire.identity import digest

IDENTITY = "pulse.registry.all.v1"
MAX_PAIRS = 1024
DAY_MS = 86400000
PAIR = re.compile(r"B-([A-Z0-9]{1,24})_USDT")
RELEASE = "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('PEXPIRE',KEYS[1],1) end return 0"


def select_pairs(active, exchange):
    if (type(active) is not list or not active or len(active)>8192 or
            any(not isinstance(p,str) or not PAIR.fullmatch(p) for p in active) or
            len(set(active))!=len(active) or type(exchange) is not dict or
            type(exchange.get("symbols")) is not list or len(exchange["symbols"])>8192):
        raise ValueError("universe_source_shape")
    targets={PAIR.fullmatch(pair)[1]+"USDT" for pair in active}
    contracts=set()
    for row in exchange["symbols"]:
        if type(row) is not dict:raise ValueError("universe_contract_shape")
        if (row.get("status")!="TRADING" or row.get("contractType") not in ("PERPETUAL","TRADIFI_PERPETUAL")
                or row.get("quoteAsset")!="USDT" or row.get("marginAsset")!="USDT"):
            continue
        symbol=row.get("symbol")
        if not isinstance(symbol,str) or not 1<=len(symbol)<=128:
            raise ValueError("universe_contract_symbol")
        # Binance also lists symbols outside CoinDCX's requested universe.
        if symbol not in targets:continue
        if symbol in contracts:raise ValueError("universe_contract_duplicate")
        contracts.add(symbol)
    selected=[];excluded=[]
    for pair in sorted(active):
        symbol=PAIR.fullmatch(pair)[1]+"USDT"
        if symbol in contracts:selected.append({"pair_id":pair,"symbol":symbol})
        else:excluded.append(pair)
    if not 1<=len(selected)<=MAX_PAIRS:raise ValueError("universe_pair_bound")
    return selected,excluded


def _checked_progress(value, now):
    if (type(value) is not dict or set(value)!={"pairs","excluded","universe","discovered_ms"} or
            type(value["discovered_ms"]) is not int or not 0<=value["discovered_ms"]<=now+5000 or
            now>=value["discovered_ms"]+DAY_MS or type(value["pairs"]) is not list or
            not 1<=len(value["pairs"])<=MAX_PAIRS or type(value["universe"]) is not list or
            len(value["universe"])>len(value["pairs"]) or type(value["excluded"]) is not list):
        raise ValueError("registry_progress_invalid")
    if (any(not isinstance(p,str) or not PAIR.fullmatch(p) for p in value["pairs"]+value["excluded"]) or
            value["pairs"]!=sorted(set(value["pairs"])) or
            value["excluded"]!=sorted(set(value["excluded"])) or set(value["pairs"])&set(value["excluded"])):
        raise ValueError("registry_progress_identity")
    for index,row in enumerate(value["universe"]):
        pair=value["pairs"][index]
        if (type(row) is not dict or set(row)!={"id","pair_id","symbol","funding_hours"} or
                type(row["id"]) is not int or row["id"]!=index or row["pair_id"]!=pair or
                row["symbol"]!=PAIR.fullmatch(pair)[1]+"USDT" or type(row["funding_hours"]) is not int or
                row["funding_hours"] not in (1,4,8)):
            raise ValueError("registry_progress_identity")
    return value


def _publish(store, value, revision, now, expiry):
    outcome=store.publish(document(value,source_ms=now,expires_ms=expiry),expected_revision=revision)
    if outcome[0] not in (b"published",b"duplicate"):
        raise RuntimeError("registry_publication_refused")


def bootstrap_step(client, run_id, sources, *, batch_size=8):
    import json
    if type(batch_size) is not int or not 1<=batch_size<=8:raise ValueError("registry_batch_bound")
    store=RuntimeDocument(client,run_id,"source:registry_bootstrap",IDENTITY)
    store.acquire()
    try:
        now=sources.clock();prior=store.read(now_ms=now)
        if prior["status"]=="available":
            value=_checked_progress(json.loads(prior["payload"]),now)
        elif prior.get("reason") in ("document_missing","document_expired"):
            active=sources.request("/exchange/v1/derivatives/futures/data/active_instruments",
                {"margin_currency_short_name[]":"USDT"},host="https://api.coindcx.com")
            exchange=sources.request("/fapi/v1/exchangeInfo",{},weight=1)
            pairs,excluded=select_pairs(active,exchange)
            value=dict(pairs=[p["pair_id"] for p in pairs],excluded=excluded,universe=[],discovered_ms=sources.clock())
        else:raise ValueError("registry_progress_integrity")
        error=None
        for index in range(len(value["universe"]),min(len(value["pairs"]),len(value["universe"])+batch_size)):
            store.renew()
            pair=value["pairs"][index]
            try:
                raw=sources.request("/exchange/v1/derivatives/futures/data/instrument",
                    {"pair":pair,"margin_currency_short_name":"USDT"},host="https://api.coindcx.com")
                if type(raw) is not dict:raise ValueError("cohort_contract_identity")
                row=raw.get("instrument",raw)
                if (type(row) is not dict or row.get("pair")!=pair or row.get("quote_currency_short_name")!="USDT"
                        or row.get("settle_currency_short_name")!="USDT"):
                    raise ValueError("cohort_contract_identity")
                value["universe"].append(dict(id=index,pair_id=pair,symbol=PAIR.fullmatch(pair)[1]+"USDT",
                    funding_hours=hours(row.get("funding_frequency"))))
            except (ValueError,RuntimeError,OSError) as exc:
                error=exc
                break
        now=sources.clock();expiry=value["discovered_ms"]+DAY_MS
        _checked_progress(value,now)
        _publish(store,value,prior["revision"],now,expiry)
        if error is not None:raise error
        if len(value["universe"])<len(value["pairs"]):
            return dict(status="incomplete",completed=len(value["universe"]),total=len(value["pairs"]),excluded=value["excluded"])
        registry=RuntimeDocument(client,run_id,"source:registry",IDENTITY)
        registry.acquire()
        try:
            previous=registry.read(now_ms=now)
            result=dict(universe=value["universe"],mapping_version=digest(value["universe"]),source_ms=now,
                discovered_ms=value["discovered_ms"],excluded=value["excluded"])
            _publish(registry,result,previous["revision"],now,expiry)
            return dict(status="registry_ready",**result)
        finally:
            client.eval(RELEASE,1,registry.lease,registry.owner)
    finally:
        client.eval(RELEASE,1,store.lease,store.owner)


def load(client, run_id, mapping, *, now_ms):
    import json
    record=RuntimeDocument(client,run_id,"source:registry",IDENTITY).read(now_ms=now_ms)
    if record["status"]!="available":raise RuntimeError("registry_unavailable")
    value=json.loads(record["payload"])
    rows=value.get("universe")
    if type(rows) is not list:raise ValueError("registry_mapping_conflict")
    progress=dict(pairs=[r.get("pair_id") if type(r) is dict else None for r in rows],
        excluded=value.get("excluded"),universe=rows,discovered_ms=value.get("discovered_ms"))
    _checked_progress(progress,now_ms)
    if value.get("mapping_version")!=mapping or digest(rows)!=mapping:
        raise ValueError("registry_mapping_conflict")
    return rows
