"""ADR-0030: existing request parsing and UTM sanitization."""
import re


def sanitize_utm_string(val):
    if val is None:
        return ""
    val_str = str(val).strip()
    # Cyber defense: strip characters that look like SQL injection, shell tags, or XSS scripts
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.\/\:\@\s]", "", val_str)
    # Truncate to 200 characters to prevent memory exhausting allocations
    return cleaned[:200]


def _first(query, name, default=None):
    value = query.get(name, [default])
    return value[0] if value else default


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _split_symbols(query):
    raw = []
    for name in ("instrument", "instruments", "symbols", "tokens"):
        raw.extend(query.get(name, []))
    values = []
    for item in raw:
        values.extend(x.strip() for x in re.split(r"[\s,;]+", str(item)) if x.strip())
    return values
