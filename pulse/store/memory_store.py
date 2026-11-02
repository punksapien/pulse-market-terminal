"""ADR-0044: bounded offline adapter; no Redis/lease equivalence implied."""

from threading import RLock

from .bucket_ring import BucketRing, IntegrityError


class MemoryStore:
    def __init__(self, max_bytes):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("invalid_capacity")
        self.max_bytes = max_bytes
        self._rings = {}
        self._used = 0
        self._fence = 0
        self._lock = RLock()
        self._control = {}

    def atomic_control(self, token, operation):
        """ADR-0054: offline atomic/fenced state transition, not Redis parity."""
        with self._lock:
            self._check(token)
            return operation(self._control)

    def read_control(self, operation):
        with self._lock:
            return operation(self._control)

    def inspect_ring_headers(self,keys,token):
        if type(keys) is not tuple or len(keys)>100:
            raise ValueError("invalid_scrub_batch")
        with self._lock:
            self._check(token)
            result=[]
            for key in keys:
                ring=self._rings.get(key)
                if ring is None:
                    result.append((key,"missing"))
                    continue
                try:
                    ring._check_header()
                    result.append((key,"valid"))
                except IntegrityError:
                    result.append((key,"integrity_failed"))
            return tuple(result)

    def claim(self, token):
        # shortcut: offline fence only; replace with atomic lease+fence validation in C1.
        with self._lock:
            if type(token) is not int or token <= self._fence:
                raise ValueError("nonmonotonic_fence")
            self._fence = token

    def _check(self, token):
        if type(token) is not int or token != self._fence or token <= 0:
            raise PermissionError("fenced_out")

    def create_ring(self, key, step_ms, capacity, columns, token):
        with self._lock:
            self._check(token)
            if key in self._rings:
                raise ValueError("already_exists")
            required = BucketRing.nbytes_for(step_ms, capacity, columns)
            if self._used + required > self.max_bytes:
                raise MemoryError("admission_denied")
            ring = BucketRing(step_ms, capacity, columns)
            self._rings[key] = ring
            self._used += ring.nbytes

    def write_ring(self, key, timestamp_ms, values, token, *, now_ms):
        with self._lock:
            self._check(token)
            return self._rings[key].write(timestamp_ms, values, now_ms=now_ms)

    def baseline(self, key, target_ms, tolerance_ms):
        with self._lock:
            return self._rings[key].baseline(target_ms, tolerance_ms)

    @property
    def used_bytes(self):
        with self._lock:
            return self._used
