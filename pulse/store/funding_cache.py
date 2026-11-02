"""ADR-0074 exact FundingReport codec and retained verification boundary."""
from dataclasses import asdict, fields
from decimal import Decimal
import json

from pulse.metrics.funding import FundingReport, FundingMetadata, Settlement, FundingValue, settled_view, GRACE, HOUR
from .runtime_document import document

VALUE_NAMES=("last","cumulative_1d","cumulative_7d","delta_1d","delta_7d")


def _wire(value):
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,dict): return {k:_wire(v) for k,v in value.items()}
    return value


def encode(report):
    if not isinstance(report,FundingReport) or report.verified_at_ms is None:
        raise ValueError("funding_report_unverified")
    checked=settled_view(report,now_ms=report.verified_at_ms)
    if checked!=report or report.latest is None:
        raise ValueError("funding_report_unverified")
    expiry=report.latest.time_ms+report.metadata.coindcx_hours*HOUR+GRACE
    for name in VALUE_NAMES:
        value=getattr(report,name)
        if (not isinstance(value,FundingValue) or value.status not in ("available","unavailable","settlement_pending")
                or value.classification_status!="uncalibrated"):
            raise ValueError("funding_report_value")
        if value.status=="available":
            if (not isinstance(value.fraction,Decimal) or not value.fraction.is_finite()
                    or value.reason is not None or value.asof_ms!=report.latest.time_ms
                    or value.expires_at_ms!=expiry):
                raise ValueError("funding_report_value")
        elif value.fraction is not None or value.level_basis_bps is not None or not isinstance(value.reason,str):
            raise ValueError("funding_report_value")
        if value.level_basis_bps is not None and (not isinstance(value.level_basis_bps,Decimal)
                                                 or not value.level_basis_bps.is_finite()):
            raise ValueError("funding_report_value")
    value={"schema":"pulse.funding.report.v1","report":_wire(asdict(report))}
    return document(value,source_ms=report.verified_at_ms,expires_ms=expiry)


def _construct(cls,value,decimal_fields):
    if type(value) is not dict or set(value)!={f.name for f in fields(cls)}:
        raise ValueError("funding_cache_shape")
    result=dict(value)
    for key in decimal_fields:
        raw=result[key]
        if raw is not None:
            if not isinstance(raw,str) or len(raw)>128: raise ValueError("funding_cache_decimal")
            number=Decimal(raw)
            if not number.is_finite(): raise ValueError("funding_cache_decimal")
            result[key]=number
    return cls(**result)


def decode(payload):
    try:
        value=json.loads(payload)
        if type(value) is not dict or set(value)!={"schema","report"} or value["schema"]!="pulse.funding.report.v1":
            raise ValueError("funding_cache_schema")
        raw=value["report"]
        if type(raw) is not dict or set(raw)!={f.name for f in fields(FundingReport)}:
            raise ValueError("funding_cache_shape")
        raw=dict(raw)
        raw["metadata"]=_construct(FundingMetadata,raw["metadata"],("settled_rate","cap","floor"))
        raw["latest"]=_construct(Settlement,raw["latest"],("rate",))
        for name in VALUE_NAMES:
            raw[name]=_construct(FundingValue,raw[name],("fraction","level_basis_bps"))
        report=FundingReport(**raw)
        if encode(report).payload!=payload:
            raise ValueError("funding_cache_noncanonical")
        return report
    except (ValueError,TypeError,KeyError,ArithmeticError,UnicodeError,AttributeError):
        raise ValueError("funding_cache_integrity_failed") from None


class FundingCache:
    def __init__(self,store,pair_id,mapping):
        self.store,self.pair_id,self.mapping=store,pair_id,mapping

    def refresh(self,report,*,expected_revision):
        if report.metadata.pair_id!=self.pair_id or report.metadata.mapping_version!=self.mapping:
            raise ValueError("funding_cache_identity")
        try:
            candidate=encode(report)
        except ValueError:
            self.store.failed("settlement_verification_failed")
            return [b"retained_last_good"]
        return self.store.publish(candidate,expected_revision=expected_revision)

    def read(self,*,now_ms):
        record=self.store.read(now_ms=now_ms)
        if record["status"]!="available": return record
        try:
            report=decode(record["payload"])
            if report.metadata.pair_id!=self.pair_id or report.metadata.mapping_version!=self.mapping:
                raise ValueError()
            return {**record,"report":settled_view(report,now_ms=now_ms)}
        except ValueError:
            return {"status":"unavailable","reason":"funding_cache_integrity_failed","revision":record["revision"]}
