"""ADR-0049: proposed timing admission budget, not a runtime scheduler guarantee."""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ReferenceCoverageBudget:
    producer_ms: int = 60000
    scheduler_jitter_ms: int = 5000
    lease_handoff_ms: int = 5000
    build_ms: int = 1000
    cdn_fresh_ms: int = 10000
    stale_while_revalidate_ms: int = 0
    client_poll_ms: int = 60000
    missed_poll_ms: int = 60000
    clock_margin_ms: int = 5000

    def maximum_delay_ms(self):
        components = [getattr(self, field.name) for field in fields(self)]
        if any(type(value) is not int or value < 0 for value in components):
            raise ValueError("invalid_budget")
        return sum(components)

    def assess(self, slots=5, reserve_ms=30000):
        if type(slots) is not int or not 1 <= slots <= 8 or type(reserve_ms) is not int or reserve_ms < 0:
            raise ValueError("invalid_coverage")
        remaining = slots * 60000 - 59999
        delay = self.maximum_delay_ms()
        return {"minimum_remaining_ms": remaining, "maximum_delay_ms": delay,
                "margin_ms": remaining - delay, "admitted": remaining - delay >= reserve_ms}
