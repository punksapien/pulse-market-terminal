"""Canonical Python oracle for consumer boundary witnesses (ADR-0083)."""
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse.metrics.funding import FundingMetadata,FundingEstimate,Settlement,settled_report,current_delta
from spec import NOW


def pairing_vectors():
    cases=[]
    public_mapping="sha256:"+"c"*64
    for name in ("valid","zero","estimate_before_settlement","actual_due_before_jittered_nft"):
        due_case=name=="actual_due_before_jittered_nft"
        latest=NOW-8*3600000 if due_case else NOW-1000
        due=latest+8*3600000; nft=due+(3000 if due_case else 0)
        verified=NOW-10000 if due_case else NOW
        now=NOW+1000 if due_case else NOW
        source=NOW+500 if due_case else NOW-2000 if name=="estimate_before_settlement" else NOW-500
        rate=Decimal("0.00015"); estimate_rate=rate if name=="zero" else Decimal("0.0002")
        meta=FundingMetadata("B-BTC_USDT",public_mapping,8,8,nft,rate,verified)
        report=settled_report([Settlement(meta.pair_id,latest-8*3600000,Decimal("0.0001")),
                               Settlement(meta.pair_id,latest,rate)],meta,now_ms=verified)
        if report.last.status!="available": raise AssertionError(report.last.reason)
        estimate=FundingEstimate(meta.pair_id,meta.mapping_version,8,nft,rate,estimate_rate,source)
        result=current_delta(estimate,report,now_ms=now)
        live={"id":0,"status":"available","reason":None,"h":8,"nft":nft,"fr":str(rate),"efr":str(estimate_rate),
              "source_ms":source,"expires_ms":min(source+120000,nft)}
        settled={"id":0,"mapping_version":public_mapping,"status":"available","reason":None,
            "h":8,"nft":nft,"settled_rate":str(rate),"settlement_ms":latest,"verified_at_ms":verified,"expires_ms":due+900000}
        cases.append({"name":name,"envelope":{"mapping_version":public_mapping},"live":live,"settled":settled,
            "now_ms":now,"expected":{"available":result.status=="available","reason":result.reason,
                "value":None if result.fraction is None else format(result.fraction,"f"),
                "bps":None if result.bps is None else format(result.bps,"f")}})
    return {"fixture_kind":"fictional_protocol_only","oracle":"pulse.metrics.funding.current_delta",
            "identity_bound_bridge_pending":False,"cases":cases}
