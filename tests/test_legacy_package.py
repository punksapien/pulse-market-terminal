"""ADR-0029: package moves must preserve code, numbers, callers and shared state."""
import hashlib
import importlib
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pulse.legacy import vol_core, scoring, signal_engine, signal_config, signal_oi
from tests.legacy_parity_fixture import frozen_outputs


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    ('volatility', 'vol_core'),
    ('volatility_lightweight', 'scoring'),
    ('vol_signal_engine', 'signal_engine'),
    ('vol_signal_config', 'signal_config'),
    ('vol_signal_oi', 'signal_oi'),
)
SOURCE_HASHES = {
    'vol_core': 'dd2b99ef92a3064a2b68de0361401f8e9cf006805505e2785e169ef69fb1e451',
    'scoring': '6a9ae4a8ffce6e2c12b780066001ffbcec12f0bfd9d6622143b161aae4a3f3df',
    'signal_engine': '5437335c3fe1757391df97c63c2f35a15a771f5965efe6e9cdb569ab20e4466c',
    'signal_config': 'd0aa0abc447eccad9e1ecd1581dbd4051a769cb71e2fb933e9aafd2cad63b202',
    'signal_oi': 'af67b02b08cc68f0dd6709e8577dea2295f0fb5c939cb66be9dc895b1f163974',
}


