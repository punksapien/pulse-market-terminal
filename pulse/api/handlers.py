"""ADR-0035: HTTP transport functions with explicit storage and application callbacks."""
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pulse.api.http_utils import _as_bool, _first
from pulse.config import (ALLOWED_COMBINATIONS, REPORT_SCHEMA_VERSION, MODEL_VERSION,
    REPORT_REFRESH_AFTER, REPORT_MAX_STALE, GENERATION_REFRESH_AFTER)
from pulse.legacy.generation import ReportUnavailableError, _epoch_seconds


def _send_json(
    self, status, payload, headers=None, *, serialise,
):
    _serialise = serialise
    body = json.dumps(_serialise(payload), separators=(",", ":")).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Cache-Control", "no-store, max-age=0")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("X-Frame-Options", "DENY")
    self.send_header("X-XSS-Protection", "1; mode=block")
    for name, value in (headers or {}).items():
        self.send_header(str(name), str(value))
    self.end_headers()
    self.wfile.write(body)


def do_OPTIONS(
    self,
):
    self._send_json(204, {})


def _report_headers(
    report, heal_result=None,
):
    meta = report.get("meta", {}) if isinstance(report, dict) else {}
    headers = {
        "X-Volatility-Freshness": meta.get("freshness_status", "unknown"),
        "X-Volatility-Generation": meta.get("generation_id", "unknown"),
        "X-Volatility-Model-Age": meta.get("model_age_seconds", "unknown"),
    }
    if heal_result:
        headers["X-Volatility-Auto-Heal"] = heal_result.get("status", "attempted")
    if meta.get("freshness_status") == "stale_revalidating":
        headers["Warning"] = '110 - "Volatility model refresh in progress"'
    return headers


def do_GET(
    self, *, health_snapshot, authorise, storage, cache_state, shell, render,
):
    _generation_health_snapshot = health_snapshot
    _authorise_secret = authorise
    VercelStorage = storage
    _CACHE = cache_state
    _vol_signal_shell_template = shell
    vol_signal_template = render
    parsed = urlparse(self.path)
    path = parsed.path.rstrip('/')
    query = parse_qs(parsed.query)

    if path == "/api/health" or _as_bool(_first(query, "health", "0"), False):
        snapshot = _generation_health_snapshot(verify_reports=True)
        healthy = snapshot.get("healthy", False)
        local = os.environ.get("VERCEL") != "1"
        status = 200 if healthy or local else 503
        health_status = "ok" if healthy else (
            "degraded"
            if snapshot.get("freshness_status") == "stale_recoverable"
            else "critical"
        )
        self._send_json(status, {
            "status": health_status,
            "schema_version": REPORT_SCHEMA_VERSION,
            "model_version": MODEL_VERSION,
            "freshness_slo_seconds": REPORT_REFRESH_AFTER,
            "hard_stale_limit_seconds": REPORT_MAX_STALE,
            "watchdog_rebuild_after_seconds": GENERATION_REFRESH_AFTER,
            "deployment_git_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA"),
            **snapshot,
        })
        return

    if path in ("/api/telemetry", "/api/metrics"):
        self._send_json(405, {"error": "Method Not Allowed"})
        return

    try:
        wants_json = _as_bool(_first(query, "json", "0"), False)
        wants_signal = _as_bool(_first(query, "signal", "0"), False)
        wants_legacy = _as_bool(_first(query, "legacy", "0"), False)
        accepts_json = "application/json" in self.headers.get("Accept", "")
        is_cron = (
            path in ("/api/cron", "/api/cron-watchdog")
            or _as_bool(_first(query, "_cron", "0"), False)
        )

        # Route Cron requests immediately
        if is_cron:
            authorised, reason = _authorise_secret(
                self.headers,
                ("CRON_SECRET",),
            )
            if not authorised:
                status = 503 if reason == "missing_secret" else 401
                self._send_json(status, {
                    "status": "error",
                    "error": reason,
                })
                return
            force = _as_bool(_first(query, "force", "0"), False)
            user_agent = self.headers.get("User-Agent", "")
            trigger = (
                "vercel_cron"
                if "vercel-cron/1.0" in user_agent
                else "authenticated_cron"
            )
            result = self.warm_all_combinations(trigger=trigger, force=force)
            result_status = result.get("status")
            status = 200 if (
                result.get("published")
                or result_status == "fresh_noop"
            ) else (202 if result_status == "locked" else 500)
            self._send_json(status, result)
            return

        is_clear_cache = _as_bool(_first(query, "clear_cache", "0"), False)
        if is_clear_cache:
            authorised, reason = _authorise_secret(
                self.headers,
                ("ADMIN_SECRET", "CRON_SECRET"),
            )
            if not authorised:
                status = 503 if reason == "missing_secret" else 401
                self._send_json(status, {
                    "status": "error",
                    "error": reason,
                })
                return
            client = VercelStorage.get_client()
            if client:
                try:
                    deleted_count = 0
                    for pattern in (
                        "base_*",
                        "USDT_*",
                        "vol_lock_*",
                        "vol:v2:*",
                    ):
                        batch = []
                        for key in client.scan_iter(match=pattern, count=100):
                            batch.append(key)
                            if len(batch) >= 100:
                                deleted_count += client.delete(*batch)
                                batch = []
                        if batch:
                            deleted_count += client.delete(*batch)
                    with _CACHE["lock"]:
                        _CACHE["store"].clear()
                    self._send_json(200, {"status": "ok", "message": f"Successfully deleted {deleted_count} keys from Redis"})
                except Exception as e:
                    self._send_json(500, {"status": "error", "message": f"Failed to clear Redis cache: {e}"})
            else:
                self._send_json(200, {"status": "ok", "message": "Redis not available, no cache cleared"})
            return

        # ADR-0028: retired HTML must not trigger report retrieval or healing.
        if wants_legacy and not (wants_json or wants_signal or accepts_json):
            self._send_json(410, {"error": "legacy_dashboard_retired"})
            return

        if not (wants_json or wants_signal or wants_legacy or accepts_json):
            html = _vol_signal_shell_template()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        # Extract and validate combinations
        tf = str(_first(query, "timeframe", _first(query, "resolution", "60")))
        hz = str(_first(query, "horizon", _first(query, "scale_horizon", "1H"))).upper()

        # STRICT WHITELIST VALIDATION
        if (tf, hz) not in ALLOWED_COMBINATIONS:
            self._send_json(400, {
                "error": f"Invalid parameter combination: ({tf}, {hz})",
                "allowed_combinations": list(sorted(ALLOWED_COMBINATIONS))
            })
            return

        report, heal_result = self._get_report_with_auto_heal(query)
        response_headers = self._report_headers(report, heal_result)

        if wants_json or wants_signal or accepts_json:
            self._send_json(200, report, headers=response_headers)
        else:
            html = vol_signal_template(report)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
    except ReportUnavailableError as exc:
        retry_after = 60
        backoff = exc.details.get("backoff") if isinstance(exc.details, dict) else None
        if isinstance(backoff, dict):
            retry_at = _epoch_seconds(backoff.get("retry_after"))
            if retry_at:
                retry_after = max(1, int(retry_at - time.time()))
        self._send_json(503, {
            "status": "unavailable",
            "error": exc.reason,
            "retryable": True,
            "retry_after_seconds": retry_after,
            "details": exc.details,
            "schema_version": REPORT_SCHEMA_VERSION,
            "model_version": MODEL_VERSION,
        }, headers={"Retry-After": retry_after})
    except Exception as exc:
        self._send_json(500, {"error": str(exc), "framework": "vol_signal"})


