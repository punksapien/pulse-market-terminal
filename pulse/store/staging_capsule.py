"""ADR-0059 persistent staging BTC capsule; no production or collector wiring."""
from .one_pair_oi import OnePairStore, CAPSULE_BYTES, empty_capsule
from .staging_policy import staging_key
from .expansion_guard import guard


class StagingCapsule(OnePairStore):
    def __init__(self, client, run_id, *, approved_plan_fallback=False):
        # Reuse reviewed write/read/fence semantics, not copied per-metric Lua.
        super().__init__(client, run_id)
        self.prefix = "pulse:stg:{" + run_id + "}:btc:oi:"
        self.frame, self.lease, self.fence, self.budget = (
            staging_key(self.prefix + suffix)
            for suffix in ("frame", "lease", "fence", "source-budget"))
        if type(approved_plan_fallback) is not bool:
            raise ValueError("invalid_capacity_exception")
        self.approved_plan_fallback = approved_plan_fallback

    def _target(self, frame):
        return super()._target(staging_key(self.frame if frame is None else frame))

    def preflight(self):
        info = self.client.info("memory")
        capacity, self.last_memory = guard(info, additional_bytes=CAPSULE_BYTES,
                                          approved_plan_fallback=self.approved_plan_fallback)
        return capacity

    def initialise(self):
        self.preflight()
        self._target(self.frame)
        staging_key(self.lease)
        script = """if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return {'fenced_out'} end
        if redis.call('EXISTS',KEYS[1])==1 then
            if redis.call('PTTL',KEYS[1])~=-1 then return {'invalid_ring_lifecycle'} end
            return {'already_initialised'}
        end
        if string.len(ARGV[2])~=tonumber(ARGV[3]) then return {'invalid_preallocation'} end
        redis.call('SET',KEYS[1],ARGV[2])
        return {'initialised'}"""
        result = self.client.eval(script, 2, self.frame, self.lease, self.owner,
                                  empty_capsule(), CAPSULE_BYTES)
        if result not in ([b"initialised"], [b"already_initialised"]):
            raise RuntimeError("staging_initialisation_refused")
        self.preflight()
        return result

    def write(self, point, *, owner=None, frame=None):
        self._target(frame)
        staging_key(self.lease)
        self.preflight()
        result = super().write(point, owner=owner, frame=frame)
        self.preflight()
        return result

    def acquire(self):
        self.preflight()
        staging_key(self.lease)
        staging_key(self.fence)
        return super().acquire()

    def reserve_source(self):
        # The inherited one-shot experimental budget is NOT shared admission.
        # No live source requests on this adapter until the shared budget lands.
        raise RuntimeError("shared_source_admission_required")
