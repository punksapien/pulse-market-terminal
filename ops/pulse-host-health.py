"""ADR-0201: bounded report-only host probe; no recovery, deletion or credentials."""
import json
import math
from pathlib import Path
import subprocess


UNITS = ("pulse-stream.service", "pulse-preview-driver.service")
PROPERTIES = ("ActiveState", "SubState", "Result", "ExecMainStatus", "NRestarts",
              "MemoryCurrent", "MemoryPeak")


def memory_sample(text):
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in {"MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"}:
            if len(fields) != 3 or fields[2] != "kB" or not fields[1].isdigit():
                raise ValueError("invalid_meminfo")
            key = fields[0][:-1]
            if key in values:
                raise ValueError("duplicate_meminfo")
            values[key] = int(fields[1]) * 1024
    if len(values) != 4 or values["MemTotal"] <= 0:
        raise ValueError("incomplete_meminfo")
    if values["MemAvailable"] > values["MemTotal"] or values["SwapFree"] > values["SwapTotal"]:
        raise ValueError("invalid_meminfo")
    return values


def pressure_sample(text):
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in {"some", "full"}:
            raise ValueError("invalid_psi")
        name = fields[0] + "_avg10"
        if name in values:
            raise ValueError("duplicate_psi")
        pairs = [item.split("=", 1) for item in fields[1:]]
        fields = dict(pairs)
        if len(fields) != len(pairs) or set(fields) != {"avg10", "avg60", "avg300", "total"}:
            raise ValueError("invalid_psi")
        value = float(fields["avg10"])
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("invalid_psi")
        values[name] = value
    if len(values) != 2:
        raise ValueError("incomplete_psi")
    return values


def service_sample(text):
    raw = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in PROPERTIES:
            if key in raw:
                raise ValueError("duplicate_property")
            raw[key] = value
    if set(raw) != set(PROPERTIES):
        raise ValueError("incomplete_service")
    result = {}
    allowed = {
        "ActiveState": {"active", "inactive", "failed", "activating", "deactivating", "reloading"},
        "SubState": {"running", "dead", "failed", "auto-restart", "start", "stop", "stop-sigterm"},
        "Result": {"success", "exit-code", "signal", "core-dump", "oom-kill", "timeout", "start-limit-hit", "resources", "watchdog"},
    }
    for key, value in raw.items():
        if key in allowed:
            result[key] = value if value in allowed[key] else "unknown"
        elif value.isdigit() and len(value) <= 20:
            result[key] = int(value)
        else:
            result[key] = None
    return result


def report(read=lambda path: Path(path).read_text(), run=subprocess.run):
    output = {"service": "pulse-host-health", "mode": "observe_only", "status": "unknown"}
    try:
        memory = memory_sample(read("/proc/meminfo"))
        output["memory_bytes"] = memory
        output["pressure"] = pressure_sample(read("/proc/pressure/memory"))
        available = memory["MemAvailable"]
        output["status"] = "critical" if available < 32 * 1024**2 else "pressure" if available < 64 * 1024**2 else "normal"
    except (OSError, ValueError, KeyError, OverflowError):
        output["status"] = "unknown"
    services = {}
    for unit in UNITS:
        try:
            completed = run(["/usr/bin/systemctl", "show", unit,
                             *["--property=" + p for p in PROPERTIES]],
                            capture_output=True, text=True, timeout=1, check=True)
            # systemctl emits only seven fixed properties, never Environment/cmdlines.
            if len(completed.stdout) > 4096:
                raise ValueError("service_output_bound")
            services[unit] = service_sample(completed.stdout)
            if (services[unit]["ActiveState"] != "active" or
                    services[unit]["SubState"] != "running" or
                    services[unit]["Result"] != "success" or
                    any(services[unit][p] is None for p in PROPERTIES[3:])):
                output["status"] = "degraded" if output["status"] == "normal" else output["status"]
        except (OSError, ValueError, subprocess.SubprocessError):
            services[unit] = {"status": "unknown"}
            output["status"] = "unknown"
    output["units"] = services
    return output


if __name__ == "__main__":
    print(json.dumps(report(), separators=(",", ":"), allow_nan=False))