def do_POST(
    self, *, sanitize, now_utc, storage,
):
    sanitize_utm_string = sanitize
    _now_utc = now_utc
    VercelStorage = storage
    parsed = urlparse(self.path)
    path = parsed.path.rstrip('/')

    if path in ("/api/telemetry", "/api/metrics"):
        try:
            # 1. Enforce strict content size limit (cybersecurity check against buffer exhaustion/DoS)
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0 or content_length > 4096:
                self._send_json(413, {"error": "Payload Too Large. Max size is 4KB."})
                return

            # 2. Extract payload securely
            body = self.rfile.read(content_length).decode("utf-8")

            # 3. Parse and check structure
            try:
                payload = json.loads(body)
            except Exception:
                self._send_json(400, {"error": "Malformed JSON structure."})
                return

            # 4. Strict Schema Whitelist and regex validation (neutralizes XSS & SQLi injections)
            allowed_schema = ("timestamp", "event", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
            sanitized = {}
            for key in allowed_schema:
                val = payload.get(key, "")
                sanitized[key] = sanitize_utm_string(val)

            if not sanitized["event"]:
                self._send_json(400, {"error": "Missing required field: event"})
                return

            # Timestamp syntax validation
            if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", sanitized["timestamp"]):
                sanitized["timestamp"] = _now_utc().isoformat()

            # 5. Persistent secure storage
            stored_payload = json.dumps(sanitized, separators=(",", ":"))

            if VercelStorage.is_available():
                # Multi-instance persistent Redis queue (capped list size keeping last 1000 logs)
                VercelStorage.rpush("volatility_utm_interactions", stored_payload)
                VercelStorage.ltrim("volatility_utm_interactions", -1000, -1)
            else:
                # Fallback to local secure temporary log partition
                tmp_log_path = Path(tempfile.gettempdir()) / "vol_utm_interactions.json"
                try:
                    logs = []
                    if tmp_log_path.exists():
                        logs = json.loads(tmp_log_path.read_text("utf-8"))
                    if not isinstance(logs, list):
                        logs = []
                    logs.append(sanitized)
                    if len(logs) > 1000:
                        logs = logs[-1000:]
                    tmp_log_path.write_text(json.dumps(logs, separators=(",", ":")), "utf-8")
                except Exception:
                    pass

            self._send_json(200, {"status": "ok", "message": "Telemetry logged securely"})
        except Exception as e:
            self._send_json(500, {"error": f"Internal Server Error: {str(e)}"})
    else:
        self._send_json(404, {"error": "Resource Not Found"})

