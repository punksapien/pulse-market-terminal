import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_health", ROOT / "ops/pulse-host-health.py")
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)
MEMORY = "MemTotal: 421888 kB\nMemAvailable: 80000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
PSI = "some avg10=0.14 avg60=0.05 avg300=0.01 total=1\nfull avg10=0.14 avg60=0.04 avg300=0.00 total=1\n"
SERVICE = "ActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nNRestarts=12\nMemoryCurrent=95555584\nMemoryPeak=96419840\n"


class HostHealthTests(unittest.TestCase):
    def sample(self, mem=MEMORY, psi=PSI, service=SERVICE):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(kwargs["timeout"], 1)
            self.assertTrue(kwargs["check"])
            self.assertNotIn("shell", kwargs)
            return SimpleNamespace(stdout=service)
        output = health.report(read=lambda path: mem if path == "/proc/meminfo" else psi, run=run)
        return output, calls

    def test_safe_report_never_acts_or_reads_secrets(self):
        output, calls = self.sample()
        self.assertEqual(output["status"], "normal")
        self.assertEqual(output["mode"], "observe_only")
        self.assertEqual(len(calls), 2)
        for argv, unit in zip(calls, health.UNITS):
            self.assertEqual(argv[:3], ["/usr/bin/systemctl", "show", unit])
            self.assertEqual(set(argv[3:]), {"--property=" + p for p in health.PROPERTIES})
        self.assertEqual(output["units"][health.UNITS[0]]["NRestarts"], 12)

    def test_pressure_boundaries_and_no_swap_are_not_redis_rules(self):
        for kib, expected in [(32767, "critical"), (32768, "pressure"), (65535, "pressure"), (65536, "normal")]:
            with self.subTest(kib=kib):
                output, _ = self.sample(mem=MEMORY.replace("80000", str(kib)))
                self.assertEqual(output["status"], expected)
                self.assertEqual(output["memory_bytes"]["SwapTotal"], 0)

    def test_bad_meminfo_never_healthy(self):
        for value in ["", MEMORY.replace("80000", "-1"), MEMORY.replace("80000", "999999"),
                      MEMORY.replace("MemTotal: 421888", "MemTotal: 0"),
                      MEMORY.replace("SwapFree: 0", "SwapFree: 1"), MEMORY + "MemTotal: 421888 kB\n",
                      MEMORY.replace("MemAvailable: 80000 kB", "MemAvailable: 80000 MB")]:
            with self.subTest(value=value):
                self.assertEqual(self.sample(mem=value)[0]["status"], "unknown")

    def test_bad_psi_never_healthy(self):
        for value in ["", PSI.replace("avg10=0.14", "avg10=nan"), PSI.replace("avg10=0.14", "avg10=101"),
                      PSI.replace("avg10=0.14", "avg10=-1"), PSI + PSI, PSI.replace("avg10=0.14", "bad")]:
            with self.subTest(value=value):
                self.assertEqual(self.sample(psi=value)[0]["status"], "unknown")

    def test_refusal_78_never_restarted_or_retried(self):
        service = SERVICE.replace("active\n", "failed\n").replace("SubState=running", "SubState=failed")
        service = service.replace("Result=success", "Result=exit-code").replace("ExecMainStatus=0", "ExecMainStatus=78")
        output, calls = self.sample(service=service)
        self.assertEqual(output["status"], "degraded")
        self.assertEqual(output["units"][health.UNITS[0]]["ExecMainStatus"], 78)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(argv[1] == "show" for argv in calls))

    def test_start_limit_and_oom_reported_not_cleared(self):
        for result in ["start-limit-hit", "oom-kill", "signal", "timeout"]:
            output, _ = self.sample(service=SERVICE.replace("Result=success", "Result=" + result))
            self.assertEqual(output["status"], "degraded")
            self.assertEqual(output["units"][health.UNITS[0]]["Result"], result)

    def test_unexpected_properties_and_values_cannot_leak_arbitrary_text(self):
        service = SERVICE.replace("Result=success", "Result=redis://secret@example.invalid")
        service += "Environment=REDIS_PASSWORD=secret\n"
        output, _ = self.sample(service=service)
        self.assertNotIn("secret", str(output))
        self.assertEqual(output["status"], "degraded")

    def test_numeric_unknown_and_missing_output_never_healthy(self):
        for service in ["", SERVICE.replace("MemoryCurrent=95555584", "MemoryCurrent=[not set]"),
                        SERVICE + "NRestarts=1\n", "X" * 4097]:
            with self.subTest(service=service):
                self.assertNotEqual(self.sample(service=service)[0]["status"], "normal")

    def test_timeout_and_missing_proc_do_not_throw_or_retry(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, 1)
        def read(path):
            raise OSError("permission")
        output = health.report(read=read, run=run)
        self.assertEqual(output["status"], "unknown")
        self.assertEqual(len(calls), 2)

    def test_units_preserve_native_single_recovery_owner_and_terminal_refusal(self):
        for unit in health.UNITS:
            text = (ROOT / "ops" / unit).read_text()
            self.assertIn("Restart=on-failure", text)
            self.assertIn("RestartPreventExitStatus=78", text)
            self.assertIn("StartLimitBurst=5", text)
        unit = (ROOT / "ops/pulse-host-health.service").read_text()
        for value in ["Type=oneshot", "TimeoutStartSec=5", "MemoryMax=24M", "TasksMax=8", "Nice=19",
                      "DynamicUser=yes", "ProtectSystem=strict", "PrivateNetwork=yes", "LogNamespace=pulse"]:
            self.assertIn(value, unit)
        self.assertNotIn("Restart=", unit)
        timer = (ROOT / "ops/pulse-host-health.timer").read_text()
        self.assertIn("OnUnitInactiveSec=30s", timer)

    def test_pruner_is_only_dedicated_namespace_with_native_limits(self):
        config = (ROOT / "ops/journald-pulse.conf").read_text()
        self.assertIn("/etc/systemd/journald@pulse.conf", config)
        for value in ["SystemMaxUse=16M", "RuntimeMaxUse=1M", "MaxRetentionSec=2day", "RateLimitBurst=200"]:
            self.assertIn(value, config)
        self.assertEqual((ROOT / "ops/pulse-log-namespace.conf").read_text().splitlines()[-1], "LogNamespace=pulse")


if __name__ == "__main__":
    unittest.main()
