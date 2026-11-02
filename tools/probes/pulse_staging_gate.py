"""ADR-0058: read-only Pulse Preview identity/TLS/capacity preflight.

No exchange requests, Redis writes, scripts, deletes or Production secret reads.
Refuses plaintext unless the explicit staging exception is supplied. Never prints
the URL. The default remains TLS-only, with a measured runtime capacity requirement.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pulse.store.staging_policy import require_identity, require_staging_url, MeasuredCapacity
from preview_redis_access import api, PROJECT


def connect_staging(cli, *, approved_plaintext=False):
    resource = api(cli, "/v1/storage/stores/store_UEHUkH5XFJYP1hur")["store"]
    require_identity(resource, PROJECT)
    entries = api(cli, f"/v10/projects/{PROJECT}/env?target=preview")["envs"]
    entries = [entry for entry in entries if entry.get("key") == "PULSE_REDIS_URL"
               and entry.get("target") == ["preview"] and not entry.get("gitBranch")]
    if len(entries) != 1:
        raise RuntimeError("staging_preview_entry_refused")
    entry = api(cli, f"/v1/projects/{PROJECT}/env/{entries[0]['id']}")
    if entry.get("key") != "PULSE_REDIS_URL" or entry.get("target") != ["preview"]:
        raise RuntimeError("staging_preview_secret_scope_refused")
    result = {"resource":"pulse-redis", "region":"bom1", "environment":"preview",
              "variable":"PULSE_REDIS_URL", "identity_gate":True,
              "namespace":"pulse:stg:", "redis_connected":False,
              "redis_writes":0, "exchange_calls":0, "production_secret_read":False}
    url = require_staging_url(entry.get("value"), approved_plaintext=approved_plaintext)
    import redis
    secure = url.startswith("rediss://")
    tls_options = {"ssl_cert_reqs":"required", "ssl_check_hostname":True} if secure else {}
    result.update(transport="tls" if secure else "plaintext_approved_staging_exception")
    client = redis.Redis.from_url(url, **tls_options, socket_connect_timeout=2, socket_timeout=3,
                                  max_connections=1, retry_on_timeout=False)
    return client, result


def run(cli, *, approved_plaintext=False, diagnose_memory=False, approved_plan_fallback=False):
    try:
        client, result = connect_staging(cli, approved_plaintext=approved_plaintext)
    except ValueError as error:
        if str(error) != "staging_tls_url_refused":
            raise
        return {"status":"blocked_tls_url_not_secure_or_invalid", "redis_connected":False,
                "memory_ceiling_verified":False, "staging_complete":False}
    try:
        # Successful certificate-validated INFO implies the TLS handshake succeeded.
        info = client.info("memory")
        result.update(redis_connected=True, tls_certificate_validated=result["transport"]=="tls",
                      observed_maxmemory=info.get("maxmemory"),
                      observed_used_memory=info.get("used_memory"),
                      observed_eviction_policy=info.get("maxmemory_policy"))
        if diagnose_memory:
            result["memory_info_fields"] = sorted(info)
            try:
                config = client.config_get("maxmemory")
                raw_limit = config.get("maxmemory", config.get(b"maxmemory"))
                if isinstance(raw_limit, bytes):
                    raw_limit = raw_limit.decode("ascii")
                result["config_maxmemory"] = int(raw_limit) if raw_limit is not None else None
                result["config_maxmemory_returned_entries"] = len(config)
            except redis.ResponseError:
                result["config_maxmemory"] = "provider_denied"
        try:
            capacity = MeasuredCapacity.from_info(info, provider_usable_bytes=125000000,
                                                 approved_plan_fallback=approved_plan_fallback)
        except ValueError:
            return dict(result, status="blocked_memory_ceiling_unverified", staging_complete=False)
        return dict(result, status="preflight_passed_not_staging_acceptance",
                    ceiling_bytes=capacity.ceiling_bytes, thresholds=capacity.thresholds,
                    capacity_source=capacity.capacity_source,
                    staging_complete=False)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli", required=True)
    parser.add_argument("--approved-staging-plaintext", action="store_true",
                        help="Explicit 26 Oct approval: new Pulse staging only, not production")
    parser.add_argument("--diagnose-memory", action="store_true",
                        help="Read-only INFO field list and CONFIG GET maxmemory; no configuration changes")
    parser.add_argument("--approved-plan-capacity", action="store_true",
                        help="Explicit staging approval: verified 125 MB entitlement if maxmemory is hidden")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.vercel_cli, approved_plaintext=args.approved_staging_plaintext,
                             diagnose_memory=args.diagnose_memory,
                             approved_plan_fallback=args.approved_plan_capacity), indent=2))
    except Exception as error:
        # Provider/client exception strings can embed connection details.
        print(json.dumps({"status":"preflight_failed", "error_type":type(error).__name__,
                          "staging_complete":False}))
        raise SystemExit(1) from None
