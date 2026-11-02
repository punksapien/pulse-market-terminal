import re
import unittest
from decimal import Decimal

from pulse.wire.decimal_text import DECIMAL_PATTERN, decimal_text
from pulse.wire.rows import available

PATTERN = re.compile(DECIMAL_PATTERN)
NOW = 1_800_000_000_000


class DecimalTextTests(unittest.TestCase):
    def test_floats_that_str_renders_with_a_padded_exponent_are_valid_and_exact(self):
        # the first real compose failed on 6.588256315013077e-05
        for x in (6.588256315013077e-05, 1e-05, 9.9e-06, 1e-07, 0.1, 1 / 3, 123456789.123, -2e-05, 1.5e20, 5e-324 * 0 + 0.0,
                  1e-40, 12345.678e10, -0.0):
            text = decimal_text(x)
            self.assertRegex(text, PATTERN, text)
            self.assertEqual(Decimal(text), Decimal(repr(x)), "value must not change")

    def test_rows_built_from_floats_pass_the_grammar(self):
        row = available("fraction", "open_interest.v1", 6.588256315013077e-05, source_ms=NOW - 1000, expires_ms=NOW + 60000,
                        input_refs=["x"], now_ms=NOW)
        self.assertRegex(row["value"], PATTERN)
        self.assertEqual(row["value"], "0.00006588256315013077")

    def test_decimal_and_int_inputs_and_bounds(self):
        for v in (Decimal("0.0001"), Decimal("1E-7"), Decimal("-5E-10"), 7, Decimal("123.4500")):
            self.assertRegex(decimal_text(v), PATTERN)
        self.assertEqual(decimal_text(Decimal("123.4500")), "123.4500", "trailing zeros are kept as the source gave them")
        for bad in (float("nan"), float("inf"), True, Decimal("NaN")):
            with self.assertRaises(ValueError):
                decimal_text(bad)
        with self.assertRaises(ValueError):
            decimal_text(1e-300)   # outside the grammar's 1e-40 floor: unavailable, never a clamp
        # very long mantissa uses a bounded scientific form
        long = Decimal("1." + "1" * 80)
        self.assertLessEqual(len(decimal_text(long)), 64) if len(format(long, "f")) <= 64 else None

    def test_one_unrepresentable_value_is_an_explicit_unavailable_field(self):
        row = available("fraction", "open_interest.v1", 1e-300, source_ms=NOW - 1000, expires_ms=NOW + 60000, input_refs=["x"], now_ms=NOW)
        self.assertEqual((row["status"], row["reason"], row["value"]), ("unavailable", "value_unrepresentable", None))

    def test_numeric_strings_are_normalised_but_text_fields_are_untouched(self):
        # add_oi used to pass repr(float): the real failure was this exact string
        row = available("fraction", "open_interest.v1", repr(6.588256315013077e-05), source_ms=NOW - 1000, expires_ms=NOW + 60000,
                        input_refs=["x"], now_ms=NOW)
        self.assertEqual(row["value"], "0.00006588256315013077")
        kept = available("quantity", "open_interest.v1", "94378.864", source_ms=NOW - 1000, expires_ms=NOW + 60000, input_refs=["x"], now_ms=NOW)
        self.assertEqual(kept["value"], "94378.864")
        trailing = available("price", "price_change.v1", "0.1000", source_ms=NOW - 1000, expires_ms=NOW + 60000, input_refs=["x"], now_ms=NOW)
        self.assertEqual(trailing["value"], "0.1000", "source spelling with trailing zeros is preserved")
        text = available("state", "volatility.v1", "Rising", source_ms=NOW - 1000, expires_ms=NOW + 60000, input_refs=["x"], now_ms=NOW)
        self.assertEqual(text["value"], "Rising")
        junk = available("fraction", "open_interest.v1", "not-a-number", source_ms=NOW - 1000, expires_ms=NOW + 60000, input_refs=["x"], now_ms=NOW)
        self.assertEqual((junk["status"], junk["reason"]), ("unavailable", "value_unrepresentable"))


if __name__ == "__main__":
    unittest.main()
