"""ADR-0056 fixed capsule adapter; caller must verify isolated Preview identity."""
import hashlib
import struct
import uuid
import zlib
from pathlib import Path

from pulse.store.bucket_ring import BucketRing

MAPPING=hashlib.sha256(b"coindcx:B-BTC_USDT|binance:BTCUSDT|oi_quantity_v1").digest()[:16]
RING_BYTES=BucketRing.nbytes_for(60000,1536,1)
META=32
DERIVED=struct.Struct("<QdQQB63s")
COVERAGE=192
CAPSULE_BYTES=RING_BYTES+META+DERIVED.size+COVERAGE


def empty_capsule():
    body=bytes(DERIVED.size+COVERAGE)
    return BucketRing(60000,1536).to_bytes()+b"PULSEC11R\x00"+struct.pack("<I",zlib.crc32(body))+b"\0\0"+MAPPING+body


class OnePairStore:
    def __init__(self,client,run_id=None):
        run_id=run_id or uuid.uuid4().hex
        if len(run_id)!=32 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("invalid_c1_run_id")
        self.client,self.prefix=client,"pulse:c1:{"+run_id+"}:btc:oi:"
        self.frame,self.lease,self.fence,self.budget=(self.prefix+suffix for suffix in ("frame","lease","fence","source-budget"))
        folder=Path(__file__).with_name("lua")
        self.write_sha=client.script_load((folder/"c1_oi_write.lua").read_bytes())
        self.read_sha=client.script_load((folder/"c1_oi_read.lua").read_bytes())
        self.owner=None

    def acquire(self):
        nonce=uuid.uuid4().hex
        script="""if redis.call('EXISTS',KEYS[1])==1 then return false end
        local epoch=redis.call('INCR',KEYS[2])
        local owner=tostring(epoch)..':'..ARGV[1]
        redis.call('SET',KEYS[1],owner,'PX',30000)
        redis.call('EXPIRE',KEYS[2],900)
        return owner"""
        self.owner=self.client.eval(script,2,self.lease,self.fence,nonce)
        if not self.owner:
            raise RuntimeError("c1_lease_busy")
        return self.owner

    def initialise(self):
        # Provider rejects INFO inside Lua. This observed preflight is NOT a
        # distributed memory reservation; only the bounded one-pair run is enabled.
        used=self.client.info("memory").get("used_memory")
        if type(used) is not int or used+100000>=21000000:
            raise RuntimeError("c1_memory_gate_denied")
        script="""if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return {'fenced_out'} end
        if redis.call('EXISTS',KEYS[1])==1 then return {'already_initialised'} end
        if string.len(ARGV[2])~=25152 then return {'invalid_preallocation'} end
        redis.call('SET',KEYS[1],ARGV[2],'EX',900)
        return {'initialised'}"""
        result=self.client.eval(script,2,self.frame,self.lease,self.owner,empty_capsule())
        if result not in ([b"initialised"],[b"already_initialised"]):
            raise RuntimeError("c1_initialisation_refused")
        return result

    def reserve_source(self):
        # One experimental call only, durable across collector retry in this run.
        script="""if redis.call('GET',KEYS[2])~=ARGV[1] or redis.call('PTTL',KEYS[2])<=0 then return 0 end
        return redis.call('SET',KEYS[1],'reserved','NX','EX',900) and 1 or 0"""
        return self.client.eval(script,2,self.budget,self.lease,self.owner)==1

    def write(self,point,*,owner=None,frame=None):
        if point.symbol!="BTCUSDT":
            raise ValueError("oi_identity_mismatch")
        target=self._target(frame)
        return self.client.evalsha(self.write_sha,2,target,self.lease,
                                   self.owner if owner is None else owner,point.source_ms,struct.pack("<d",point.quantity),point.source_quantity,MAPPING)

    def _target(self,frame):
        target=self.frame if frame is None else frame
        if not isinstance(target,str) or not target.startswith(self.prefix) or len(target)>128:
            raise ValueError("c1_frame_scope_refused")
        return target

    def read(self,*,frame=None):
        # Zero source calls; trusted server clock plus fixed-size footer read only.
        result=self.client.evalsha(self.read_sha,1,self._target(frame),MAPPING)
        if result[0]!=b"available":
            return {"status":"unavailable","reason":result[0].decode("ascii")}
        timestamp,quantity,expiry,version,length,source=DERIVED.unpack(result[1])
        return {"status":"available","symbol":"BTCUSDT","source_ms":timestamp,"quantity":quantity,
                "source_quantity":source[:length].decode("ascii"),"expires_ms":expiry,"version":version,
                "changes":{"1H":None,"4H":None,"24H":None},"change_reason":"baseline_missing"}
