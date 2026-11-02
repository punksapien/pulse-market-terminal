"""ADR-0045: exact-source bucket ring with explicit lossy-selection guard."""

import math
import struct
import zlib
from enum import Enum

from pulse.observation import MAX_FUTURE_SKEW_MS, MAX_SOURCE_TIME_MS
MAX_TIMESTAMP = MAX_SOURCE_TIME_MS


class WriteStatus(str, Enum):
    WRITTEN = "written"
    DUPLICATE = "duplicate"
    DROPPED_OLDER = "dropped_older"
    CONFLICT = "conflict"


class IntegrityError(ValueError):
    pass


class BucketCollision(IntegrityError):
    status = WriteStatus.CONFLICT


class DiscardedBaseline(LookupError):
    pass


class BucketRing:
    HEADER = struct.Struct("<8sHHIIq32sI")
    MAGIC = b"PULSER02"
    COLLIDED = 1 << 62
    CONFLICT = 1 << 63
    TIME_MASK = MAX_TIMESTAMP

    @classmethod
    def nbytes_for(cls, step_ms, capacity, columns=1):
        if any(type(x) is not int or x <= 0 for x in (step_ms, capacity, columns)):
            raise ValueError("positive_integer_dimensions_required")
        if step_ms > 2**32 - 1 or capacity > 2**20 or columns > 64:
            raise ValueError("dimensions_too_large")
        return cls.HEADER.size + (capacity + 7) // 8 + capacity * 8 * (columns + 1)

    def __init__(self, step_ms, capacity, columns=1):
        size = self.nbytes_for(step_ms, capacity, columns)
        self.step_ms, self.capacity, self.columns = step_ms, capacity, columns
        self.mask_bytes = (capacity + 7) // 8
        self.record = struct.Struct("<Q" + "d" * columns)
        self.stride = self.record.size
        self.offset = self.HEADER.size + self.mask_bytes
        self._data = bytearray(size)
        self._header(-1, 0)

    @property
    def nbytes(self):
        return len(self._data)

    @property
    def collision_buckets(self):
        return self._check_header()[1]

    def _header(self, latest, collisions):
        reserved = struct.pack("<Q", collisions) + b"\0" * 24
        self.HEADER.pack_into(self._data, 0, self.MAGIC, 2, self.columns,
                              self.step_ms, self.capacity, latest, reserved, 0)
        struct.pack_into("<I", self._data, 60, zlib.crc32(memoryview(self._data)[:60]))

    def _check_header(self):
        if len(self._data) != self.nbytes_for(self.step_ms, self.capacity, self.columns):
            raise IntegrityError("invalid_length")
        magic, version, columns, step, capacity, latest, reserved, expected = self.HEADER.unpack_from(self._data)
        if (magic, version, columns, step, capacity, reserved[8:]) != (
            self.MAGIC, 2, self.columns, self.step_ms, self.capacity, b"\0" * 24
        ) or not -1 <= latest <= MAX_TIMESTAMP // self.step_ms:
            raise IntegrityError("invalid_header")
        if zlib.crc32(memoryview(self._data)[:60]) != expected:
            raise IntegrityError("header_crc_failed")
        return latest, struct.unpack_from("<Q", reserved)[0]

    def _bit(self, slot):
        return bool(self._data[64 + slot // 8] & (1 << (slot % 8)))

    def _read_slot(self, slot):
        offset = self.offset + slot * self.stride
        packed_time, *values = self.record.unpack_from(self._data, offset)
        if packed_time & ~(self.TIME_MASK | self.COLLIDED | self.CONFLICT):
            raise IntegrityError("unknown_timestamp_flags")
        timestamp = packed_time & self.TIME_MASK
        if (timestamp // self.step_ms) % self.capacity != slot or not all(math.isfinite(x) for x in values):
            raise IntegrityError("invalid_slot")
        return (timestamp, *values), packed_time & ~self.TIME_MASK

    def write(self, timestamp_ms, values, *, now_ms):
        if type(now_ms) is not int or not 0 <= now_ms <= MAX_TIMESTAMP:
            raise ValueError("invalid_observation_clock")
        if type(timestamp_ms) is not int or not 0 <= timestamp_ms <= MAX_TIMESTAMP:
            raise ValueError("invalid_timestamp")
        if timestamp_ms > now_ms + MAX_FUTURE_SKEW_MS:
            raise ValueError("future_timestamp")
        values = tuple(values)
        if len(values) != self.columns or any(type(x) is not float or not math.isfinite(x) for x in values):
            raise ValueError("finite_binary64_values_required")
        latest, collisions = self._check_header()
        bucket = timestamp_ms // self.step_ms
        if bucket <= latest - self.capacity:
            raise ValueError("outside_retention")
        slot = bucket % self.capacity
        raw = self.record.pack(timestamp_ms, *values)
        offset = self.offset + slot * self.stride
        flags = 0
        if self._bit(slot):
            previous, flags = self._read_slot(slot)
            old_bucket = previous[0] // self.step_ms
            if old_bucket > bucket:
                raise ValueError("newer_slot_present")
            if old_bucket == bucket:
                previous_values = bytes(self._data[offset + 8:offset + self.stride])
                if previous[0] == timestamp_ms:
                    if flags & self.CONFLICT:
                        canonical = min(previous_values, raw[8:])
                        self._data[offset:offset + self.stride] = struct.pack("<Q", timestamp_ms | flags) + canonical
                        raise BucketCollision("exact_timestamp_conflict")
                    if previous_values == raw[8:]:
                        return WriteStatus.DUPLICATE
                    self._data[offset:offset + self.stride] = struct.pack("<Q", timestamp_ms | flags | self.CONFLICT) + min(previous_values, raw[8:])
                    raise BucketCollision("exact_timestamp_conflict")
                if not flags & self.COLLIDED:
                    collisions = min(collisions + 1, 2**64 - 1)
                flags |= self.COLLIDED
                if timestamp_ms < previous[0]:
                    struct.pack_into("<Q", self._data, offset, previous[0] | flags)
                    self._header(latest, collisions)
                    return WriteStatus.DROPPED_OLDER
                flags &= ~self.CONFLICT
            else:
                flags = 0
        self._data[offset:offset + self.stride] = struct.pack("<Q", timestamp_ms | flags) + raw[8:]
        self._data[64 + slot // 8] |= 1 << (slot % 8)
        self._header(max(latest, bucket), collisions)
        return WriteStatus.WRITTEN

    def baseline(self, target_ms, tolerance_ms):
        if type(target_ms) is not int or not 0 <= target_ms <= MAX_TIMESTAMP or type(tolerance_ms) is not int or not 0 <= tolerance_ms <= self.step_ms:
            raise ValueError("invalid_lookup")
        latest, _ = self._check_header()
        bucket = target_ms // self.step_ms
        for candidate in (bucket, bucket - 1):
            if candidate < 0 or not latest - self.capacity < candidate <= latest:
                continue
            slot = candidate % self.capacity
            if not self._bit(slot):
                continue
            record, flags = self._read_slot(slot)
            if record[0] // self.step_ms != candidate:
                continue
            if flags & self.CONFLICT:
                raise BucketCollision("exact_timestamp_conflict")
            if record[0] > target_ms:
                if flags & self.COLLIDED:
                    raise DiscardedBaseline("discarded_baseline_candidate")
                continue
            if target_ms - record[0] <= tolerance_ms:
                return record
        return None

    def scrub(self):
        latest, _ = self._check_header()
        highest = -1
        for slot in range(self.capacity):
            if self._bit(slot):
                record, _ = self._read_slot(slot)
                highest = max(highest, record[0] // self.step_ms)
            elif any(memoryview(self._data)[self.offset + slot * self.stride:self.offset + (slot + 1) * self.stride]):
                raise IntegrityError("unmarked_record")
        if highest != latest:
            raise IntegrityError("latest_bucket_mismatch")
        if self.capacity % 8 and self._data[self.offset - 1] >> (self.capacity % 8):
            raise IntegrityError("unused_mask_bits")
        return True

    def coverage_buckets(self, first, last):
        """ADR-0054: bounded bitmap/popcount coverage; reject stale wrapped tags."""
        if type(first) is not int or type(last) is not int or first<0 or last<first or last-first+1>self.capacity:
            raise ValueError("invalid_coverage_window")
        latest,_=self._check_header()
        selected=0
        for bucket in range(first,last+1):
            if not latest-self.capacity<bucket<=latest:
                continue
            slot=bucket%self.capacity
            if not self._bit(slot):
                continue
            record,flags=self._read_slot(slot)
            if record[0]//self.step_ms==bucket and not flags & self.CONFLICT:
                selected |= 1<<slot
        return selected.bit_count(),last-first+1

    def to_bytes(self):
        self.scrub()
        return bytes(self._data)

    @classmethod
    def from_bytes(cls, raw):
        if len(raw) < cls.HEADER.size:
            raise IntegrityError("short_header")
        magic, version, columns, step, capacity, _, _, _ = cls.HEADER.unpack_from(raw)
        if magic != cls.MAGIC or version != 2:
            raise IntegrityError("unknown_format")
        if len(raw) != cls.nbytes_for(step, capacity, columns):
            raise IntegrityError("invalid_length")
        ring = cls(step, capacity, columns)
        ring._data[:] = raw
        ring.scrub()
        return ring
