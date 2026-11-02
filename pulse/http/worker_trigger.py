"""ADR-0094 Preview-only authenticated POST boundary; no import-time clients."""
import hmac
import re
from .published_document import error_response, Response
import json
from pulse.store.production_scope import production_enabled


def response(*, method, authorization, content_length, query, environment, config, runner):
    active=environment=="production" and production_enabled(config)
    if not ((environment=="preview" and method=="POST") or (active and method in ("POST","GET"))):
        return error_response(404,"preview_only")
    secret=config.get("CRON_SECRET" if method=="GET" else "PULSE_WORKER_SECRET")
    if (not isinstance(secret,str) or not 32<=len(secret)<=256 or not secret.isascii()
            or not isinstance(authorization,str) or len(authorization)>300
            or not hmac.compare_digest(authorization,"Bearer "+secret)):
        return error_response(401,"worker_unauthorized")
    if content_length not in (None,"0") or query:
        return error_response(400,"worker_payload_refused")
    if config.get("PULSE_WORKER_ENABLED")!="1": return error_response(503,"worker_disabled")
    run_id,mapping=config.get("PULSE_RUNTIME_RUN_ID"),config.get("PULSE_MAPPING_VERSION")
    if (not isinstance(run_id,str) or not re.fullmatch(r"[a-f0-9]{32}",run_id)
            or not isinstance(mapping,str) or not re.fullmatch(r"sha256:[a-f0-9]{64}",mapping)):
        return error_response(503,"worker_scope_unconfigured")
    try:
        value=runner(run_id=run_id,mapping_version=mapping,duration_ms=55000)
        if type(value) is not dict or value.get("status") not in ("published","incomplete","lease_busy"):
            raise ValueError("worker_result_refused")
        code=409 if value["status"]=="lease_busy" else 200
        return Response(code,{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store"},
                        json.dumps(value,allow_nan=False,separators=(",",":")).encode())
    except Exception as exc:
        # Provider and client exceptions may contain connection details, so only the class name is returned for
        # them; this codebase's own errors carry fixed short codes, returned only when they match that grammar,
        # so a failed run can be diagnosed from the response without exposing free text.
        body={"status":"unavailable","reason":"worker_run_failed","error_type":type(exc).__name__}
        stage = getattr(exc,"worker_stage",None)
        if stage in ("initialise","shared_inputs","all_pair_collect","hot_collect","detail_collect","reference_repair","funding_collect","ls_tip_collect","collect","compose","promote"):
            body["stage"] = stage
        # only fixed snake_case codes are shown; anything else (a URL, a path, free text) is withheld
        if isinstance(exc,(RuntimeError,ValueError,KeyError,TypeError)) and re.fullmatch(r"[a-z0-9_:]{1,100}",str(exc)): body["detail"]=str(exc)
        print(json.dumps({"event":"pulse_worker_failed",**body},separators=(",",":")),flush=True)
        return Response(503,{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store"},
                        json.dumps(body,allow_nan=False,separators=(",",":")).encode())
