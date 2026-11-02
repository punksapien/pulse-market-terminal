"""ADR-0039: allowlisted static artifacts; fictional sample is Preview-only."""
import os
import re
from pathlib import Path
import shutil
from frontend.render import analytics_snippet
from build_cached import build_pulse_document

ROOT = Path(__file__).resolve().parent


def build(root=ROOT, environment=None, production_enabled=None, standalone=None):
    environment = os.environ.get('VERCEL_ENV') if environment is None else environment
    if production_enabled is None:
        production_enabled = os.environ.get('PULSE_PRODUCTION_ENABLED') == '1'
    if type(production_enabled) is not bool:
        raise ValueError('invalid_production_build_flag')
    active = environment == 'production' and production_enabled
    standalone = os.environ.get('PULSE_STANDALONE') == '1' if standalone is None else standalone
    if type(standalone) is not bool or standalone and environment not in ('preview','production'):
        raise ValueError('invalid_standalone_build')
    output = root / 'public'
    output.mkdir(exist_ok=True)
    sample = output / 'pulse'
    if sample.exists():
        shutil.rmtree(sample)
    legacy = output / 'legacy'
    if legacy.exists():
        shutil.rmtree(legacy)
    if not standalone:
        shutil.copyfile(root / 'index.html', output / 'index.html')
        shutil.copyfile(root / 'Volatility Dashboard Playbook (1).pdf', output / 'playbook.pdf')
    elif (output / 'playbook.pdf').exists():
        (output / 'playbook.pdf').unlink()
    if environment == 'preview' or active:
        sample.mkdir()
        # ADR-0182: ship current validated sources, never a stale generated bundle.
        pulse_page = build_pulse_document()
        (sample / 'index.html').write_text(pulse_page)
        if standalone:
            (output / 'index.html').write_text(pulse_page)
    if active:
        if not standalone:
            legacy.mkdir()
            shutil.copyfile(root / 'index.html', legacy / 'index.html')
        page = pulse_page
        if '<head>' not in page:
            raise ValueError('pulse_page_head_missing')
        # ADR-0130: production defaults to live without dropping UTM attribution.
        bootstrap = '<script>{const u=new URL(location.href);if(u.searchParams.get("source")!=="live"){u.searchParams.set("source","live");history.replaceState(null,"",u);}}</script>'
        page = re.sub(r'<meta name="robots"[^>]*>', '', page, flags=re.I)
        page = re.sub(r'<title>.*?</title>', '<title>CoinDCX Pulse</title>', page, count=1, flags=re.S)
        page = re.sub(r'<meta name="description"[^>]*>',
            '<meta name="description" content="Live multi-metric screener for CoinDCX USDT perpetual futures.">',page,count=1)
        page = page.replace('<head>', '<head>' + bootstrap + analytics_snippet(), 1)
        (output / 'index.html').write_text(page)
        (sample / 'index.html').write_text(page)


if __name__ == '__main__':
    build()
