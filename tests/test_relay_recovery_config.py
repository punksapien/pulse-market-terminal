import hashlib
import unittest
from unittest.mock import patch

from tools.probes import relay_recovery_config as config


class RecoveryConfigTests(unittest.TestCase):
    def test_removes_only_exact_line_and_preserves_surroundings(self):
        for path, entry in config.ENTRIES.items():
            with self.subTest(path=path):
                content = b"# retained\n" + entry + b"other unrelated entry\n"
                with patch.dict(config.EXPECTED, {path: hashlib.sha256(content).hexdigest()}):
                    self.assertEqual(config.remove_entry(path, content), b"# retained\nother unrelated entry\n")

    def test_changed_missing_duplicate_or_suffix_refused(self):
        for path, entry in config.ENTRIES.items():
            for content in (b"", entry * 2, b"# changed\n" + entry, entry.rstrip(b"\n") + b" changed\n"):
                with self.subTest(path=path, content=content):
                    with self.assertRaises(ValueError):
                        config.remove_entry(path, content)

    def test_duplicate_refused_even_with_matching_hash(self):
        path = "/etc/fstab"
        content = config.ENTRIES[path] * 2
        with patch.dict(config.EXPECTED, {path: hashlib.sha256(content).hexdigest()}):
            with self.assertRaisesRegex(ValueError, "entry_count"):
                config.remove_entry(path, content)

    def test_unknown_path_refused(self):
        with self.assertRaisesRegex(ValueError, "unexpected_path"):
            config.remove_entry("/etc/shadow", b"")

    def test_render_is_bounded_nonsecret_boot_hook(self):
        rendered = config.cloud_init()
        self.assertTrue(rendered.startswith("#!/bin/sh\nset -eu\n"))
        self.assertLess(len(rendered.encode()), 16384)
        self.assertIn("scope['apply_clone']()", rendered)
        self.assertNotIn("redis://", rendered)
        self.assertNotIn("rediss://", rendered)

    def test_swap_drain_requires_measured_pages_plus_reserve(self):
        mib = 1024**2
        for used, available, total, expected in (
                (174, 544, 909, True), (174, 430, 909, False),
                (174, 431, 909, True), (0, 257, 909, True),
                (0, 256, 909, False), (0, 400, 412, False),
                (-1, 544, 909, False), (0, 1000, 909, False)):
            with self.subTest(used=used, available=available, total=total):
                self.assertEqual(config.swapoff_headroom(used*mib, available*mib, total*mib), expected)
        self.assertFalse(config.swapoff_headroom(True, 900*mib, 1000*mib))

    def test_mapper_must_match_encryption_and_exact_backing(self):
        text = ("/dev/mapper/pulse_cryptswap is active.\n  type: PLAIN\n"
                "  cipher: aes-xts-plain64\n  keysize: 512 bits\n"
                "  loop: /var/lib/pulse-swap/backing\n")
        self.assertTrue(config.expected_mapper_status(text))
        for bad in (text.replace("PLAIN", "LUKS2"), text.replace("512 bits", "256 bits"),
                    text.replace("/var/lib/pulse-swap/backing", "/dev/vda1"),
                    text + "  type: PLAIN\n", ""):
            self.assertFalse(config.expected_mapper_status(bad))
