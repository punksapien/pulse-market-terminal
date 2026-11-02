"""TLS helpers for exchange probes.

Verification always stays on (never disable it). On networks that inspect TLS with a
corporate CA (e.g. the CoinDCX office proxy for Bybit and Vercel hosts), set
PULSE_EXTRA_CA_FILE to that CA's PEM; it is added to certifi's bundle. Python 3.13+
strict X.509 checks reject CAs without an Authority Key Identifier, so only
VERIFY_X509_STRICT is cleared — the chain is still verified.
"""
import os
import ssl

import certifi
import requests
from requests.adapters import HTTPAdapter

EXTRA_CA = os.environ.get("PULSE_EXTRA_CA_FILE")


def ssl_context():
    ctx = ssl.create_default_context(cafile=certifi.where())
    if EXTRA_CA and os.path.exists(EXTRA_CA):
        ctx.load_verify_locations(EXTRA_CA)
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


class _CtxAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = ssl_context()
        return super().init_poolmanager(*args, **kwargs)


def session():
    s = requests.Session()
    s.headers["User-Agent"] = "pulse-probe/0.1"
    s.mount("https://", _CtxAdapter())
    return s
