import json
import unittest
from decimal import Decimal

from pulse.wire.level_contract import validate_levels
from pulse.wire.pilot_policy import apply, CUTS
from pulse.wire.rows import available, empty_row

NOW = 1_800_000_000_000


def row_with(**fields):
    row = empty_row({"id": 0, "funding_hours": fields.pop("hours", 8)}, "sha256:" + "0" * 64)
    def put(path, unit, definition, value):
        node = row
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = available(unit, definition, value, source_ms=NOW - 1000, expires_ms=NOW + 60000, input_refs=["x"], now_ms=NOW)
    for key, value in fields.items():
        path, unit, definition = {
            "funding": (("funding", "native_estimate"), "fraction", "funding.v1"),
            "change": (("funding_change", "settled_last"), "fraction", "funding_change.v1"),
            "ls": (("ls", "global_ratio"), "ratio", "long_short.v1"),
            "oi1": (("oi", "change_1h"), "fraction", "open_interest.v1"),
            "oi4": (("oi", "change_4h"), "fraction", "open_interest.v1"),
            "oi24": (("oi", "change_24h"), "fraction", "open_interest.v1"),
        }[key]
        put(path, unit, definition, value)
    return row


def level(row, *path):
    node = row
    for key in path:
        node = node[key]
    return node["level"]


class PilotPolicyTests(unittest.TestCase):
    def test_boundaries_are_inclusive_lower_bounds(self):
        # (metric key, path, value, expected level): just below, exactly at, just above each cut
        cases = [
            # funding bps from anchor 1 bp at 8h: rate 0.0003 -> 2 bps
            ("funding", ("funding", "native_estimate"), "0.00029", 1), ("funding", ("funding", "native_estimate"), "0.0003", 2),
            ("funding", ("funding", "native_estimate"), "0.0005", 3), ("funding", ("funding", "native_estimate"), "0.001", 4),
            ("funding", ("funding", "native_estimate"), "0.0001", 1), ("funding", ("funding", "native_estimate"), "-0.0001", 2),
            ("funding", ("funding", "native_estimate"), "-0.0002", 2),
            ("change", ("funding_change", "settled_last"), "0.00009", 1), ("change", ("funding_change", "settled_last"), "0.0001", 2),
            ("change", ("funding_change", "settled_last"), "-0.0005", 3), ("change", ("funding_change", "settled_last"), "0.001", 4),
            ("oi1", ("oi", "change_1h"), "0.0099", 1), ("oi1", ("oi", "change_1h"), "0.01", 2), ("oi1", ("oi", "change_1h"), "-0.06", 4),
            ("oi4", ("oi", "change_4h"), "0.0599", 2), ("oi4", ("oi", "change_4h"), "0.06", 3),
            ("oi24", ("oi", "change_24h"), "0.14", 2), ("oi24", ("oi", "change_24h"), "0.15", 3), ("oi24", ("oi", "change_24h"), "-0.30", 4),
            ("ls", ("ls", "global_ratio"), "1.0", 1), ("ls", ("ls", "global_ratio"), "1.24", 1), ("ls", ("ls", "global_ratio"), "1.3", 2),
            ("ls", ("ls", "global_ratio"), "2.0", 3), ("ls", ("ls", "global_ratio"), "3.5", 4),
            # symmetric: 1/ratio lands in the same band
            ("ls", ("ls", "global_ratio"), "0.5", 3), ("ls", ("ls", "global_ratio"), "0.2", 4), ("ls", ("ls", "global_ratio"), "0.81", 1), ("ls", ("ls", "global_ratio"), "0.79", 2),
        ]
        for key, path, value, expected in cases:
            with self.subTest(key=key, value=value):
                row = row_with(**{key: value})
                apply(row)
                self.assertEqual(level(row, *path), expected)
                validate_levels(row)  # backend contract accepts the produced classification

    def test_native_interval_scales_funding(self):
        # 4h: rate x 2 -> 0.00015 x2 = 0.0003 -> 2 bps -> level 2; at 8h the same rate is 1 bp -> level 1
        for hours, expected in ((4, 2), (8, 1)):
            row = row_with(funding="0.00015", hours=hours)
            apply(row)
            self.assertEqual(row["funding"]["native_estimate"]["level"], expected)
            validate_levels(row)

    def test_exactly_at_ratio_cut_is_the_higher_band(self):
        for ratio, expected in (("1.25", 2), ("1.8", 3), ("3.0", 4), ("1.2499", 1)):
            row = row_with(ls=ratio)
            apply(row)
            self.assertEqual(row["ls"]["global_ratio"]["level"], expected, ratio)

    def test_unavailable_and_null_stay_unclassified(self):
        row = row_with()
        apply(row)
        for node in (row["funding"]["native_estimate"], row["ls"]["global_ratio"], row["oi"]["change_1h"], row["funding_change"]["settled_last"]):
            self.assertEqual((node["classification_status"], node["level"]), ("uncalibrated", None))
        zero = row_with(oi1="0")
        apply(zero)
        self.assertEqual(zero["oi"]["change_1h"]["level"], 1)  # zero is a valid Steady value, not missing

    def test_version_names_policy_and_window(self):
        row = row_with(oi4="0.07", funding="0.001")
        apply(row)
        self.assertEqual(row["oi"]["change_4h"]["classification_version"], "open_interest.v1.l2.4H.pilot1")
        self.assertEqual(row["funding"]["native_estimate"]["classification_version"], "funding.v1.l2.pilot1")

    def test_cuts_are_strictly_increasing_and_approved_values(self):
        self.assertEqual(CUTS["funding.v1"], ("2", "4", "9"))
        self.assertEqual(CUTS["funding_change.v1"], ("1", "5", "10"))
        self.assertEqual(CUTS["open_interest.v1"]["24H"], ("0.05", "0.15", "0.30"))
        ln = [Decimal(x) for x in CUTS["long_short.v1"]]
        self.assertTrue(ln[0] < ln[1] < ln[2])


