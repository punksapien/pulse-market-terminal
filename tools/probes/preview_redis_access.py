"""ADR-0052/0056: reviewed Preview identity gate; no cleanup entry point."""
import argparse
import json
import os
import subprocess
import time

STORE = "store_1MV2ApAK4SbmM0V9"
PROJECT = "prj_Iu3ENBLVf3uLtfVjshOISANFzrPN"
SCOPE = "punksapien-projects"


def api(cli, endpoint):
    process = subprocess.run(["node", cli, "api", endpoint, "--scope", SCOPE, "--raw"],
                             capture_output=True, text=True, timeout=25, check=False)
    if process.returncode:
        raise RuntimeError("control_plane_read_failed")
    try:
        return json.loads(process.stdout)
    except ValueError:
        raise RuntimeError("control_plane_invalid_json") from None


def preview_url(cli):
    store = api(cli, f"/v1/storage/stores/{STORE}")["store"]
    if (store.get("id"), store.get("name"), store.get("billingPlan", {}).get("id"), store.get("metadata", {}).get("Region"), store.get("metadata", {}).get("HighAvailability")) != (STORE,"volatility-terminal-preview","26480","sin1","None"):
        raise RuntimeError("preview_resource_identity_failed")
    links = store.get("projectsMetadata", [])
    if len(links) != 1 or links[0].get("projectId") != PROJECT or links[0].get("environments") != ["preview"] or links[0].get("environmentVariables") != ["REDIS_URL"]:
        raise RuntimeError("preview_binding_failed")
    # Filter at the API boundary: no Production environment is requested.
    envs = api(cli, f"/v10/projects/{PROJECT}/env?target=preview")["envs"]
    entries = [env for env in envs if env.get("key") == "REDIS_URL" and not env.get("gitBranch")]
    if len(entries) != 1 or entries[0].get("target") != ["preview"]:
        raise RuntimeError("preview_env_scope_failed")
    secret = api(cli, f"/v1/projects/{PROJECT}/env/{entries[0]['id']}")
    if secret.get("key") != "REDIS_URL" or secret.get("target") != ["preview"]:
        raise RuntimeError("preview_secret_scope_failed")
    url = secret.get("value", "")
    if not isinstance(url,str) or not url.startswith(("redis://", "rediss://")):
        raise RuntimeError("preview_credential_unavailable")
    return url


def connect(cli):
    import redis
    url = preview_url(cli)
    # No implicit retry on timeout; never resolve DCXVOL_REDIS_URL.
    return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=3,
                                max_connections=1, retry_on_timeout=False,
                                decode_responses=False)


def state(client):
    memory = client.info("memory")
    return {"used_memory_bytes":memory.get("used_memory"),
            "maxmemory":memory.get("maxmemory"),
            "eviction_policy":memory.get("maxmemory_policy"),"dbsize":client.dbsize()}
