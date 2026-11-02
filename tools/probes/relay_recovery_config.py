"""ADR-0204: exact non-secret swap-config transform for the replacement only."""
from __future__ import annotations

import hashlib

ENTRIES = {
    "/etc/crypttab": b"pulse_cryptswap /var/lib/pulse-swap/backing /dev/urandom plain,swap,cipher=aes-xts-plain64,size=512,hash=plain,nofail\n",
    "/etc/fstab": b"/dev/mapper/pulse_cryptswap none swap sw,nofail,pri=10,x-systemd.device-timeout=10s 0 0\n",
}
EXPECTED = {
    "/etc/crypttab": "658b686747eb6ca5dc8bbc1a9770ab061d008a0f9771286efc126572ec3b07bd",
    "/etc/fstab": "02447feae7e510668a45d4908cd40dcbaee64fec1451164bca6cc9d5274c248b",
}


def remove_entry(path: str, content: bytes) -> bytes:
    if path not in ENTRIES:
        raise ValueError("unexpected_path")
    entry = ENTRIES[path]
    if hashlib.sha256(content).hexdigest() != EXPECTED[path]:
        raise ValueError("configuration_changed")
    if content.splitlines(keepends=True).count(entry) != 1:
        raise ValueError("unexpected_entry_count")
    return b"".join(line for line in content.splitlines(keepends=True) if line != entry)


def swapoff_headroom(used_bytes: int, available_bytes: int, total_bytes: int) -> bool:
    """Drain only the measured used pages with an additional256MiB reserve."""
    values = (used_bytes, available_bytes, total_bytes)
    return (all(type(v) is int and v >= 0 for v in values)
            and total_bytes >= 800 * 1024**2 and available_bytes <= total_bytes
            and used_bytes + 256 * 1024**2 < available_bytes)


def expected_mapper_status(text: str) -> bool:
    fields = {}
    for line in text.splitlines()[1:]:
        key, separator, value = line.strip().partition(":")
        if separator:
            if key in fields:
                return False
            fields[key] = value.strip()
    return (fields.get("type") == "PLAIN" and fields.get("cipher") == "aes-xts-plain64"
            and fields.get("keysize") == "512 bits"
            and fields.get("loop") == "/var/lib/pulse-swap/backing")


