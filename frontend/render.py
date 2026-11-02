"""ADR-0028: one volatility source and renderer for build, API and CLI."""
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent / "src"
VOLATILITY_DIR = SOURCE_DIR / "volatility"
ANALYTICS_ID = "G-786ZD5X9H3"


def read_volatility_source(filename: str) -> str:
    if filename not in ("vs.html", "vs.css", "vs.js"):
        raise ValueError(f"Unknown volatility resource: {filename}")
    return (VOLATILITY_DIR / filename).read_text(encoding="utf-8")


def analytics_snippet(*, compact: bool = True) -> str:
    statements = (
        ('window.dataLayer=window.dataLayer||[];', 'window.dataLayer = window.dataLayer || [];'),
        ('function gtag(){dataLayer.push(arguments);}', 'function gtag(){dataLayer.push(arguments);}'),
        ('gtag("js",new Date());', 'gtag("js", new Date());'),
        (f'gtag("config","{ANALYTICS_ID}");', f'gtag("config", "{ANALYTICS_ID}");'),
    )
    body = ''.join(pair[0] if compact else '  ' + pair[1] for pair in statements)
    return (
        '<!-- Google tag (gtag.js) -->'
        f'<script async src="https://www.googletagmanager.com/gtag/js?id={ANALYTICS_ID}"></script>'
        f'<script>{body}</script>'
    )


def render_volatility_document(data_json: str | None = None, *, preview: bool = False) -> str:
    css = read_volatility_source("vs.css")
    body = read_volatility_source("vs.html")
    javascript = read_volatility_source("vs.js")
    embedded = (
        f'<script id="vd" type="application/json">{data_json}</script>'
        if data_json is not None else ''
    )
    viewport = 'width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no'
    if preview:
        viewport += ',viewport-fit=cover'
    title = 'CoinDCX Volatility' if preview else 'CoinDCX VOLATILITY : Volatility Discovery'
    description = (
        'Discover volatility regimes, trends, and trading signals across CoinDCX futures markets.'
        if preview else
        'Real-time crypto volatility discovery tool. Find tradeable movement, assess direction, and get actionable signals.'
    )
    theme = '<meta name="theme-color" content="#ffffff">' if preview else ''
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'{analytics_snippet(compact=preview)}'
        f'<meta name="viewport" content="{viewport}">{theme}'
        f'<title>{title}</title>'
        f'<meta name="description" content="{description}">'
        f'<style>{css}</style></head><body>{body}{embedded}'
        f'<script>{javascript}</script></body></html>'
    )
