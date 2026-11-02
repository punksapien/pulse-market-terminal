"""ADR-0111 semantic classification paths; no active numeric policy."""
from decimal import Decimal
from pulse.metrics.levels import magnitude

def validate_levels(row):
    allowed={id(row["volatility"]["score"]):(None,None)}
    for field in row["volatility"]["windows"].values():allowed[id(field)]=(None,None)
    allowed[id(row["ls"]["global_ratio"])]=(None,None)
    for window,field in row["ls"].get("period_ratios",{}).items():allowed[id(field)]=(None,window)
    allowed[id(row["funding"]["native_estimate"])]=(row["funding"]["h"],None)
    allowed[id(row["funding_change"]["settled_last"])]=(row["funding_change"]["h"],None)
    for window,key in (("1D","1d"),("7D","7d")):
        allowed[id(row["funding"]["cumulative_"+key])]=(row["funding"]["h"],window)
        allowed[id(row["funding_change"]["settled_"+key])]=(row["funding_change"]["h"],window)
    for window in ("1H","4H","24H"):allowed[id(row["oi"]["change_"+window.lower()])]=(None,window)
    def walk(node):
        if isinstance(node,dict):
            if node.get("classification_status")=="classified":
                if id(node) not in allowed:raise ValueError("l2_classified_path_refused")
                definition=node["definition_version"]
                if definition=="volatility.v1":
                    if node.get("classification_version") not in ("volatility.v1","volatility.v1.regime"):
                        raise ValueError("l2_policy_identity")
                else:
                    hours,window=allowed[id(node)]
                    prefix=definition+".l2."+(window+"." if window else "")
                    version=node.get("classification_version","")
                    if not version.startswith(prefix) or len(version)<=len(prefix):raise ValueError("l2_policy_identity")
                    basis,unit=magnitude(definition,node["value"],hours=hours,
                        window=window if window in ("1D","7D") else None)
                    if node["level_basis_unit"]!=unit or Decimal(node["level_basis"])!=basis:
                        raise ValueError("l2_basis_mismatch")
            for value in node.values():walk(value)
        elif isinstance(node,list):
            for value in node:walk(value)
    walk(row)
