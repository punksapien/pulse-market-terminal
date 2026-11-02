"""ADR-0062 shared exact-source observation capsule; no metric classification."""
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import struct
import zlib

from pulse.observation import MAX_SOURCE_TIME_MS
from .bucket_ring import BucketRing

METRICS={"reference":1,"oi":1,"oi_cold":1,"ls_global":3,"ls_top":3,
         "funding_estimate":1,"funding_settled":1,"volatility":3}


@dataclass(frozen=True)
class CapsuleSpec:
    pair_id: str
    symbol: str
    metric: str
    unit: str
    step_ms: int
    capacity: int
    ttl_ms: int
    max_ingress_age_ms: int
    inclusive_expiry: bool=False
    mapping_version: str="v1"

    def __post_init__(self):
        if (not isinstance(self.pair_id,str) or not re.fullmatch(r"B-[A-Z0-9]{1,24}_USDT",self.pair_id)
                or not isinstance(self.symbol,str) or not re.fullmatch(r"[A-Z0-9]{1,24}USDT",self.symbol)
                or not isinstance(self.metric,str) or self.metric not in METRICS
                or not isinstance(self.unit,str) or self.unit not in ("quantity","price","ratio_shares","fraction","percent")
                or not isinstance(self.mapping_version,str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}",self.mapping_version)):
            raise ValueError("invalid_capsule_identity")
        for value,limit in ((self.step_ms,2**32-1),(self.capacity,1536),(self.ttl_ms,8*86400000),
                            (self.max_ingress_age_ms,8*86400000)):
            if type(value) is not int or not 0<value<=limit:
                raise ValueError("invalid_capsule_dimensions")
        expected={"reference":"price","oi":"quantity","oi_cold":"quantity",
                  "ls_global":"ratio_shares","ls_top":"ratio_shares",
                  "funding_estimate":"fraction","funding_settled":"fraction","volatility":"percent"}
        if type(self.inclusive_expiry) is not bool or self.unit!=expected[self.metric]:
            raise ValueError("invalid_capsule_units")

    @property
    def columns(self):
        return METRICS[self.metric]

    @property
    def mapping(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True,separators=(",",":")).encode()).digest()[:16]

    @property
    def ring_bytes(self):
        return BucketRing.nbytes_for(self.step_ms,self.capacity,self.columns)

    @property
    def coverage_bytes(self):
        return (self.capacity+7)//8

    @property
    def derived_bytes(self):
        return 24+72*self.columns

    @property
    def footer_bytes(self):
        return 32+self.derived_bytes+self.coverage_bytes

    @property
    def nbytes(self):
        return self.ring_bytes+self.footer_bytes

    @property
    def allow_negative(self):
        return self.metric.startswith("funding_")

    @property
    def wire_config(self):
        return (self.step_ms,self.capacity,self.columns,self.ttl_ms,self.max_ingress_age_ms,
                int(self.inclusive_expiry),int(self.allow_negative))


@dataclass(frozen=True)
class CapsuleObservation:
    source_ms: int
    values: tuple
    sources: tuple
    expires_ms: int


def observation(spec,source_ms,sources,*,expires_ms=None):
    if type(source_ms) is not int or not 0<=source_ms<=MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_source_timestamp")
    if type(sources) is not tuple or len(sources)!=spec.columns:
        raise ValueError("invalid_source_columns")
    values=[]
    for source in sources:
        if (not isinstance(source,str) or not re.fullmatch(r"[0-9eE.+-]{1,63}",source)):
            raise ValueError("invalid_decimal_source")
        try:
            decimal=Decimal(source)
            number=float(decimal) if decimal else 0.
        except (InvalidOperation,ValueError,OverflowError):
            raise ValueError("invalid_decimal_source") from None
        if (not decimal.is_finite() or not math.isfinite(number)
                or Decimal(repr(number))!=decimal or (number and abs(number)<2**-1022)
                or (not spec.allow_negative and number<0)):
            raise ValueError("source_precision_or_sign_unavailable")
        values.append(number)
    maximum=source_ms+spec.ttl_ms+int(spec.inclusive_expiry)
    expiry=maximum if expires_ms is None else expires_ms
    if type(expiry) is not int or not source_ms<expiry<=min(maximum,MAX_SOURCE_TIME_MS):
        raise ValueError("invalid_source_expiry")
    return CapsuleObservation(source_ms,tuple(values),sources,expiry)


def source_bytes(point):
    return b"".join(bytes([len(source)])+source.encode("ascii").ljust(63,b"\0") for source in point.sources)


def derived_bytes(spec,point,version):
    if type(version) is not int or not 1<=version<=MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_derived_version")
    checked=observation(spec,point.source_ms,point.sources,expires_ms=point.expires_ms)
    if checked!=point:
        raise ValueError("source_value_mismatch")
    return struct.pack("<Q"+"d"*spec.columns+"QQ",point.source_ms,*point.values,point.expires_ms,version)+source_bytes(point)


def footer(spec,derived,coverage,state=b"R",dirty=0):
    if (len(derived)!=spec.derived_bytes or len(coverage)!=spec.coverage_bytes
            or state not in (b"R",b"Q",b"W") or type(dirty) is not int or dirty not in (0,1)
            or int.from_bytes(coverage,"little")>>spec.capacity):
        raise ValueError("invalid_footer_size")
    body=derived+coverage
    return b"PULSEC11"+state+bytes([dirty])+struct.pack("<I",zlib.crc32(body))+b"\0\0"+spec.mapping+body


def empty_capsule(spec):
    return (BucketRing(spec.step_ms,spec.capacity,spec.columns).to_bytes()
            +footer(spec,bytes(spec.derived_bytes),bytes(spec.coverage_bytes)))


def decode_derived(spec,raw):
    if len(raw)!=spec.derived_bytes:
        raise ValueError("invalid_derived_size")
    numbers=struct.unpack_from("<Q"+"d"*spec.columns+"QQ",raw)
    start=24+8*spec.columns
    sources=[]
    for index in range(spec.columns):
        offset=start+64*index
        length=raw[offset]
        if not 1<=length<=63 or any(raw[offset+1+length:offset+64]):
            raise ValueError("invalid_stored_source")
        sources.append(raw[offset+1:offset+1+length].decode("ascii"))
    point=observation(spec,numbers[0],tuple(sources),expires_ms=numbers[-2])
    if point.values!=tuple(numbers[1:1+spec.columns]) or not 1<=numbers[-1]<=MAX_SOURCE_TIME_MS:
        raise ValueError("derived_value_mismatch")
    return point,numbers[-1]
