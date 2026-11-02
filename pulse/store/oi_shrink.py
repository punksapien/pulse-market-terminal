"""ADR-0174: offline, lossless-within-retention OI1536→300 frame conversion."""
from dataclasses import replace
import json,base64,struct,sys,zlib
from .bucket_ring import BucketRing
from .capsule_model import CapsuleSpec,decode_derived,footer


def specs(pair,symbol):
    old=CapsuleSpec(pair,symbol,"oi","quantity",60000,1536,900000,900000,True)
    return old,replace(old,capacity=300)


def shrink(raw,pair,symbol):
    old,new=specs(pair,symbol)
    current=new if len(raw)==new.nbytes else old
    if len(raw)!=current.nbytes:raise ValueError("shrink_length_refused")
    ring=BucketRing.from_bytes(raw[:current.ring_bytes])
    if (ring.step_ms,ring.capacity,ring.columns)!=(60000,current.capacity,1):
        raise ValueError("shrink_dimensions_refused")
    tail=raw[current.ring_bytes:]
    if (tail[:8]!=b"PULSEC11" or tail[8:9] not in (b"R",b"Q") or tail[9] not in (0,1)
            or tail[14:16]!=b"\0\0" or tail[16:32]!=current.mapping
            or struct.unpack_from("<I",tail,10)[0]!=zlib.crc32(tail[32:])):
        raise ValueError("shrink_integrity_refused")
    derived=tail[32:32+current.derived_bytes]
    coverage=int.from_bytes(tail[32+current.derived_bytes:],"little")
    if coverage>>current.capacity:raise ValueError("shrink_coverage_refused")
    if any(derived):decode_derived(current,derived)
    if current==new:return raw
    latest,collisions=ring._check_header();out=BucketRing(60000,300)
    for slot in range(old.capacity):
        if not ring._bit(slot):continue
        point,flags=ring._read_slot(slot);bucket=point[0]//60000
        if not latest-new.capacity<bucket<=latest:continue
        index=bucket%new.capacity
        out.record.pack_into(out._data,out.offset+index*out.stride,point[0]|flags,*point[1:])
        out._data[64+index//8]|=1<<(index%8)
    out._header(latest,collisions)
    return out.to_bytes()+footer(new,derived,(coverage&((1<<300)-1)).to_bytes(new.coverage_bytes,"little"),
        state=tail[8:9],dirty=tail[9])


if __name__=="__main__":
    request=json.loads(sys.stdin.buffer.read(40000))
    raw=base64.b64decode(request["frame"],validate=True)
    result=shrink(raw,request["pair"],request["symbol"])
    print(json.dumps({"frame":base64.b64encode(result).decode(),"before":len(raw),"after":len(result)}))
