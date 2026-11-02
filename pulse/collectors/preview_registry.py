"""ADR-0097 real metadata bootstrap; no default contract intervals."""
from .preview_cohort import PAIRS
from .funding_inputs import hours
from pulse.wire.identity import digest
from pulse.store.runtime_document import RuntimeDocument,document

IDENTITY=digest(PAIRS)


def bootstrap(client,run_id,sources):
    store=RuntimeDocument(client,run_id,"source:registry",IDENTITY);store.acquire()
    active=sources.request("/exchange/v1/derivatives/futures/data/active_instruments",
        {"margin_currency_short_name[]":"USDT"},host="https://api.coindcx.com")
    exchange=sources.request("/fapi/v1/exchangeInfo",{},weight=1)
    if type(active) is not list or len(active)>4096 or type(exchange) is not dict:
        raise ValueError("universe_source_shape")
    contracts={row["symbol"] for row in exchange.get("symbols",[]) if row.get("status")=="TRADING"
        and row.get("contractType") in ("PERPETUAL","TRADIFI_PERPETUAL")
        and row.get("quoteAsset")==row.get("marginAsset")=="USDT"}
    universe=[]
    for index,pair in enumerate(PAIRS):
        store.renew()
        if pair["pair_id"] not in active or pair["symbol"] not in contracts:
            raise ValueError("cohort_contract_inactive")
        raw=sources.request("/exchange/v1/derivatives/futures/data/instrument",
            {"pair":pair["pair_id"],"margin_currency_short_name":"USDT"},host="https://api.coindcx.com")
        raw=raw.get("instrument",raw)
        if (raw.get("pair")!=pair["pair_id"] or raw.get("quote_currency_short_name")!="USDT"
                or raw.get("settle_currency_short_name")!="USDT"):
            raise ValueError("cohort_contract_identity")
        universe.append({"id":index,**pair,"funding_hours":hours(raw.get("funding_frequency"))})
    now=sources.clock();prior=store.read(now_ms=now)
    result={"universe":universe,"mapping_version":digest(universe),"source_ms":now}
    outcome=store.publish(document(result,source_ms=now,expires_ms=now+86400000),expected_revision=prior["revision"])
    if outcome[0] not in (b"published",b"duplicate"): raise RuntimeError("registry_publication_refused")
    return result


def load(client,run_id,mapping,*,now_ms):
    import json
    record=RuntimeDocument(client,run_id,"source:registry",IDENTITY).read(now_ms=now_ms)
    if record["status"]!="available": raise RuntimeError("registry_unavailable")
    value=json.loads(record["payload"])
    if (value.get("mapping_version")!=mapping or digest(value.get("universe"))!=mapping
            or len(value["universe"])!=len(PAIRS)
            or any({k:row[k] for k in ("pair_id","symbol")}!=pair
                   for row,pair in zip(value["universe"],PAIRS))):
        raise ValueError("registry_mapping_conflict")
    return value["universe"]
