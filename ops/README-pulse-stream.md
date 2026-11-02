# Existing-host staging relay (ADR-0101)

## Proposed host maintenance (ADR-0201, NOT installed)

29Oct diagnostics prove global host OOM, not just Redis pressure. The host has
412MiB RAM/no swap. Adding another recovery process or deleting disk logs does
not supply RAM. Existing systemd failure restart/cooldown and exit78 refusal
remain the sole application recovery owner; never reset-failed to bypass bans.

Prepared files, pending verified activation approval:

- `pulse-host-health.py`: report-only fixed-unit/proc probe; no credentials,
  network, mutations, restarts or deletion. Manual read-only execution is safe.
- `pulse-host-health.service` and `.timer`: bounded oneshot/30s, no resident
  daemon. Deploy script at `/opt/pulse-stream/ops/pulse-host-health.py`; units
  belong under `/etc/systemd/system/`. Do not enable before overhead validation.
- `journald-pulse.conf`: ONLY `/etc/systemd/journald@pulse.conf`, not the global
  journal. Native16MiB/2day disk retention,1MiB runtime fallback and rate limits.
  Observe actual allocation/active-file overhead; this is not an exact hard cap.
- `pulse-log-namespace.conf`: optional exact `20-pulse-logs.conf` drop-in in EACH
  Pulse unit's `.service.d/` directory. Existing production env overrides stay
  untouched. Requires an approved NEXT service start to route logs; don't
  restart working production just to install a pruner. Do not vacuum global logs.

Validation: Python unit suite; `systemd-analyze verify` on copied units against
the host version; bounded read-only probe, peak RSS and runtime measurement.
Then measure namespace usage/rotation and30min OOM/restart/latency behaviour.
Rollback removes only these newly installed files/timer and leaves existing
service/restart/env overrides. Namespace deletion/global pruning are not part of
rollback. Capacity mitigation (including encrypted swap if chosen) is separate.

Public payload, viewer requests, exchange calls and render cost: unchanged.
Not checked: activated namespace retention,30min soak and adequate peak RAM.

## Original staging installation

28Oct update (ADR-0102/0103/0161/0162): project owner authorised oneUSD5/month Mumbai
Lightsail relay. `pulse-staging-relay` is installed from Git revision
`f9d8650105cc6e81bbb0b2c00346dfac5bce5ba7`, Ubuntu24.04, pinned Node24.22.1.
User attached staticIPv4 `15.252.101.201`; authenticated AWS API confirms it.
Console-verified ED25519 host fingerprint:
`SHA256:tgY10rMV2K4AMatJL9wc4ca+TUipbmJRWOEmiuG4KD4`.
This is ONLY the staging price publisher, not an app/Redis production cutover.

Requirements: Linux/systemd, supported Node 24, outbound TLS to CoinDCX plus the
approved staging Redis endpoint, accurate NTP clock. No inbound listener is used.
The 192 MiB process limit is a staging bound, not a measured all-universe capacity.

Install this branch's `package.json`, `package-lock.json`, `pulse/stream/` under
`/opt/pulse-stream`, run `npm ci --omit=dev --ignore-scripts`, owned by a dedicated
`pulse-stream` account. Install the reviewed unit from `ops/pulse-stream.service`.
Do not copy the entire repo, `.git`, scratch scripts, or legacy credentials.

Root-owned `/etc/pulse-stream/staging.env` (mode0600) must contain ONLY the Preview
connector and these non-secret settings: `VERCEL_ENV=preview`,
`PULSE_STREAM_ENABLED=1`, `PULSE_STREAM_DAEMON_ENABLED=1`,
`PULSE_RUNTIME_RUN_ID`, `PULSE_MAPPING_VERSION`, approved
`PULSE_STAGING_PLAINTEXT_APPROVED=1`, and existing `PULSE_MEMORY_STOP_BYTES`.
Transfer the staging Redis connector through an approved secret channel, never
chat, Git, command history or logs. The service needs no worker secret.

The top350 release also requires `PULSE_RELEASE_TOP350=1`. Its ephemeral bus is
`ticks:v2`; old Preview deployments do not receive this bus. Use the Preview from
the same commit or later. The service deliberately stops with exit78 after an
upstream HTTP refusal; systemd will not automatically retry a possible ban.

Start only after identity gate/approval. Inspect `systemctl status pulse-stream`
and `journalctl -u pulse-stream`; verify one fenced owner, source events and
browser paint. Stop via `systemctl stop pulse-stream`. The lease expires naturally
within five seconds; do not delete keys. Do not run the HTTP stream producer while
the daemon owns the lease. Existing metric worker is separate and still needed.

Vercel deployment remains Git-triggered only. The separately authorised host
receives a minimal `git archive` containing package/lock, stream code and unit,
never the repository or AWS credentials. Node archive SHA256 verified against
the official release. Root-only0600 staging.env was transferred via host-key-pinned
SSH stdin, not argv, logs or cloud user-data. Necessary SSH keys are protected
locally; the service user cannot modify its root-owned source/configuration.
Latest real-browser and continuous-runtime evidence belongs in docs/evidence/.

## Fixed Preview cadence (ADR-0163)

`pulse-preview-driver` is enabled on the same host, installed from9e8c666. It uses
the separate non-login `pulse-driver` user and root-only `/etc/pulse-stream/driver.env`.
project owner explicitly approved the branch worker secret plus project-level bypass
token transfer for ONLY the fixed Preview worker/publisher. No Redis/AWS credentials
are in this driver's config. Do not print Environment or config contents in
diagnostics. Safe commands: `systemctl status pulse-preview-driver` and
`journalctl -u pulse-preview-driver`. Worker/minute, publisher/minute offset20s;
no same-lane overlap or same-cycle retries. Authentication/refusal stops exit78.
The deployment URL is pinned; updating it requires another Preview identity gate.
Do not also run the local driver. Stop with `systemctl stop pulse-preview-driver`.
