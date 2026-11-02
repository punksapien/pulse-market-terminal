import json
import unittest
import shutil
import subprocess
import random
from decimal import Decimal
from pathlib import Path

from pulse.metrics.price_precision import artifact_source, parity_case, prepare_batch, prepare_price, ring_artifact_source
from pulse.store.bucket_ring import BucketRing

VECTORS = Path(__file__).parent / "fixtures/price_change/shared_parity.json"


class PriceParityTests(unittest.TestCase):
    def test_guarded_ring_numeric_source_property(self):
        rng = random.Random(20261026)
        accepted = 0
        ring = BucketRing(60000, 8)
        sources = ["0.1000", "1.00E+5"] + [str(Decimal(rng.randrange(1, 10**17)).scaleb(rng.randrange(-30, 20))) for _ in range(10000)]
        for i, source in enumerate(sources):
            prepared = prepare_price(source)
            if not prepared.available:
                continue
            stamp = i * 60000
            ring.write(stamp, (prepared.ring_value,), now_ms=stamp)
            restored = BucketRing.from_bytes(ring.to_bytes()).baseline(stamp, 0)
            self.assertEqual(Decimal(ring_artifact_source(restored[1])), Decimal(source))
            accepted += 1
        self.assertGreater(accepted, 2000)
        self.assertEqual(ring_artifact_source(prepare_price("0.1000").ring_value), "0.1")

    @unittest.skipUnless(shutil.which("node"), "Node required for cross-runtime parity gate")
    def test_actual_node_results_against_decimal_reference(self):
        root = Path(__file__).parents[1]
        code = "import fs from 'node:fs'; import {parityCase} from './frontend/lib/price_math.mjs'; const v=JSON.parse(fs.readFileSync('tests/fixtures/price_change/shared_parity.json')); console.log(JSON.stringify(v.cases.map(c=>parityCase(c.current,c.reference))));"
        process = subprocess.run(["node", "--input-type=module", "-e", code], cwd=root, capture_output=True, text=True, timeout=15, check=True)
        vectors = json.loads(VECTORS.read_text())
        for case, actual in zip(vectors["cases"], json.loads(process.stdout), strict=True):
            expected = parity_case(case["current"], case["reference"])
            self.assertEqual((actual["status"], actual["reason"]), (expected["status"], expected["reason"]))
            if actual["status"] == "available":
                reference = expected["reference_fraction"]
                bound = Decimal(vectors["bound_factor"]) * max(Decimal(1), abs(reference))
                self.assertLessEqual(abs(Decimal.from_float(float(actual["fraction"])) - reference), bound)
                self.assertEqual(actual["display"], expected["display"])

    def test_shared_vectors_numeric_and_exact_display(self):
        vectors = json.loads(VECTORS.read_text())
        for case in vectors["cases"]:
            with self.subTest(case=case["id"]):
                result = parity_case(case["current"], case["reference"])
                if "reason" in case:
                    self.assertEqual((result["status"], result["reason"]), ("unavailable", case["reason"]))
                else:
                    expected = Decimal(case["expected_fraction"])
                    self.assertEqual(result["reference_fraction"], expected)
                    bound = Decimal(vectors["bound_factor"]) * max(Decimal(1), abs(expected))
                    self.assertLessEqual(abs(Decimal.from_float(result["fraction"]) - expected), bound)
                    self.assertEqual(result["display"], case["display"])
                    self.assertEqual(result["current_source"], case["current"])

    def test_ingress_refusal_has_no_numeric_placeholder_and_is_counted(self):
        prepared, counts = prepare_batch(["0.1", "1e-8", "1.0000000000000001", "0", "NaN"])
        self.assertEqual(counts, {"binary64_roundtrip_loss": 1, "nonpositive_price": 1, "invalid_source_price": 1})
        self.assertEqual(artifact_source(prepare_price("0.1000")), "0.1000")
        for rejected in prepared[2:]:
            self.assertIsNone(rejected.ring_value)
            with self.assertRaises(ValueError):
                artifact_source(rejected)
