import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_cached


class PulseBuildTests(unittest.TestCase):
    def test_default_entry_is_production_dashboard(self):
        html = build_cached.build_document()
        self.assertIn('id="app"', html)
        self.assertIn('googletagmanager', html)
        self.assertNotIn('noindex', html)
        self.assertNotIn('id="pulse-app"', html)
        self.assertNotIn('Sample data', html)

    def test_separate_sample_is_explicit_design_only(self):
        html = build_cached.build_pulse_document()
        self.assertIn('id="pulse-app"', html)
        self.assertIn('Sample data', html)
        self.assertIn('noindex,nofollow', html)
        self.assertNotIn('<script src=', html)
        self.assertNotIn('googletagmanager', html)
        self.assertNotIn('@import', html)
        self.assertIn('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>', html)
        self.assertIn('family=Inter:wght@400;500;600;700&amp;display=swap', html)

    def test_generated_outputs_are_separate(self):
        documents = build_cached.build_documents()
        self.assertEqual(set(documents), {'index.html', 'pulse-preview.html'})
        self.assertEqual(documents['index.html'], build_cached.build_document())
        self.assertIn('id="pulse-app"', documents['pulse-preview.html'])

    def test_embedded_legacy_document_is_preserved(self):
        html = build_cached.build_document('{"summary":[]}')
        self.assertIn('id="app"', html)
        self.assertIn('id="vd"', html)
        self.assertNotIn('id="pulse-app"', html)

    def test_invalid_design_artifacts_fail_build(self):
        names = build_cached.PULSE_SOURCES
        source = {name: (build_cached.PULSE_DIR / name).read_text() for name in names}
        cases = (
            ('pulse.html', source['pulse.html'] + '<div id="pulse-app"></div>', 'Duplicate'),
            ('pulse.html', source['pulse.html'].replace('id="pulse-app"', 'id="missing"'), 'missing'),
            ('pulse_design.js', source['pulse_design.js'] + '</ScRiPt>', 'Unsafe'),
        )
        for filename, bad, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name, body in source.items():
                    (root / name).write_text(bad if name == filename else body)
                with patch.object(build_cached, 'PULSE_DIR', root), self.assertRaisesRegex(RuntimeError, reason):
                    build_cached.build_pulse_document()


if __name__ == '__main__':
    unittest.main()
