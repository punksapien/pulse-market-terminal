from pathlib import Path
from collections import Counter
from html.parser import HTMLParser
import json
import re

from frontend.render import SOURCE_DIR, read_volatility_source, render_volatility_document


ROOT = Path(__file__).resolve().parent
API_DIR = ROOT / "api"
PULSE_DIR = SOURCE_DIR / "pulse"
# order matters: model, then the live data layer, then the fixture and the controller
PULSE_SOURCES = ("pulse.html", "pulse.css", "pulse_model.js", "pulse_decimal.js", "pulse_live.js", "pulse_ladder.js",
                 "pulse_adapter.js", "pulse_wire.js", "pulse_loader.js", "pulse_history.js", "pulse_ticks.js", "pulse_socket.js", "pulse_bridge.js", "pulse_copy.js", "pulse_navigation.js", "pulse_fixture.js", "pulse_design.js")
REQUIRED_DASHBOARD_IDS = {
    "app",
    "regime-tabs",
    "signal-strip",
    "col-header-row",
    "token-list",
    "timeframe-control",
    "filter-sheet",
    "detail-sheet",
    "detail-vol-meter",
    "detail-why-box",
    "signal-since-line",
}

FORBIDDEN_DASHBOARD_FRAGMENTS = {
    'id="app-header"',
    "Volatility components",
    'id="component-yz"',
    'id="component-ewma"',
    'id="component-jump"',
}

REQUIRED_METER_FRAGMENTS = {
    "Calm: '#4965D2'",
    "Active: '#37B271'",
    "Hot: '#F7931A'",
    "Extreme: '#F13B4D'",
    "calm: 'M8 13L4.45 9.45'",
    "active: 'M8 13V8'",
    "hot: 'M8 13L11.55 9.45'",
    "extreme: 'M8 13H13'",
    "const volatilityColor = volatilityBandColor(token.regime);",
    "Q('#detail-vol-score').style.color = regimeColor;",
}

REQUIRED_MARKET_LAYOUT_FRAGMENTS = {
    "grid-template-columns: 129px 92px 12px 70px 12px 28px;",
    "grid-template-columns: 129px 12px 80px 12px 70px 12px 28px;",
}

REQUIRED_SIGNAL_SINCE_FRAGMENTS = {
    "const SIGNAL_SINCE_STATES = new Set([",
    "'MomentumLong'",
    "'MomentumShort'",
    "'BreakoutWatch'",
    "'Range'",
    "signalSinceLine.hidden = !signalSince;",
    "since $${formatPrice(sincePrice)}",
}


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        self.ids.extend(value for key, value in attrs if key == "id" and value)


def build_pulse_document() -> str:
    """ADR-0025: static sample-only entry; the legacy API document stays separate."""
    names = PULSE_SOURCES
    files = {name: (PULSE_DIR / name).read_text(encoding="utf-8") for name in names}
    parser = IdCollector()
    parser.feed(files["pulse.html"])
    if any(count > 1 for count in Counter(parser.ids).values()):
        raise RuntimeError("Duplicate Pulse design IDs")
    required = {"pulse-app", "metric-tabs", "level-tabs", "market-viewport", "row-pool",
                "pulse-sheet", "sheet-content", "timeframe-risk-disclaimer", "detail-risk-disclaimer"}
    if not required.issubset(parser.ids):
        raise RuntimeError("Pulse design is missing required interaction regions")
    javascript = "\n".join(files[name] for name in names if name.endswith(".js"))
    if "</script" in javascript.lower():
        raise RuntimeError("Unsafe inline script terminator")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#ffffff">'
        '<meta name="robots" content="noindex,nofollow">'
        '<title>CoinDCX Pulse · Design preview</title>'
        '<meta name="description" content="Illustrative multi-metric screener design. Sample data only; not live trading signals.">'
        # ADR-0027: discover the unchanged font stylesheet directly, not via CSS @import.
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&amp;display=swap">'
        f'<style>{files["pulse.css"]}</style></head><body>{files["pulse.html"]}'
        f'<script>{javascript}</script></body></html>'
    )


def build_document(data_json: str = "") -> str:
    if not data_json:
        return render_volatility_document(None, preview=True)
    return render_volatility_document(data_json, preview=True)


