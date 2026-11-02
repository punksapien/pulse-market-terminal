"""ADR-0030: existing fail-closed secret authorization policy."""
import hmac
import os


def _bearer_token(headers):
    value = str(headers.get("Authorization", "") or "").strip()
    if not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


def _authorise_secret(headers, env_names):
    expected = next(
        (os.environ.get(name) for name in env_names if os.environ.get(name)),
        "",
    )
    if not expected:
        # Local development remains usable without production secrets. Vercel
        # must be configured explicitly and fails closed.
        return os.environ.get("VERCEL") != "1", "missing_secret"
    supplied = _bearer_token(headers)
    return hmac.compare_digest(supplied, expected), "invalid_secret"
