"""ADR-0059 whole-capsule oracle for synthetic, unpublished parity vectors.

Uses the separately reviewed Python ring and coverage implementations, not a
translation of Lua bitmap shifts. Never opens a network connection.
"""
import struct
import zlib

from pulse.store.bucket_ring import BucketRing, BucketCollision, WriteStatus
from pulse.store.coverage import CoverageBitmap
from pulse.store.memory_store import MemoryStore
from pulse.store.one_pair_oi import MAPPING, DERIVED, RING_BYTES, empty_capsule


class CapsuleReference:
    def __init__(self):
        self.ring = BucketRing(60000, 1536)
        self.store = MemoryStore(100000)
        self.store.claim(1)
        self.coverage = CoverageBitmap(self.store, "coverage", 1536, 1)
        self.derived = bytes(DERIVED.size)
        self.current_time = None
        self.version = 0
        self.state = b"R"
        self.dirty = 0

    def write(self, point, *, now_ms):
        if point.source_ms > now_ms + 5000:
            return "future_timestamp"
        if now_ms - point.source_ms > 900000:
            return "stale_timestamp"
        try:
            status = self.ring.write(point.source_ms, (point.quantity,), now_ms=now_ms)
        except BucketCollision:
            status = WriteStatus.CONFLICT
        if status == WriteStatus.DUPLICATE:
            return "duplicate"
        self.coverage.observe(point.source_ms // 60000, status=status, token=1)
        if status == WriteStatus.WRITTEN and (self.current_time is None or point.source_ms > self.current_time):
            self.current_time = point.source_ms
            self.version += 1
            source = point.source_quantity.encode("ascii")
            self.derived = DERIVED.pack(point.source_ms, point.quantity,
                                        point.source_ms + 900001, self.version,
                                        len(source), source)
            self.state = b"R"
        elif status == WriteStatus.CONFLICT and point.source_ms == self.current_time:
            self.state = b"Q"
        self.dirty = 1
        return "exact_timestamp_conflict" if status == WriteStatus.CONFLICT else status.value

    def to_bytes(self):
        bits = self.store.read_control(lambda data: data["coverage"]["valid"])
        body = self.derived + bits.to_bytes(192, "little")
        footer = (b"PULSEC11" + self.state + bytes([self.dirty])
                  + struct.pack("<I", zlib.crc32(body)) + b"\0\0" + MAPPING + body)
        return self.ring.to_bytes() + footer


def boundary_cases(now_ms):
    """Long advances use seeded historical fixtures, not a fake Lua clock.

    All dispatched samples are within 15 minutes of real server time. A historical
    seed models retained state after a collection gap; it is NOT exchange backfill.
    """
    from pulse.collectors.one_pair_oi import CurrentOI
    anchor = (now_ms // 60000 - 1) * 60000 + 100
    def point(stamp, source="100"):
        return CurrentOI("BTCUSDT", stamp, source, float(source))
    cases = []
    for gap in (1, 7, 8, 9, 1535, 1536):
        seed = CapsuleReference()
        older = point(anchor - gap * 60000)
        seed.write(older, now_ms=older.source_ms)
        cases.append(("advance_" + str(gap), seed, (point(anchor, "101"),)))
    seed = CapsuleReference()
    older = point(anchor - 8 * 60000)
    seed.write(older, now_ms=older.source_ms)
    cases.append(("recent_sequence_conflict_recovery", seed, (
        point(anchor-7*60000, "101"), point(anchor-6*60000, "102"),
        point(anchor-5*60000, "103"), point(anchor-6*60000+100, "104"),
        point(anchor-5*60000-50, "99"),
        point(anchor-5*60000, "105"), point(anchor-5*60000+100, "106"),
        point(anchor, "107"), point(anchor, "107"),
    )))
    seed = CapsuleReference()
    # A new cycle reuses an existing physical slot after capacity buckets.
    old = point(anchor - 1536*60000)
    seed.write(old, now_ms=old.source_ms)
    cases.append(("physical_slot_wrap", seed, (point(anchor,"200"),point(anchor+100,"201"))))
    return cases