class LegacyPackageTests(unittest.TestCase):
    def test_implementations_preserve_pre_move_bytes_except_sibling_imports(self):
        for name, digest in SOURCE_HASHES.items():
            source = (ROOT / 'pulse' / 'legacy' / f'{name}.py').read_text()
            source = source.replace('from .vol_core import (', 'from volatility import (')
            source = source.replace('from .signal_config import VolSignalConfig',
                                    'from vol_signal_config import VolSignalConfig')
            self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), digest, name)

    def test_api_still_uses_canonical_core_after_boundary_extraction(self):
        # ADR-0030: cross-module definition parity now guards the former API file.
        from api import index
        self.assertIs(index.VolatilityConfig, vol_core.VolatilityConfig)
        self.assertIs(index.run_lightweight_analysis, scoring.run_lightweight_analysis)

    def test_offline_outputs_equal_pre_move_baselines(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline parity only')):
            outputs = frozen_outputs(vol_core, scoring, signal_engine, signal_oi)
        self.assertEqual(outputs, {
            '60': 'c7c8c2b079d1dd0f90376bf590b72fe8de5d77f85e3d4d518b04d972cf7e1d92',
            '240': '838f0a5cd902f0de5cdabb217daef84ee03ee114b28cf6291da98e0d0770e548',
            '1D': 'abde994f218176a51b6e44761ffc573e97e8416d80c5e7fc7868f46fbedf65f4',
            'signals_84': '4e5be4d0a124b16db35940fc17a98389b010766b87a16ec83f6670616efe793a',
            'market_context': 'd369e7f857d66573bb42ac9f3d7f5eca8074e99d0cda2bc41ba166bd65a61177',
            'oi': '3341eaa9fb8521e9f260014b96f7da0bfa839563b83e923097b4c767d5e1a0bd',
        })

    def test_old_imports_are_exact_canonical_modules_in_both_orders(self):
        for old_first in (True, False):
            script = f'''
import importlib
pairs = {MODULES!r}
for old, name in pairs:
    names = [old, 'pulse.legacy.' + name]
    if not {old_first!r}: names.reverse()
    first, second = [importlib.import_module(name) for name in names]
    assert first is second, names
'''
            subprocess.run([sys.executable, '-c', script], cwd=ROOT, check=True,
                           capture_output=True, text=True, timeout=30)

    def test_old_private_helpers_configs_and_provider_state_are_shared(self):
        old_core = importlib.import_module('volatility')
        old_oi = importlib.import_module('vol_signal_oi')
        self.assertIs(old_core._finite, vol_core._finite)
        self.assertIs(old_oi._PROVIDER_STATE, signal_oi._PROVIDER_STATE)
        self.assertIs(old_oi._PROVIDER_STATE['lock'], signal_oi._PROVIDER_STATE['lock'])
        self.assertIs(importlib.import_module('vol_signal_config').VolSignalConfig, signal_config.VolSignalConfig)
        self.assertIs(pickle.loads(b'cvolatility\nVolatilityConfig\n.'), vol_core.VolatilityConfig)
        cfg = scoring.LWConfig(base=vol_core.VolatilityConfig(resolution='60'))
        self.assertEqual(pickle.loads(pickle.dumps(cfg)), cfg)

    def test_old_patch_target_intercepts_canonical_dependency(self):
        history = [{'timestamp': 1, 'sumOpenInterestValue': 100},
                   {'timestamp': 2, 'sumOpenInterestValue': 110}]
        with patch('vol_signal_oi.fetch_binance_oi', return_value=history) as fetch, \
                patch.object(signal_oi, '_provider_enabled', return_value=True), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('No live request')):
            metrics = signal_oi.fetch_oi_metrics('BTC', providers=['binance'])
        fetch.assert_called_once()
        self.assertTrue(metrics['oi_available'])
        with patch('vol_signal_engine.enrich_token', return_value={'compatibility': True}) as enrich:
            self.assertEqual(signal_engine.enrich_token({}), {'compatibility': True})
        enrich.assert_called_once_with({})

    def test_both_cli_entry_paths_keep_help(self):
        for old, name in MODULES[:2]:
            for arguments in ([f'{old}.py', '--help'], ['-m', f'pulse.legacy.{name}', '--help']):
                result = subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True,
                                        capture_output=True, text=True, timeout=30)
                self.assertIn('--resolution', result.stdout)
                self.assertIn('--output-json', result.stdout)
                self.assertIn('--demo', result.stdout)
                self.assertEqual(result.stderr, '')

    def test_demo_cli_old_and_package_paths_produce_same_report(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = []
            for label, invocation in (
                ('old', f'runpy.run_path({str(ROOT / "volatility_lightweight.py")!r}, run_name="__main__")'),
                ('package', 'runpy.run_module("pulse.legacy.scoring", run_name="__main__")'),
            ):
                stem = Path(directory) / label
                args = ['scoring', '--demo', '--instrument', 'BTC', '--resolution', '60',
                        '--scale-horizon', '1H', '--start-date', '2026-10-01', '--end-date', '2026-10-12',
                        '--output-json', str(stem.with_suffix('.json')),
                        '--output-csv', str(stem.with_suffix('.csv')),
                        '--output-html', str(stem.with_suffix('.html'))]
                script = f'''
import runpy, sys
from unittest.mock import patch
sys.argv = {args!r}
with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline demo only')):
    {invocation}
'''
                subprocess.run([sys.executable, '-c', script], cwd=ROOT, check=True,
                               capture_output=True, text=True, timeout=30)
                report = json.loads(stem.with_suffix('.json').read_text())
                report['meta'].pop('created_at_utc')
                self.assertEqual(report['meta']['source'], 'demo_synthetic')
                self.assertEqual(report['meta']['n_analyzed'], 1)
                self.assertIn('id="app"', stem.with_suffix('.html').read_text())
                self.assertTrue(stem.with_suffix('.csv').is_file())
                reports.append(report)
            self.assertEqual(reports[0], reports[1])

    def test_isolated_runtime_imports_without_root_compatibility_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copytree(ROOT / 'pulse', target / 'pulse', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT / 'frontend', target / 'frontend', ignore=shutil.ignore_patterns('__pycache__'))
            (target / 'api').mkdir()
            shutil.copy2(ROOT / 'api' / 'index.py', target / 'api' / 'index.py')
            for source in (ROOT / 'api').glob('volatility_lw_report*.json'):
                shutil.copy2(source, target / 'api' / source.name)
            script = f'''
import sys
sys.path.insert(0, {directory!r})
assert {str(ROOT)!r} not in sys.path
from unittest.mock import patch
with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline runtime only')):
    from api import index
    assert 'id="app"' in index._vol_signal_shell_template()
    assert index.VolatilityConfig.__module__ == 'pulse.legacy.vol_core'
    for resolution in ('60', '240', '1D'):
        report = index.load_precomputed_report(resolution)
        assert report['meta']['resolution'] == resolution
'''
            # Ignore PYTHONPATH, but keep installed user-site dependencies on this Mac.
            result = subprocess.run([sys.executable, '-E', '-B', '-c', script], cwd=target,
                                    capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
