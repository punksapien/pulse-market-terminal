import copy
import sys
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse.wire.compact import encode, decode, decode_rows, raw, expand, vector
from build import outputs
from wire_spec import varied_snapshot, wire_schema
from semantics import validate_snapshot
from snapshot_spec import make_unavailable
from canonical import seal_fixture
from jsonschema import Draft202012Validator


class WireTests(unittest.TestCase):
    def test_all_logical_fixtures_roundtrip(self):
        validator=Draft202012Validator(wire_schema())
        for name,s in outputs().items():
            if not name.startswith("docs/contracts/fixtures/snapshot."): continue
            documents=encode(s)
            for d in documents.values(): validator.validate(d)
            self.assertEqual(raw(decode(documents)),raw(s),name)

    def test_varied_504_roundtrip(self):
        s=varied_snapshot()
        validate_snapshot(s)
        self.assertEqual(raw(s),raw(decode(encode(s))))

    def test_mixed_unavailable_nested_objects_roundtrip(self):
        s=varied_snapshot(3)
        unavailable=make_unavailable(s)
        s["rows"][0]=unavailable["rows"][0]
        s=seal_fixture(s)
        validate_snapshot(s)
        self.assertEqual(raw(s),raw(decode(encode(s))))

    def test_optional_null_zero_false_and_empty_distinct(self):
        from pulse.wire.compact import encode_rows
        rows=[{"x":None,"empty":{},"array":[],"v":False},{"x":0,"v":"0"},{"v":0,"empty":{"child":1}}]
        encoded={"count":3,**encode_rows(rows)}
        self.assertEqual(decode_rows(encoded),rows)

    def test_bad_generation_count_tables_path_presence_and_vectors(self):
        s=next(v for k,v in outputs().items() if k.endswith("snapshot.valid.1H.json"))
        original=encode(s)
        for kind in ("generation","count","path","duplicate","presence","table","vector","missing"):
            bad=copy.deepcopy(original); doc=bad["oi"]
            if kind=="generation": doc["generation"]="wrong"
            if kind=="count": doc["count"]=2
            if kind=="path": doc["columns"][0][0]=["__proto__","bad"]
            if kind=="duplicate": doc["columns"].append(copy.deepcopy(doc["columns"][0]))
            if kind=="presence": doc["columns"][0][1]=[0,0]
            if kind=="table":
                c=next(c for c in doc["columns"] if c[2]=="f"); c[4]=["c",999999]
            if kind=="vector": doc["columns"][0][3]=["d",[]]
            if kind=="missing": del bad["funding"]
            with self.subTest(kind=kind),self.assertRaises(ValueError): decode(bad)

    def test_vector_choices_and_sparse_bounds(self):
        for values in ([None]*10,[0]*20+[1],["x","y",0,None,False]):
            self.assertEqual(expand(vector(values),len(values)),values)
        for bad in (["s",0,[[1,2],[1,3]]],["s",0,[[-1,2]]],["s",0,[[True,2]]],["unknown",0]):
            with self.assertRaises(ValueError): expand(bad,3)


if __name__=="__main__": unittest.main()