def finish_partial_clone() -> None:
    """Explicit operator continuation after exact config transform, not a retry loop."""
    import json
    import os
    from pathlib import Path
    import stat
    import subprocess
    import time
    directory = Path("/var/lib/pulse-relay-recovery-20261029")
    meta = directory.lstat()
    if os.geteuid() != 0 or not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o700:
        raise ValueError("partial_recovery_identity")
    after = {}
    for path in ENTRIES:
        backup = directory / (Path(path).name + ".before")
        for file in (Path(path), backup):
            m = file.lstat()
            if not stat.S_ISREG(m.st_mode) or m.st_uid != 0 or m.st_mode & 0o022 or m.st_size > 65536:
                raise ValueError("configuration_identity_refused")
        expected = remove_entry(path, backup.read_bytes())
        if Path(path).read_bytes() != expected:
            raise ValueError("partial_configuration_changed")
        after[path] = hashlib.sha256(expected).hexdigest()
    for unit in ("pulse-stream.service", "pulse-preview-driver.service"):
        p = subprocess.run(["systemctl", "show", unit, "--property=ActiveState", "--value"],
                           capture_output=True, timeout=5, check=True)
        if p.stdout.strip() != b"inactive":
            raise ValueError("application_must_be_stopped")
    memory = {k: int(v.split()[0]) * 1024 for k, v in
              (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
    before_swaps = Path("/proc/swaps").read_text().splitlines()[1:]
    used = 0
    for line in before_swaps:
        fields = line.split()
        if os.path.realpath(fields[0]) != os.path.realpath("/dev/mapper/pulse_cryptswap"):
            raise ValueError("unexpected_active_swap")
        used += int(fields[3]) * 1024
    if not swapoff_headroom(used, memory["MemAvailable"], memory["MemTotal"]):
        raise ValueError("swapoff_headroom_refused")
    if before_swaps:
        subprocess.run(["swapoff", "/dev/mapper/pulse_cryptswap"],
                       capture_output=True, timeout=30, check=True)
    if Path("/proc/swaps").read_text().splitlines()[1:]:
        raise ValueError("swap_still_active")
    # daemon-reload removes the generated swap unit after its fstab entry goes;
    # do not turn that expected missing unit into an unknown recovery outcome.
    if Path("/dev/mapper/pulse_cryptswap").exists():
        # The crypttab-generated service disappears too. Close only the positively
        # identified mapper after /proc/swaps independently confirms it is unused.
        status = subprocess.run(["cryptsetup", "status", "pulse_cryptswap"],
                                capture_output=True, text=True, timeout=5, check=True)
        if not expected_mapper_status(status.stdout):
            raise ValueError("unexpected_mapper_identity")
        subprocess.run(["cryptsetup", "close", "pulse_cryptswap"],
                       capture_output=True, timeout=30, check=True)
    if Path("/dev/mapper/pulse_cryptswap").exists():
        raise ValueError("mapper_still_active")
    receipt = {"schema": "pulse.relay-clone-recovery.v1", "at_ms": int(time.time() * 1000),
               "ram_bytes": memory["MemTotal"], "apps_stopped_disabled": True,
               "removed_only_exact_swap_entries": True, "secrets_downloaded": False,
               "redis_calls": 0, "measured_swap_drain_bytes": used,
               "available_before_drain_bytes": memory["MemAvailable"],
               "reserve_bytes": 256 * 1024**2, "after": after}
    fd = os.open(directory / "complete.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(receipt, sort_keys=True))


def apply_clone() -> None:
    """Non-secret early-boot recovery, never run on the old 0.5 GiB host."""
    import json
    import os
    from pathlib import Path
    import stat
    import subprocess
    import time

    def native(*args):
        result = subprocess.run(args, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("native_recovery_refused")
        return result.stdout.decode("utf8")

    if os.geteuid() != 0:
        raise ValueError("root_required")
    memory = {k: int(v.split()[0]) * 1024 for k, v in
              (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
    if memory["MemTotal"] < 800 * 1024**2:
        raise ValueError("replacement_capacity_required")
    directory = Path("/var/lib/pulse-relay-recovery-20261029")
    if directory.exists():
        if (directory / "complete.json").is_file():
            return
        raise ValueError("partial_recovery_inspect_manually")
    native("systemctl", "disable", "--now", "pulse-stream.service", "pulse-preview-driver.service")
    plans = []
    for path in ENTRIES:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_nlink != 1 or meta.st_mode & 0o022 or meta.st_size > 65536:
                raise ValueError("configuration_identity_refused")
            data = os.read(fd, 65537)
            plans.append((Path(path), data, meta, remove_entry(path, data)))
        finally:
            os.close(fd)
    os.mkdir(directory, 0o700)

    def save_new(path, data, mode=0o600):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)

    for path, data, meta, replacement in plans:
        save_new(directory / (path.name + ".before"), data)
        if path.read_bytes() != data:
            raise ValueError("configuration_race_refused")
        temp = path.with_name(path.name + ".pulse-recovery-new")
        save_new(temp, replacement, stat.S_IMODE(meta.st_mode))
        os.chown(temp, meta.st_uid, meta.st_gid)
        os.replace(temp, path)
    native("systemctl", "daemon-reload")
    swaps = Path("/proc/swaps").read_text().splitlines()[1:]
    for line in swaps:
        fields = line.split()
        if fields[0] != "/dev/dm-0" and fields[0] != "/dev/mapper/pulse_cryptswap":
            raise ValueError("unexpected_active_swap")
        # Verify dm-0 identity, not just its likely name.
        if os.path.realpath(fields[0]) != os.path.realpath("/dev/mapper/pulse_cryptswap"):
            raise ValueError("unexpected_active_mapper")
        if int(fields[3]) != 0 or memory["MemAvailable"] < 600 * 1024**2:
            raise ValueError("swapoff_headroom_refused")
        native("swapoff", "/dev/mapper/pulse_cryptswap")
    if Path("/dev/mapper/pulse_cryptswap").exists():
        native("systemctl", "stop", "systemd-cryptsetup@pulse_cryptswap.service")
    save_new(directory / "complete.json", json.dumps({
        "schema": "pulse.relay-clone-recovery.v1", "at_ms": int(time.time() * 1000),
        "ram_bytes": memory["MemTotal"], "apps_stopped_disabled": True,
        "removed_only_exact_swap_entries": True, "secrets_downloaded": False,
        "redis_calls": 0,
        "after": {str(path): hashlib.sha256(replacement).hexdigest()
                  for path, _, _, replacement in plans},
    }, sort_keys=True).encode())
    print("pulse_clone_recovery_complete_apps_disabled")


def cloud_init() -> str:
    import base64
    from pathlib import Path
    code = base64.b64encode(Path(__file__).read_bytes()).decode("ascii")
    # No secret or operator address is embedded in the provider-visible launch script.
    # Lightsail wraps user data in /bin/sh; nested shebangs are only comments.
    return ("#!/bin/sh\nset -eu\n"
            "python3 - <<'PULSE_RECOVERY'\nimport base64\n"
            "scope = {'__name__': 'pulse_recovery'}\n"
            f"exec(base64.b64decode('{code}'), scope)\n"
            "scope['apply_clone']()\nPULSE_RECOVERY\n")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--user-data"]:
        raise SystemExit("only --user-data rendering is allowed locally")
    print(cloud_init())
