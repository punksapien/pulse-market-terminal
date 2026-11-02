"""ADR-0061/0190: allocation guard for staging and explicitly enabled production.

ADR-0129 retires the experimental 40 MB default. ADR-0192 raises the approved
allocation stop to 120 MB; an explicit lower diagnostic limit remains possible.
ADR-0066's capacity admission and reserve checks still apply independently."""
import os
from .staging_policy import MeasuredCapacity, PLAN_ALLOCATION_STOP_BYTES


def _stop_bytes(env=os.environ):
    raw = env.get("PULSE_MEMORY_STOP_BYTES")
    if raw is None:
        return PLAN_ALLOCATION_STOP_BYTES
    if not raw.isascii() or not raw.isdigit() or not 40000000 <= int(raw) <= PLAN_ALLOCATION_STOP_BYTES:
        raise ValueError("invalid_memory_stop_bytes")
    return int(raw)


STOP_BYTES = _stop_bytes()
RESERVE_BYTES = 1000000
FIELDS = ("used_memory", "used_memory_rss", "mem_fragmentation_ratio",
          "used_memory_peak", "used_memory_lua", "maxmemory", "maxmemory_policy")


class ExpansionStopped(RuntimeError):
    def __init__(self, reason, observation, *, additional_bytes=0):
        super().__init__(reason)
        self.observation = observation
        self.additional_bytes = additional_bytes


def guard(info, *, additional_bytes=0, approved_plan_fallback=False):
    observation = {key:info.get(key) for key in FIELDS}
    used = info.get("used_memory")
    if type(used) is not int or used < 0:
        raise ExpansionStopped("staging_memory_gate_unknown_usage", observation)
    if used >= STOP_BYTES:
        raise ExpansionStopped("staging_memory_gate_stop", observation)
    if info.get("maxmemory_policy") != "noeviction":
        raise ExpansionStopped("staging_eviction_policy_refused", observation)
    capacity = MeasuredCapacity.from_info(info, provider_usable_bytes=125000000,
                                         approved_plan_fallback=approved_plan_fallback)
    if type(additional_bytes) is not int or additional_bytes < 0:
        raise ValueError("invalid_expansion_bytes")
    if (used + additional_bytes + RESERVE_BYTES >= STOP_BYTES
            or not capacity.admit(additional_bytes, reserve_bytes=RESERVE_BYTES)):
        raise ExpansionStopped("staging_memory_gate_headroom_denied", observation, additional_bytes=additional_bytes)
    return capacity, observation
