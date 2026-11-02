import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from build_static import build
from build_cached import build_pulse_document


class StaticBuildTests(unittest.TestCase):
    def test_standalone_production_has_no_legacy_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            build(root,'production',production_enabled=True,standalone=True)
            self.assertIn('<title>CoinDCX Pulse</title>',(root/'public/index.html').read_text())
            self.assertFalse((root/'public/legacy').exists())
            self.assertFalse((root/'public/playbook.pdf').exists())
            self.assertEqual((root/'public/index.html').read_bytes(),(root/'public/pulse/index.html').read_bytes())
    def test_deployment_upload_keeps_pulse_build_sources(self):
        root=Path(__file__).resolve().parents[1]
        rules=(root/'.vercelignore').read_text().splitlines()
        self.assertNotIn('frontend/src/pulse/',rules)
        self.assertNotIn('build_cached.py',rules)

    def test_explicit_production_build_defaults_live_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,body in (("index.html","legacy"),("pulse-preview.html",'<html><head><title>Design preview</title><meta name="robots" content="noindex,nofollow"><meta name="description" content="Sample data only"></head><body>pulse</body></html>'),
                              ("Volatility Dashboard Playbook (1).pdf","pdf")):
                (root/name).write_text(body)
            build(root,"production",production_enabled=True)
            output=root/"public"
            self.assertEqual((output/"legacy/index.html").read_text(),"legacy")
            self.assertEqual((output/"index.html").read_text(),(output/"pulse/index.html").read_text())
            self.assertIn('history.replaceState', (output/"index.html").read_text())
            self.assertIn('searchParams.set("source","live")', (output/"index.html").read_text())
            page=(output/"index.html").read_text()
            self.assertIn('<title>CoinDCX Pulse</title>',page)
            self.assertNotIn('noindex',page)
            self.assertNotIn('Sample data only',page)
            self.assertEqual(page.count('googletagmanager.com/gtag/js'),1)
            self.assertIn('u.searchParams.get("source")!=="live"',page)
            script=page.split('<script>',1)[1].split('</script>',1)[0]
            check='''const vm=require("node:vm"),assert=require("node:assert/strict"),fs=require("node:fs");
const script=fs.readFileSync(0,"utf8");let changed=null;
vm.runInNewContext(script,{URL,location:{href:"https://fixture.invalid/?utm_source=review&source=sample"},
  history:{replaceState:(a,b,url)=>{changed=String(url);}}});
const u=new URL(changed);assert.equal(u.searchParams.get("source"),"live");
assert.equal(u.searchParams.get("utm_source"),"review");assert.equal(u.origin,"https://fixture.invalid");
'''
            result=subprocess.run(["node","-e",check],input=script,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            build(root,"production",production_enabled=False)
            self.assertEqual((output/"index.html").read_text(),"legacy")
            self.assertFalse((output/"legacy").exists())
            self.assertFalse((output/"pulse").exists())

    def test_only_explicit_preview_includes_sample_even_after_reused_build(self):
        for environment in ('preview', 'production', 'development', '', 'unexpected', None):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ('index.html', 'pulse-preview.html', 'Volatility Dashboard Playbook (1).pdf'):
                    (root / name).write_bytes(name.encode())
                (root / 'public' / 'pulse').mkdir(parents=True)
                (root / 'public' / 'pulse' / 'old.html').write_text('stale sample')
                with patch.dict('os.environ', {}, clear=True):
                    build(root, environment)
                self.assertEqual((root / 'public' / 'index.html').read_bytes(), b'index.html')
                self.assertEqual((root / 'public' / 'playbook.pdf').read_bytes(), b'Volatility Dashboard Playbook (1).pdf')
                self.assertEqual((root / 'public' / 'pulse').exists(), environment == 'preview')
                if environment == 'preview':
                    self.assertEqual((root / 'public' / 'pulse' / 'index.html').read_text(), build_pulse_document())
                    self.assertIn('[180000,720000].includes', (root / 'public' / 'pulse' / 'index.html').read_text())
                    self.assertFalse((root / 'public' / 'pulse' / 'old.html').exists())

    def test_stale_generated_page_is_never_read_for_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'index.html').write_text('legacy')
            (root/'Volatility Dashboard Playbook (1).pdf').write_text('pdf')
            # No generated Pulse file: the deployment must render source directly.
            with patch('build_static.build_pulse_document',return_value='<html>fresh source</html>') as render:
                build(root,'preview')
            render.assert_called_once_with()
            self.assertEqual((root/'public/pulse/index.html').read_text(),'<html>fresh source</html>')
            self.assertEqual((root/'public/index.html').read_text(),'legacy')
