"""ADR-0058: fail-closed staging identity, key scope and measured capacity gates.

Application scope checks are defence in depth, not a Redis ACL security boundary.
This module does not authorise a collector, source call, deletion or cutover.
"""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit, parse_qs


PREFIX = "pulse:stg:"
# ADR-0192: decimal bytes; only the verified 125 MB entitlement gets this ceiling.
PLAN_ALLOCATION_STOP_BYTES = 120000000


def staging_key(key):
    if (not isinstance(key, str) or not key.startswith(PREFIX)
            or len(key) <= len(PREFIX) or len(key) > 256
            or not re.fullmatch(r"[A-Za-z0-9_:{}.-]+", key)):
        raise ValueError("staging_key_scope_refused")
    return key


def _require_connection_url(url, schemes):
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        if parts is None or parts.scheme not in schemes:
            raise ValueError()
        if (not parts.hostname or not parts.password or parts.fragment
                or parts.path not in ("", "/", "/0") or not parts.port):
            raise ValueError()
        # Connection security options come from trusted code, not URL parameters.
        if parse_qs(parts.query) or parts.query:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("staging_tls_url_refused") from None
    return url


def require_tls_url(url):
    """Default stays TLS-only; never rewrite a plaintext connection URL."""
    return _require_connection_url(url, ("rediss",))


def require_staging_url(url, *, approved_plaintext=False):
    """ADR-0059: explicit operator exception, not a production/TLS fallback."""
    if type(approved_plaintext) is not bool:
        raise ValueError("invalid_staging_exception")
    return _require_connection_url(url, ("rediss", "redis") if approved_plaintext else ("rediss",))


def require_identity(resource, project):
    """Resource metadata only; never request the Production credential."""
    if (resource.get("id") != "store_UEHUkH5XFJYP1hur"
            or resource.get("name") != "pulse-redis"
            or resource.get("status") != "available"
            or resource.get("billingPlan", {}).get("id") != "26515"
            or resource.get("metadata") != {
                "StorageType": "RAM only", "Region": "bom1",
                "HighAvailability": "Single zone"}):
        raise ValueError("staging_resource_identity_refused")
    matches = [link for link in resource.get("projectsMetadata", [])
               if link.get("projectId") == project
               and link.get("environments") == ["preview"]
               and link.get("envVarPrefix") == "PULSE_"
               and link.get("environmentVariables") == ["PULSE_REDIS_URL"]]
    if project != "prj_Iu3ENBLVf3uLtfVjshOISANFzrPN" or len(matches) != 1:
        raise ValueError("staging_binding_refused")


@dataclass(frozen=True)
class MeasuredCapacity:
    ceiling_bytes: int
    used_bytes: int
    capacity_source: str = "min_info_and_verified_plan"

    @classmethod
    def from_info(cls, info, *, provider_usable_bytes, approved_plan_fallback=False):
        measured = info.get("maxmemory")
        used = info.get("used_memory")
        if (type(approved_plan_fallback) is not bool
                or type(used) is not int or used < 0
                or type(provider_usable_bytes) is not int or provider_usable_bytes <= 0):
            raise ValueError("staging_memory_ceiling_unverified")
        # ADR-0059: explicit user-approved staging exception for this provider's
        # hidden maxmemory, never an assertion that runtime maxmemory was read.
        if approved_plan_fallback and (measured is None or type(measured) is int and measured == 0):
            return cls(provider_usable_bytes, used, "verified_plan_staging_exception")
        if type(measured) is not int or measured <= 0:
            raise ValueError("staging_memory_ceiling_unverified")
        # A node-wide maxmemory larger than the purchased dataset must not grant
        # capacity beyond the provider's separately verified usable entitlement.
        return cls(min(measured, provider_usable_bytes), used)

    @property
    def thresholds(self):
        return {percent: self.ceiling_bytes * percent // 100
                for percent in (70, 80, 85, 90, 100)}

    def admit(self, additional_bytes, *, reserve_bytes):
        if (type(additional_bytes) is not int or additional_bytes < 0
                or type(reserve_bytes) is not int or reserve_bytes < 0):
            raise ValueError("invalid_memory_reservation")
        limit = PLAN_ALLOCATION_STOP_BYTES if self.ceiling_bytes == 125000000 else self.thresholds[80]
        return self.used_bytes + additional_bytes + reserve_bytes < limit

    def pressure(self, *, previous="normal"):
        if previous not in ("normal", "admission_paused", "bootstrap_repair_paused", "hard_stop"):
            raise ValueError("invalid_pressure_state")
        if self.ceiling_bytes == 125000000:
            if self.used_bytes >= PLAN_ALLOCATION_STOP_BYTES:
                return "hard_stop"
            if previous == "hard_stop" and self.used_bytes >= 105000000:
                return "hard_stop"
            if self.used_bytes >= 110000000:
                return "bootstrap_repair_paused"
            if previous == "bootstrap_repair_paused" and self.used_bytes >= 105000000:
                return "bootstrap_repair_paused"
            return "normal"
        if self.used_bytes >= self.thresholds[90]:
            return "hard_stop"
        if previous == "hard_stop" and self.used_bytes >= self.thresholds[80]:
            return "hard_stop"
        if self.used_bytes >= self.thresholds[85]:
            return "bootstrap_repair_paused"
        if previous == "bootstrap_repair_paused" and self.used_bytes >= self.thresholds[80]:
            return "bootstrap_repair_paused"
        if self.used_bytes >= self.thresholds[80]:
            return "admission_paused"
        return "normal"