def build_documents() -> dict[str, str]:
    # ADR-0037: sample output can never replace the production homepage.
    return {"index.html": build_document(), "pulse-preview.html": build_pulse_document()}


def extract_preview_payload() -> str:
    # The dashboard opens on 1D/24H, so a complete fresh build should drive the
    # preview from that packaged report instead of perpetuating whatever payload
    # happened to be embedded in the previous preview.
    for report_path in (
        API_DIR / "volatility_lw_report_1D.json",
    ):
        if not report_path.exists():
            continue
        try:
            payload = report_path.read_text(encoding="utf-8")
            report = json.loads(payload)
            meta = report.get("meta", {})
            if (
                str(meta.get("resolution")) == "1D"
                and str(meta.get("scale_horizon", "")).upper() == "24H"
                and isinstance(report.get("summary"), list)
                and report["summary"]
            ):
                return payload
        except (OSError, TypeError, ValueError):
            continue

    # Retain the prior embedded payload only when a valid packaged default report
    # is unavailable (for example, a frontend-only checkout).
    preview_path = ROOT / "vol_signal_preview.html"
    if not preview_path.exists():
        return ""
    content = preview_path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="vd" type="application/json">(.*?)</script>',
        content,
        flags=re.DOTALL,
    )
    return match.group(1) if match else ""


def validate_sources() -> None:
    for filename in ("vs.html", "vs.css", "vs.js"):
        path = SOURCE_DIR / "volatility" / filename
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            raise RuntimeError(f"Required frontend source is missing or empty: {filename}")

    body = read_volatility_source("vs.html")
    parser = IdCollector()
    parser.feed(body)
    duplicate_ids = sorted(key for key, count in Counter(parser.ids).items() if count > 1)
    if duplicate_ids:
        raise RuntimeError(f"Duplicate dashboard IDs: {', '.join(duplicate_ids)}")

    missing_ids = sorted(REQUIRED_DASHBOARD_IDS - set(parser.ids))
    if missing_ids:
        raise RuntimeError(f"Dashboard is missing required IDs: {', '.join(missing_ids)}")

    forbidden_fragments = sorted(
        fragment for fragment in FORBIDDEN_DASHBOARD_FRAGMENTS if fragment in body
    )
    if forbidden_fragments:
        raise RuntimeError(
            "Dashboard contains removed UI fragments: "
            + ", ".join(forbidden_fragments)
        )

    css = read_volatility_source("vs.css")
    if css.count("{") != css.count("}"):
        raise RuntimeError("Dashboard CSS contains unbalanced braces")
    missing_layout_fragments = sorted(
        fragment for fragment in REQUIRED_MARKET_LAYOUT_FRAGMENTS if fragment not in css
    )
    if missing_layout_fragments:
        raise RuntimeError(
            "Dashboard 343px market-row layout contract is incomplete: "
            + ", ".join(missing_layout_fragments)
        )

    javascript = read_volatility_source("vs.js")
    missing_meter_fragments = sorted(
        fragment for fragment in REQUIRED_METER_FRAGMENTS if fragment not in javascript
    )
    if missing_meter_fragments:
        raise RuntimeError(
            "Dashboard volatility-band meter contract is incomplete: "
            + ", ".join(missing_meter_fragments)
        )
    missing_signal_fragments = sorted(
        fragment
        for fragment in REQUIRED_SIGNAL_SINCE_FRAGMENTS
        if fragment not in javascript
    )
    if missing_signal_fragments:
        raise RuntimeError(
            "Dashboard status-transition contract is incomplete: "
            + ", ".join(missing_signal_fragments)
        )


def main() -> None:
    validate_sources()
    payload = extract_preview_payload()
    for filename, document in build_documents().items():
        (ROOT / filename).write_text(document, encoding="utf-8")
    if payload:
        (ROOT / "vol_signal_preview.html").write_text(
            build_document(payload),
            encoding="utf-8",
        )
    print("Built index.html from canonical frontend sources (no API asset copies).")
    if payload:
        print(f"Rebuilt vol_signal_preview.html with {len(payload):,} bytes of embedded report data.")


if __name__ == "__main__":
    main()
