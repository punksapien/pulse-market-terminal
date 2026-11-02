"""ADR-0073: pure settled-stage marginal estimates, not a capacity guarantee."""
import math
from statistics import median

from .expansion_guard import FIELDS, guard


def settled_sample(client, *, wait, clock):
    wait(2)
    samples = []
    for index in range(5):
        if index:
            wait(1)
        info = client.info("memory")
        guard(info, additional_bytes=8*65536, approved_plan_fallback=True)
        samples.append({"observed_at_ms": clock(),
                        **{key: info.get(key) for key in FIELDS}})
    return {"samples": samples,
            "used_memory_median": median(s["used_memory"] for s in samples),
            "used_memory_min": min(s["used_memory"] for s in samples),
            "used_memory_max": max(s["used_memory"] for s in samples)}


def marginal_projection(stages, current, *, universe, ceiling):
    if (type(universe) is not int or universe < current["pairs"]
            or type(ceiling) is not int or ceiling <= 0):
        raise ValueError("invalid_cohort_projection")
    eligible = [s for s in stages if current["pairs"]-s["pairs"] >= 10]
    limit = ceiling*90//100
    if not eligible:
        return {"status": "unavailable", "reason": "no_settled_stage_ten_pairs_apart",
                "stop": False, "limit_bytes": limit}
    prior = max(eligible, key=lambda s: s["pairs"])
    slope = (current["used_memory_median"]-prior["used_memory_median"])/(current["pairs"]-prior["pairs"])
    projected = math.ceil(current["used_memory_median"]+max(0,slope)*(universe-current["pairs"]))
    return {"status": "estimated", "prior_pairs": prior["pairs"],
            "raw_bytes_per_pair": slope, "bytes_per_pair": max(0,slope),
            "negative_slope_clamped": slope < 0, "universe": universe,
            "projected_bytes": projected, "limit_bytes": limit,
            "stop": projected >= limit}
