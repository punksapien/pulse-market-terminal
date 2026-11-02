"""ADR-0085 fixed Preview wire URLs, read-only and compression-free."""
import hashlib
import json
import re
from .published_document import Response,error_response
from .encoded_artifact import artifact_response,negotiate
from pulse.store.published_artifacts import descriptor_valid
from pulse.wire.compact import PARTS

HASH=re.compile(r"^sha256:[a-f0-9]{64}$")


def route(query):
    if set(query)=={"horizon"} and query["horizon"] in (["1H"],["4H"],["24H"]):
        return "manifest","snapshot:"+query["horizon"][0].lower()+":manifest",10
    if query=={"live":["1"]}: return "live","live_funding_v1",3
    if query=={"live":["price"]}: return "live","live_price_v1",1
    if (set(query)=={"history"} and len(query["history"])==1
            and isinstance(query["history"][0],str) and re.fullmatch(r"[0-9]|[1-9][0-9]|[12][0-9]{2}|3[0-4][0-9]",query["history"][0])):
        return "live","history:"+str(int(query["history"][0])//10),10
    if set(query)=={"artifact"} and len(query["artifact"])==1 and isinstance(query["artifact"][0],str) and HASH.fullmatch(query["artifact"][0]):
        return "artifact",query["artifact"][0],0
    raise ValueError("invalid_wire_query")


def response(query,*,environment,store_factory,artifacts_factory,now_ms,accept_encoding=None,if_none_match=None,production_active=False):
    if environment!="preview" and not (environment=="production" and production_active): return error_response(404,"preview_only")
    try: kind,name,ttl=route(query)
    except (ValueError,TypeError): return error_response(400,"invalid_published_document_query")
    try:
        if kind=="artifact":
            artifacts=artifacts_factory()
            descriptor=artifacts.descriptor(name)
            return artifact_response(descriptor,artifacts.blob,accept_encoding=accept_encoding,
                if_none_match=if_none_match,cache_control="public, max-age=31536000, immutable")
        record=store_factory(name).read(now_ms=now_ms)
        if record["status"]!="available": return error_response(503,record["reason"])
        if not 0<=record["source_ms"]<record["expires_ms"] or record["source_ms"]>now_ms+5000 or now_ms>=record["expires_ms"]:
            raise ValueError("expired_pointer")
        body=record["payload"]
        if not isinstance(body,bytes) or len(body)>131072 or hashlib.sha256(body).hexdigest()!=record["digest"]:
            raise ValueError("pointer_integrity")
        value=json.loads(body)
        cache=f"public, max-age=0, s-maxage={min(ttl,max(0,(record['expires_ms']-now_ms)//1000))}, must-revalidate"
        if kind=="live":
            descriptor_valid(value)
            artifacts=artifacts_factory()
            if value["mapping_version"]!=artifacts.mapping_version: raise ValueError("mapping_mismatch")
            return artifact_response(value,artifacts.blob,accept_encoding=accept_encoding,
                if_none_match=if_none_match,cache_control=cache)
        if value.get("schema")!="pulse.manifest.v1" or set(value.get("parts",{}))!=set(PARTS) or value.get("horizon")!=query["horizon"][0] or value.get("expires_ms")!=record["expires_ms"] or value.get("generated_ms")!=record["source_ms"]:
            raise ValueError("invalid_manifest")
        if not isinstance(value.get("mapping_version"),str) or not HASH.fullmatch(value["mapping_version"]) or not isinstance(value.get("generation"),str) or not HASH.fullmatch(value["generation"]):
            raise ValueError("invalid_manifest_identity")
        if value["mapping_version"]!=artifacts_factory().mapping_version: raise ValueError("mapping_mismatch")
        for descriptor in value["parts"].values():
            descriptor_valid(descriptor,mapping_version=value["mapping_version"])
            if descriptor["generation"]!=value["generation"]: raise ValueError("mixed_manifest_generation")
        # The small manifest is identity-only. Respect explicit exclusions
        # without ever compressing on the viewer path.
        if negotiate(accept_encoding,{"identity":{}}) is None:
            return Response(406,{"Cache-Control":"no-store","Vary":"Accept-Encoding"},b"")
        etag='"'+record["digest"]+'"'
        headers={"Content-Type":"application/json","ETag":etag,"Vary":"Accept-Encoding",
            "Cache-Control":cache,"X-Content-Type-Options":"nosniff"}
        if if_none_match==etag: return Response(304,headers,b"")
        headers["Content-Length"]=str(len(body))
        return Response(200,headers,body)
    except Exception:
        return error_response(503,"published_document_unavailable")
