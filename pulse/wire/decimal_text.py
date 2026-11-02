"""Wire grammar shared by formal schema generation and source producers."""
DECIMAL_PATTERN=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?(?:[0-9]|[1-3][0-9]|40))?$"


def decimal_text(value):
    """Exact shortest decimal string in the wire grammar. str(float) is not allowed: it renders 6.5e-05, whose
    two-digit zero-padded exponent the grammar refuses. Floats go through their shortest round-trip decimal."""
    from decimal import Decimal
    if isinstance(value, bool):
        raise ValueError("decimal_text_bool")
    d = Decimal(repr(value)) if type(value) is float else Decimal(value)
    if not d.is_finite():
        raise ValueError("decimal_text_nonfinite")
    plain = format(d, "f")
    if len(plain) <= 64:
        return plain
    # extreme magnitudes: scientific notation without a padded exponent
    sign, digits, exponent = d.as_tuple()
    adjusted = len(digits) + exponent - 1
    mantissa = str(digits[0]) + ("." + "".join(map(str, digits[1:])) if len(digits) > 1 else "")
    text = ("-" if sign else "") + mantissa + "e" + str(adjusted)
    if len(text) > 64 or abs(adjusted) > 40:
        raise ValueError("decimal_text_out_of_range")
    return text