if __name__ == "__main__":
    unittest.main()


class LiveFundingLevelTests(unittest.TestCase):
    def test_live_row_levels_match_the_snapshot_policy(self):
        from pulse.wire.pilot_policy import funding_level
        # (efr, native hours) -> level; same bands as the snapshot field, including native-interval scaling
        for efr, hours, expected in (("0.0001", 8, 1), ("0.00029", 8, 1), ("0.0003", 8, 2), ("-0.0002", 8, 2),
                                      ("0.0005", 8, 3), ("0.001", 8, 4), ("0.00015", 4, 2), ("0.00015", 8, 1)):
            with self.subTest(efr=efr, hours=hours):
                self.assertEqual(funding_level(efr, hours), (expected, "funding.v1.l2.pilot1"))

    def test_live_document_with_level_validates_and_a_forged_level_is_refused(self):
        import pathlib, sys
        from copy import deepcopy
        from pulse.wire import validation
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools" / "contracts"))
        from build import outputs
        from live_spec import live_fixtures
        from pulse.wire.pilot_policy import funding_level
        main = outputs()["docs/contracts/fixtures/snapshot.partial.json"]
        doc = deepcopy(live_fixtures(main)["live_funding.valid.json"])
        validation.live(doc, main["universe"], main["mapping_version"])  # earlier producers without a level stay valid
        for row in doc["rows"]:
            row["level"], row["classification_version"] = funding_level(row["efr"], row["h"])
        validation.live(doc, main["universe"], main["mapping_version"])
        forged = deepcopy(doc)
        forged["rows"][0]["level"] = 1 if doc["rows"][0]["level"] != 1 else 4
        with self.assertRaises(ValueError):
            validation.live(forged, main["universe"], main["mapping_version"])
        stale_version = deepcopy(doc)
        stale_version["rows"][0]["classification_version"] = "funding.v1.l2.pilot0"
        with self.assertRaises(ValueError):
            validation.live(stale_version, main["universe"], main["mapping_version"])


if __name__ == "__main__":
    unittest.main()
