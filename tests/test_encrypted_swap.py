import hashlib
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("encrypted_swap", ROOT / "tools/probes/pulse_encrypted_swap.py")
swap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(swap)


class EncryptedSwapTests(unittest.TestCase):
    def test_configuration_preserves_existing_bytes(self):
        for original in [b"", b"# existing\n", b"root / ext4 defaults 0 1", b"# unicode \xc3\xa9\n"]:
            result = swap.appended_config(original, hashlib.sha256(original).hexdigest(), "new entry")
            self.assertTrue(result.startswith(original))
            self.assertTrue(result.endswith(b"new entry\n"))
            self.assertEqual(result.count(b"new entry"), 1)

    def test_changed_configuration_is_refused(self):
        with self.assertRaisesRegex(ValueError, "configuration_changed"):
            swap.appended_config(b"changed", "0" * 64, "new")

    def test_preexisting_name_or_path_is_refused_even_when_hash_matches(self):
        for text in [b"pulse_cryptswap", b"# old /var/lib/pulse-swap/backing"]:
            with self.assertRaisesRegex(ValueError, "existing_swap_configuration"):
                swap.appended_config(text, hashlib.sha256(text).hexdigest(), "new")

    def test_only_encrypted_device_can_be_activated_as_swap(self):
        crypt = swap.CONFIGS[0][2].split()
        fstab = swap.CONFIGS[1][2].split()
        self.assertEqual(crypt[:3], ["pulse_cryptswap", str(swap.BACKING), "/dev/urandom"])
        for value in ["plain", "swap", "cipher=aes-xts-plain64", "size=512", "hash=plain", "nofail"]:
            self.assertIn(value, crypt[3].split(","))
        self.assertEqual(fstab[:3], [swap.MAPPER, "none", "swap"])
        self.assertNotIn(str(swap.BACKING), swap.CONFIGS[1][2])
        self.assertIn("nofail", fstab[3].split(","))
        self.assertEqual(swap.SIZE, 536870912)

    def test_no_apps_or_secret_reader_in_install_commands(self):
        source = (ROOT / "tools/probes/pulse_encrypted_swap.py").read_text()
        for forbidden in ["production.env", "production-driver.env", "--showkeys", '"restart"', '"reset-failed"', '"swapoff"', "apt-get"]:
            self.assertNotIn(forbidden, source)
        self.assertIn("os.O_EXCL | os.O_NOFOLLOW", source)
        self.assertIn("os.posix_fallocate(fd, 0, SIZE)", source)
        self.assertIn("LOCK_EX | fcntl.LOCK_NB", source)
        self.assertIn("timeout=20", source)


if __name__ == "__main__":
    unittest.main()
