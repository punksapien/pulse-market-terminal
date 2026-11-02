"""ADR-0074 fixed shared URLs; reads never collect, rebuild or acquire a lease."""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


def error_response(status,reason):
    return Response(status,{"Content-Type":"application/json","Cache-Control":"no-store"},
                    json.dumps({"status":"unavailable","reason":reason},separators=(",",":")).encode())


def channel(query):
    # Four shared URLs only; filters, arbitrary symbols and UTM params aren't API keys.
    if query=={"live":["1"]}: return "live_funding",5
    if query.keys()=={"horizon"} and query["horizon"] in (["1H"],["4H"],["24H"]):
        return "snapshot:"+query["horizon"][0].lower(),10
    raise ValueError("invalid_published_document_query")


def read_response(store,*,now_ms,max_cache_seconds,if_none_match=None):
    if type(max_cache_seconds) is not int or not 1<=max_cache_seconds<=10:
        raise ValueError("invalid_cache_budget")
    record=store.read(now_ms=now_ms)
    if record["status"]!="available": return error_response(503,record["reason"])
    ttl=min(max_cache_seconds,max(0,(record["expires_ms"]-now_ms)//1000))
    etag='"'+record["digest"]+'"'
    headers={"Content-Type":"application/json","ETag":etag,
             "Vary":"Accept-Encoding",
             "Cache-Control":f"public, max-age=0, s-maxage={ttl}, must-revalidate",
             "X-Content-Type-Options":"nosniff"}
    if if_none_match==etag: return Response(304,headers,b"")
    return Response(200,headers,record["payload"])
