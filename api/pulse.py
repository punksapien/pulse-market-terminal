"""ADR-0130: explicitly gated published reader; no collector imports or source calls."""
from http.server import BaseHTTPRequestHandler
import os
import threading
import time
from urllib.parse import parse_qs, urlsplit

from pulse.http.published_document import channel, read_response, error_response
from pulse.store.runtime_document import RuntimeDocument
from pulse.store.production_scope import connection_url, scope_client, production_enabled, require_environment

_pool=None
_lock=threading.Lock()


def response(query,*,environment,store_factory,now_ms,if_none_match=None,production_active=False):
    if environment!="preview" and not (environment=="production" and production_active): return error_response(404,"preview_only")
    try:
        name,ttl=channel(query)
    except ValueError:
        return error_response(400,"invalid_published_document_query")
    try:
        return read_response(store_factory(name),now_ms=now_ms,max_cache_seconds=ttl,if_none_match=if_none_match)
    except Exception:
        return error_response(503,"published_document_unavailable")


def store_factory(name):
    return RuntimeDocument(redis_client(),os.environ.get("PULSE_RUNTIME_RUN_ID",""),name,
                           os.environ.get("PULSE_MAPPING_VERSION",""))


def redis_client():
    global _pool
    # Defence in depth if invoked outside the HTTP wrapper.
    require_environment(os.environ)
    with _lock:
        if _pool is None:
            url=connection_url(os.environ)
            import redis
            options={"ssl_cert_reqs":"required","ssl_check_hostname":True} if url.startswith("rediss://") else {}
            _pool=redis.ConnectionPool.from_url(url,max_connections=4,socket_connect_timeout=2,
                socket_timeout=3,retry_on_timeout=False,**options)
        import redis
        return scope_client(redis.Redis(connection_pool=_pool),os.environ)


def artifacts_factory():
    from pulse.store.published_artifacts import PublishedArtifacts
    return PublishedArtifacts(redis_client(),os.environ.get("PULSE_RUNTIME_RUN_ID",""),
                              os.environ.get("PULSE_MAPPING_VERSION",""))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            query=parse_qs(urlsplit(self.path).query,keep_blank_values=True,max_num_fields=4)
        except ValueError:
            self.send_error(400); return
        if os.environ.get("PULSE_WIRE_V1_ENABLED")=="1":
            from pulse.http.wire_reader import response as wire_response
            result=wire_response(query,environment=os.environ.get("VERCEL_ENV"),store_factory=store_factory,
                artifacts_factory=artifacts_factory,now_ms=time.time_ns()//1000000,
                accept_encoding=self.headers.get("Accept-Encoding"),if_none_match=self.headers.get("If-None-Match"),
                production_active=production_enabled(os.environ))
        else:
            result=response(query,environment=os.environ.get("VERCEL_ENV"),store_factory=store_factory,
                            now_ms=time.time_ns()//1000000,if_none_match=self.headers.get("If-None-Match"),
                            production_active=production_enabled(os.environ))
        self.send_response(result.status)
        for name,value in result.headers.items(): self.send_header(name,value)
        self.end_headers()
        if result.body: self.wfile.write(result.body)
