import json
from pathlib import Path
import unittest

import test_composer as oracle
from pulse.wire import validation


class PackagedValidationTests(unittest.TestCase):
    def test_schemas_equal_canonical_documents(self):
        root=Path(__file__).resolve().parents[2]
        for name in ("common","snapshot","live_price","live_funding"):
            self.assertEqual(json.loads((root/f"docs/contracts/schema/{name}.schema.json").read_text()),
                             json.loads((root/f"pulse/wire/schemas/{name}.schema").read_text()))

    def test_existing_oracle_documents(self):
        case=oracle.ComposerTests();case.setUp()
        validation.snapshot(case.s)
        value=case.price({"mkt":"BTCUSDT","ls":80460,"btST":case.now})
        validation.live(value,case.s["universe"],case.s["mapping_version"])
        value["rows"][0]["source_ms"]=case.now+10000
        with self.assertRaises(ValueError): validation.live(value,case.s["universe"],case.s["mapping_version"])


if __name__=="__main__": unittest.main()
