"""ADR-0130: explicit production transport scope; payload bytes are never rewritten."""
import re

from .staging_policy import staging_key, require_staging_url

LOGICAL = "pulse:stg:"
PHYSICAL = "pulse:prod:"
_GUARD = re.compile(r"string\.sub\(([^,\n]+),\s*1,\s*10\)(\s*[~=]+\s*)['\"]pulse:stg:['\"]")
_KEY_FIRST = frozenset(("get", "getrange", "set", "hget", "hmget", "strlen", "pttl", "ttl"))


def production_enabled(env):
    return (env.get("VERCEL_ENV") == "production" and env.get("PULSE_PRODUCTION_ENABLED") == "1"
            and env.get("PULSE_UNIVERSE_MODE") == "all")


def release_enabled(env):
    return env.get("PULSE_RELEASE_TOP350")=="1" and (env.get("VERCEL_ENV")=="preview" or production_enabled(env))


def dcx12_enabled(env):
    # ADR-0190: an old Preview exception never grants production access.
    if env.get("VERCEL_ENV")=="production":
        if env.get("PULSE_PREVIEW_DCX12")=="1":raise ValueError("preview_dcx_policy_scope")
        return production_enabled(env) and env.get("PULSE_PRODUCTION_DCX12_APPROVED")=="1"
    return env.get("VERCEL_ENV")=="preview" and env.get("PULSE_PREVIEW_DCX12")=="1"


def require_environment(env):
    if env.get("VERCEL_ENV") != "preview" and not production_enabled(env):
        raise ValueError("pulse_environment_disabled")


def connection_url(env):
    require_environment(env)
    flag = "PULSE_PRODUCTION_PLAINTEXT_APPROVED" if production_enabled(env) else "PULSE_STAGING_PLAINTEXT_APPROVED"
    return require_staging_url(env.get("PULSE_REDIS_URL"), approved_plaintext=env.get(flag) == "1")


def physical_key(key):
    return PHYSICAL + staging_key(key)[len(LOGICAL):]


def compile_script(script):
    binary = isinstance(script, bytes)
    if not isinstance(script, (str, bytes)):
        raise ValueError("production_script_type")
    text = script.decode("utf-8") if binary else script
    text = _GUARD.sub(lambda m: "string.sub(" + m[1] + ",1,11)" + m[2] + "'pulse:prod:'", text)
    if LOGICAL in text:
        raise ValueError("production_script_scope_uncompiled")
    return text.encode("utf-8") if binary else text


class ProductionScope:
    """Only Pulse's explicit bounded Redis operations; no generic execute_command."""
    def __init__(self, client):
        self._client = client
        self._loaded = set()

    def _arguments(self, name, args, kwargs):
        if name in _KEY_FIRST:
            if not args or "name" in kwargs:
                raise ValueError("production_key_required")
            return (physical_key(args[0]), *args[1:])
        if name in ("eval", "evalsha"):
            if kwargs or len(args) < 2 or type(args[1]) is not int or not 1 <= args[1] <= 167 or len(args) < 2 + args[1]:
                raise ValueError("production_script_keys")
            script, count = args[:2]
            if name == "eval":
                script = compile_script(script)
            elif script not in self._loaded:
                raise ValueError("production_script_not_loaded")
            return (script, count, *(physical_key(k) for k in args[2:2+count]), *args[2+count:])
        raise ValueError("production_command_refused")

    def __getattr__(self, name):
        if name not in _KEY_FIRST | {"eval", "evalsha"}:
            raise AttributeError("production_command_refused")
        def call(*args, **kwargs):
            mapped = self._arguments(name, args, kwargs)
            return getattr(self._client, name)(*mapped, **kwargs)
        return call

    def script_load(self, script):
        sha = self._client.script_load(compile_script(script))
        self._loaded.add(sha)
        return sha

    def info(self, section):
        if section != "memory":
            raise ValueError("production_info_refused")
        return self._client.info(section)

    def time(self):
        return self._client.time()

    def close(self):
        return self._client.close()

    def pipeline(self, transaction=False):
        if transaction is not False:
            raise ValueError("production_pipeline_transaction_refused")
        return _Pipeline(self, self._client.pipeline(transaction=False))


class _Pipeline:
    def __init__(self, scope, pipe):
        self._scope, self._pipe, self._count = scope, pipe, 0

    def __getattr__(self, name):
        if name not in _KEY_FIRST | {"eval", "evalsha"}:
            raise AttributeError("production_command_refused")
        def queue(*args, **kwargs):
            # MetricCapsule.write_many queues one lease renewal plus16 writes.
            if self._count >= 17:
                raise ValueError("production_pipeline_bound")
            mapped = self._scope._arguments(name, args, kwargs)
            getattr(self._pipe, name)(*mapped, **kwargs)
            self._count += 1
            return self
        return queue

    def execute(self):
        try:
            return self._pipe.execute()
        finally:
            self._count = 0


def scope_client(client, env):
    require_environment(env)
    return ProductionScope(client) if production_enabled(env) else client
