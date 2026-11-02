"""ADR-0202: one-time pinned install on the verified host; default is read-only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


SIZE = 512 * 1024**2
DIRECTORY = Path("/var/lib/pulse-swap")
BACKING = DIRECTORY / "backing"
MAPPER = "/dev/mapper/pulse_cryptswap"
CRYPT_UNIT = "systemd-cryptsetup@pulse_cryptswap.service"
SWAP_UNIT = "dev-mapper-pulse_cryptswap.swap"
CONFIGS = (
    (Path("/etc/crypttab"), "d49b1e746f6c62f3f4748f8b97793ceef7b8e21cfd240ce7d77bcf527d39adfa",
     "pulse_cryptswap /var/lib/pulse-swap/backing /dev/urandom plain,swap,cipher=aes-xts-plain64,size=512,hash=plain,nofail"),
    (Path("/etc/fstab"), "e96bfa6d0c08f685624799172653f08ad6dc3ef391e0d459f56bc9b356b96433",
     "/dev/mapper/pulse_cryptswap none swap sw,nofail,pri=10,x-systemd.device-timeout=10s 0 0"),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def appended_config(data, expected, entry):
    if digest(data) != expected:
        raise ValueError("configuration_changed")
    if any(token in data for token in (b"pulse_cryptswap", b"/var/lib/pulse-swap")):
        raise ValueError("existing_swap_configuration")
    return data + (b"" if not data or data.endswith(b"\n") else b"\n") + b"# Pulse ADR-0202: dedicated per-boot encrypted swap only\n" + entry.encode("ascii") + b"\n"


def read_config(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_nlink != 1 or meta.st_mode & 0o022 or meta.st_size > 65536:
            raise ValueError("configuration_identity_refused")
        data = os.read(fd, 65537)
        if len(data) != meta.st_size:
            raise ValueError("configuration_read_changed")
        return data, meta
    finally:
        os.close(fd)


def new_file(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def native(*args):
    result = subprocess.run(args, capture_output=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError("native_command_refused")
    return result.stdout.decode("utf8", errors="strict")


def install(apply=False):
    if os.geteuid() != 0 or sys.platform != "linux":
        raise ValueError("verified_linux_root_required")
    lock = os.open("/run/pulse-encrypted-swap.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(DIRECTORY) or os.path.lexists(MAPPER):
            raise ValueError("existing_target_refused")
        if len(Path("/proc/swaps").read_text().splitlines()) != 1:
            raise ValueError("existing_swap_refused")
        disk = os.statvfs("/var/lib")
        if disk.f_bavail * disk.f_frsize < SIZE + 2 * 1024**3:
            raise ValueError("disk_reserve_refused")
        plans = []
        for path, expected, entry in CONFIGS:
            data, meta = read_config(path)
            plans.append((path, expected, data, meta, appended_config(data, expected, entry)))
        receipt = {"schema": "pulse.encrypted-swap-install.v1", "at_ms": int(time.time() * 1000),
                   "size_bytes": SIZE, "mapper": MAPPER, "phase": "preflight_passed",
                   "apply": apply, "app_restarts_requested": 0, "redis_calls": 0,
                   "before": {str(p): e for p, e, *_ in plans}}
        if not apply:
            return receipt
        os.mkdir(DIRECTORY, 0o700)
        def save():
            # Private local receipt contains no keys, environment, or configuration contents.
            target = DIRECTORY / "install-receipt.json"
            temp = DIRECTORY / "receipt.new"
            new_file(temp, json.dumps(receipt, sort_keys=True).encode())
            os.replace(temp, target)
        save()
        fd = os.open(BACKING, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.posix_fallocate(fd, 0, SIZE)
            os.fsync(fd)
        finally:
            os.close(fd)
        for path, expected, original, meta, replacement in plans:
            new_file(DIRECTORY / (path.name + ".before"), original)
            current, current_meta = read_config(path)
            if digest(current) != expected or (current_meta.st_dev, current_meta.st_ino) != (meta.st_dev, meta.st_ino):
                raise ValueError("configuration_changed_before_replace")
            temp = path.with_name("." + path.name + ".pulse-swap.new")
            new_file(temp, replacement, stat.S_IMODE(meta.st_mode))
            os.chown(temp, meta.st_uid, meta.st_gid)
            os.replace(temp, path)
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        receipt["phase"] = "configuration_installed"
        receipt["after"] = {str(p): digest(r) for p, _, _, _, r in plans}
        save()
        native("/usr/bin/systemctl", "daemon-reload")
        # Formatting is native crypttab's swap post-step, only on this NEW mapper.
        native("/usr/bin/systemctl", "start", CRYPT_UNIT)
        receipt["phase"] = "encrypted_mapping_started"
        save()
        native("/usr/bin/systemctl", "start", SWAP_UNIT)
        receipt["phase"] = "swap_started_verify_separately"
        save()
        return receipt
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        if sys.argv[1:] not in ([], ["--apply-approved-512"]):
            raise ValueError("mode_refused")
        print(json.dumps(install(apply=bool(sys.argv[1:]))))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "stopped", "error_type": type(error).__name__}))
        sys.exit(1)
