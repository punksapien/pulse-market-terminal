"""ADR-0084 consumer coverage using unchanged v1 classifiers."""
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from snapshot_spec import snapshot_fixture
from canonical import seal_fixture


def classified_fixture():
    snapshot=snapshot_fixture()
    row=snapshot["rows"][0]
    # Frozen executable v1 outputs, independently rechecked by the app suite's
    # test_classified_volatility_fixture.py (which has the scorer dependencies).
    for horizon,level in (("1H",3),("4H",2),("24H",1)):
        f=row["volatility"]["windows"][horizon]
        f.update(value="0.01",classification_status="classified",classification_version="volatility.v1",
                 level=level)
    row["volatility"]["score"]=deepcopy(row["volatility"]["windows"]["1H"])
    row["volatility"]["direction"]["value"]="Stable"
    return seal_fixture(snapshot)


def all_classified_fixture():
    """Fictional contract coverage, NOT calibrated cutoffs or live activation."""
    from pulse.metrics.levels import magnitude,classify_basis
    snapshot=classified_fixture();row=snapshot["rows"][0]
    for h,value in (("1H","0.005"),("4H","0.5"),("24H","5")):
        row["oi"]["change_"+h.lower()]["value"]=value
    targets=[(row["ls"]["global_ratio"],None,None),
        (row["funding"]["native_estimate"],row["funding"]["h"],None),
        (row["funding_change"]["settled_last"],row["funding_change"]["h"],None)]
    targets += [(row["oi"]["change_"+h.lower()],None,h) for h in ("1H","4H","24H")]
    for field,hours,window in targets:
        basis,unit=magnitude(field["definition_version"],field["value"],hours=hours)
        field.update(classification_status="classified",level=classify_basis(basis,("0.1","1","10")),
            level_basis=str(basis),level_basis_unit=unit,
            classification_version=field["definition_version"]+".l2."+(window+"." if window else "")+"fictional_fixture")
    return seal_fixture(snapshot)
