import json
import os
import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from api import index


def sample_report(resolution, horizon, generation_id="test-generation"):
    now = time.time()
    return {
        "meta": {
            "resolution": resolution,
            "scale_horizon": horizon,
            "target_seconds": index._parse_horizon_to_seconds(horizon),
            "generation_id": generation_id,
            "oi_coverage_ratio": 0.9,
            "last_rebuilt": now,
            "created_at_utc": index._now_utc().isoformat(),
        },
        "summary": [{"instrument": "B-BTC_USDT", "volatility_score": 0.01}],
        "instruments": {"B-BTC_USDT": {"instrument": "B-BTC_USDT"}},
        "errors": {},
    }


def generation_store(generation_id="generation-1", age_seconds=30, missing=None):
    published_at = time.time() - age_seconds
    combinations = []
    store = {
        index.ACTIVE_GENERATION_KEY: generation_id,
        index.LAST_GOOD_GENERATION_KEY: generation_id,
    }
    for resolution, horizon in index.UI_COMBINATIONS:
        cache_key = f"USDT_{resolution}_{horizon}_True_"
        report = sample_report(resolution, horizon, generation_id)
        report["meta"]["last_rebuilt"] = published_at
        combinations.append({
            "resolution": resolution,
            "horizon": horizon,
            "cache_key": cache_key,
            "summary_rows": len(report["summary"]),
            "error_count": 0,
        })
        if (resolution, horizon) != missing:
            store[index._generation_report_key(generation_id, cache_key)] = json.dumps(report)
    manifest = {
        "schema_version": index.REPORT_SCHEMA_VERSION,
        "model_version": index.MODEL_VERSION,
        "generation_id": generation_id,
        "published_at": published_at,
        "combinations": combinations,
    }
    store[index._generation_manifest_key(generation_id)] = json.dumps(manifest)
    return store


def large_report(resolution, horizon, count=100, generation_id="generation-new"):
    report = sample_report(resolution, horizon, generation_id)
    instruments = ["B-BTC_USDT", "B-ETH_USDT"] + [
        f"B-TEST{i}_USDT" for i in range(max(0, count - 2))
    ]
    report["summary"] = [
        {"instrument": instrument, "volatility_score": 0.01 + i * 0.00001}
        for i, instrument in enumerate(instruments)
    ]
    report["instruments"] = {
        instrument: {"instrument": instrument} for instrument in instruments
    }
    return report


class CacheReliabilityTests(unittest.TestCase):
    def test_tmp_cache_path_includes_complete_cache_identity(self):
        first = index._tmp_cache_path("USDT_60_1H_False_B-BTC_USDT")
        second = index._tmp_cache_path("USDT_60_1H_False_B-ETH_USDT")
        self.assertNotEqual(first, second)

    def test_generated_report_validation_accepts_fixed_ui_combinations(self):
        for resolution, horizon in index.UI_COMBINATIONS:
            valid, reason = index._validate_generated_report(
                sample_report(resolution, horizon),
                resolution,
                horizon,
            )
            self.assertTrue(valid, reason)

    def test_generated_report_validation_rejects_wrong_horizon(self):
        valid, reason = index._validate_generated_report(
            sample_report("60", "1H"),
            "60",
            "4H",
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "horizon_mismatch")

    def test_fixed_ui_fallback_reports_keep_expected_metadata(self):
        with patch.object(index.VercelStorage, "is_available", return_value=False):
            for resolution, horizon in index.UI_COMBINATIONS:
                index._CACHE["store"].clear()
                report = index._get_cached_or_compute({
                    "timeframe": [resolution],
                    "horizon": [horizon],
                    "all": ["1"],
                })
                self.assertEqual(str(report["meta"]["resolution"]), resolution)
                self.assertEqual(report["meta"]["scale_horizon"], horizon)

    def test_invalid_volatility_score_is_rejected(self):
        report = sample_report("60", "1H")
        report["summary"][0]["volatility_score"] = float("nan")
        valid, reason = index._validate_generated_report(report, "60", "1H")
        self.assertFalse(valid)
        self.assertEqual(reason, "invalid_volatility_score")


class RedisStorageTests(unittest.TestCase):
    def test_atomic_publish_supports_persistent_generation_pointers(self):
        class FakePipeline:
            def __init__(self):
                self.calls = []

            def set(self, key, value, **kwargs):
                self.calls.append((key, value, kwargs))
                return self

            def execute(self):
                return [True] * len(self.calls)

        class FakeClient:
            def __init__(self):
                self.pipe = FakePipeline()

            def pipeline(self, transaction=True):
                self.transaction = transaction
                return self.pipe

        client = FakeClient()
        with patch.object(index.VercelStorage, "get_client", return_value=client):
            written = index.VercelStorage.set_many_atomic({
                "persistent": ("generation", None),
                "retained": ({"ok": True}, 60),
            })
        self.assertTrue(written)
        self.assertEqual(client.pipe.calls[0][2], {})
        self.assertEqual(client.pipe.calls[1][2], {"ex": 60})
        self.assertTrue(client.transaction)


class SignalTransitionTests(unittest.TestCase):
    def test_report_attachment_is_immutable_and_requires_matching_displayed_state(self):
        report = sample_report("60", "1H")
        report["summary"][0].update({
            "trade_signal": "Range",
            "price_last": 101.0,
        })
        state = {
            "signal": "Range",
            "since_price": 99.5,
            "since_at": 123.0,
        }
        with patch.object(
            index.VercelStorage,
            "get_signal_states",
            return_value={"B-BTC_USDT": state},
        ):
            attached = index._attach_signal_states(report, "60", "1H")

        self.assertIsNot(attached, report)
        self.assertNotIn("signal_since_price", report["summary"][0])
        self.assertEqual(attached["summary"][0]["signal_since_price"], 99.5)
        self.assertEqual(attached["summary"][0]["signal_since_at"], 123.0)

        report["summary"][0]["trade_signal"] = "MomentumLong"
        with patch.object(
            index.VercelStorage,
            "get_signal_states",
            return_value={"B-BTC_USDT": state},
        ):
            mismatched = index._attach_signal_states(report, "60", "1H")
        self.assertNotIn("signal_since_price", mismatched["summary"][0])

    def test_only_user_facing_up_down_range_and_breakout_states_are_displayed(self):
        for signal in (
            "MomentumLong",
            "MomentumShort",
            "Range",
            "BreakoutWatch",
        ):
            fields = index._matching_signal_state_fields(
                {"signal": signal, "since_price": 12.5, "since_at": 123.0},
                signal,
            )
            self.assertEqual(fields["signal_since_price"], 12.5)

        for signal in ("ReversalLong", "ReversalShort", "Avoid"):
            self.assertIsNone(index._matching_signal_state_fields(
                {"signal": signal, "since_price": 12.5, "since_at": 123.0},
                signal,
            ))

    def test_redis_reconciliation_accepts_all_classifier_states_and_live_prices(self):
        class FakeClient:
            def __init__(self):
                self.call = None

            def eval(self, script, key_count, key, *args):
                self.call = (script, key_count, key, args)
                return [
                    b"1",
                    b"B-BTC_USDT",
                    json.dumps({
                        "signal": "Range",
                        "since_price": 101.25,
                        "since_at": 123.0,
                    }).encode("utf-8"),
                ]

        client = FakeClient()
        observations = [
            {"instrument": "B-BTC_USDT", "signal": "Range", "price": 101.25},
            {"instrument": "B-ETH_USDT", "signal": "ReversalLong", "price": 50.0},
        ]
        with patch.object(index.VercelStorage, "get_client", return_value=client):
            result = index.VercelStorage.reconcile_signal_states(
                index._signal_state_key("60", "1H"),
                observations,
                source_at=123.0,
                generation_id="generation-1",
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["states"]["B-BTC_USDT"]["since_price"], 101.25)
        script, key_count, key, args = client.call
        self.assertEqual(key_count, 1)
        self.assertEqual(key, "vol:v2:signal-state:60:1H")
        self.assertIn("current_source", script)
        self.assertIn("source_at > current_source", script)
        baseline_block = script.split("if not state then", 1)[1].split(
            "elseif state['signal'] ~= signal then",
            1,
        )[0]
        transition_block = script.split(
            "elseif state['signal'] ~= signal then",
            1,
        )[1].split("end\n  end", 1)[0]
        self.assertNotIn("since_price", baseline_block)
        self.assertIn("since_price", transition_block)
        self.assertIn("BreakoutWatch", transition_block)
        self.assertIn("Range", transition_block)
        self.assertIn("B-ETH_USDT", args)
        self.assertIn("ReversalLong", args)

    def test_auxiliary_redis_failure_does_not_poison_primary_connection_state(self):
        class FailingClient:
            @staticmethod
            def eval(*_args):
                raise RuntimeError("transition hash unavailable")

        with patch.object(
            index.VercelStorage,
            "get_client",
            return_value=FailingClient(),
        ), patch.object(
            index.VercelStorage,
            "_connection_failed",
        ) as connection_failed:
            result = index.VercelStorage.reconcile_signal_states(
                index._signal_state_key("60", "1H"),
                [{
                    "instrument": "B-BTC_USDT",
                    "signal": "Range",
                    "price": 101.25,
                }],
                source_at=123.0,
                generation_id="generation-1",
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "redis_error")
        connection_failed.assert_not_called()

    def test_fast_hydrate_uses_live_price_and_returns_transition_fields(self):
        base = sample_report("60", "1H")
        base["summary"][0].update({
            "trade_signal": "Range",
            "price_last": 100.0,
        })

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"prices": {"B-BTC_USDT": {"ls": "101.25"}}}

        def classify(token, _target_secs, _cfg):
            token["trade_signal"] = "BreakoutWatch"

        reconciliation = {
            "accepted": True,
            "reason": "accepted",
            "states": {
                "B-BTC_USDT": {
                    "signal": "BreakoutWatch",
                    "since_price": 101.25,
                    "since_at": 123.0,
                }
            },
        }
        with patch.object(
            index,
            "_resolve_target_report",
            return_value=(base, {"source": "test"}),
        ), patch.object(
            index.requests,
            "get",
            return_value=Response(),
        ), patch(
            "pulse.legacy.signal_engine.enrich_token",
            side_effect=classify,
        ), patch.object(
            index.VercelStorage,
            "reconcile_signal_states",
            return_value=reconciliation,
        ) as reconcile:
            result = index._fast_hydrate(
                {},
                "USDT_60_1H_True_",
                "60",
                "1H",
            )

        delta = result["delta"]["B-BTC_USDT"]
        self.assertEqual(delta["trade_signal"], "BreakoutWatch")
        self.assertEqual(delta["signal_since_price"], 101.25)
        self.assertEqual(delta["signal_since_at"], 123.0)
        observation = reconcile.call_args.args[1][0]
        self.assertEqual(observation["price"], 101.25)
        self.assertEqual(observation["signal"], "BreakoutWatch")

    def test_superseded_fast_snapshot_never_reaches_browser(self):
        base = sample_report("60", "1H")
        base["summary"][0].update({
            "trade_signal": "Range",
            "price_last": 100.0,
        })

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"prices": {"B-BTC_USDT": {"ls": "101.25"}}}

        def classify(token, _target_secs, _cfg):
            token["trade_signal"] = "MomentumLong"

        with patch.object(
            index,
            "_resolve_target_report",
            return_value=(base, {"source": "test"}),
        ), patch.object(
            index.requests,
            "get",
            return_value=Response(),
        ), patch(
            "pulse.legacy.signal_engine.enrich_token",
            side_effect=classify,
        ), patch.object(
            index.VercelStorage,
            "reconcile_signal_states",
            return_value={
                "accepted": False,
                "states": {},
                "reason": "superseded",
            },
        ):
            result = index._fast_hydrate(
                {},
                "USDT_60_1H_True_",
                "60",
                "1H",
            )
        self.assertEqual(result["delta"], {})


class GenerationReadTests(unittest.TestCase):
    def _redis_patches(self, store):
        return (
            patch.object(index.VercelStorage, "is_available", return_value=True),
            patch.object(index.VercelStorage, "get", side_effect=lambda key: store.get(key)),
        )

    def test_reader_uses_generation_payload_after_direct_key_expiry(self):
        store = generation_store()
        available, get_value = self._redis_patches(store)
        with available, get_value, patch.object(index, "_save_to_local_cache_layers"):
            report, provenance = index._resolve_target_report(
                "USDT_60_1H_True_", "60", "1H"
            )
        self.assertIsNotNone(report)
        self.assertEqual(provenance["source"], "active_generation")
        self.assertEqual(report["meta"]["generation_id"], "generation-1")

    def test_reader_repairs_missing_active_pointer_from_last_good(self):
        store = generation_store()
        store[index.ACTIVE_GENERATION_KEY] = None
        available, get_value = self._redis_patches(store)
        with available, get_value, \
                patch.object(index.VercelStorage, "set_many_atomic", return_value=True) as repair, \
                patch.object(index, "_save_to_local_cache_layers"):
            report, provenance = index._resolve_target_report(
                "USDT_60_1H_True_", "60", "1H"
            )
        self.assertIsNotNone(report)
        self.assertEqual(provenance["source"], "recovered_generation")
        repaired = repair.call_args.args[0]
        self.assertEqual(
            repaired[index.ACTIVE_GENERATION_KEY],
            ("generation-1", None),
        )

    def test_production_rejects_generation_beyond_hard_stale_limit(self):
        store = generation_store(age_seconds=index.REPORT_MAX_STALE + 1)
        available, get_value = self._redis_patches(store)
        with available, get_value, \
                patch.object(index, "load_precomputed_report", return_value=None), \
                patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
            report, diagnostics = index._resolve_target_report(
                "USDT_60_1H_True_", "60", "1H", allow_stale=True
            )
        self.assertIsNone(report)
        self.assertEqual(diagnostics["reason"], "no_acceptable_generation")

    def test_health_detects_missing_generation_member(self):
        store = generation_store(missing=("240", "4H"))
        available, get_value = self._redis_patches(store)
        with available, get_value:
            health = index._generation_health_snapshot(verify_reports=True)
        self.assertFalse(health["healthy"])
        self.assertFalse(health["complete"])
        self.assertIn("240/4H:report_not_object", health["reasons"])


class DeploymentConfigurationTests(unittest.TestCase):
    def test_legacy_entrypoint_is_statically_discoverable(self):
        import ast
        source = (Path(__file__).resolve().parents[1] / "api/index.py").read_text("utf-8")
        declarations = [node for node in ast.parse(source).body
                        if isinstance(node, ast.ClassDef) and node.name == "handler"]
        self.assertEqual(len(declarations), 1)
        self.assertTrue(any(isinstance(base, ast.Name) and base.id == "BaseHTTPRequestHandler"
                            for base in declarations[0].bases))

    def test_pulse_region_override_preserves_legacy_default(self):
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "vercel.json").read_text("utf-8")
        )
        # explicit per-file entries: a glob plus a second entry for the worker's duration would be ambiguous
        self.assertEqual(config["functions"]["api/pulse.py"]["regions"], ["bom1"])
        self.assertEqual(config["functions"]["api/pulse_worker.py"]["regions"], ["bom1"])
        self.assertEqual(config["functions"]["api/pulse_worker.py"]["maxDuration"], 90)
        self.assertNotIn("api/pulse*.py", config["functions"])
        self.assertEqual(config["regions"], ["bom1"])
        self.assertNotIn("api/index.py",config["functions"])

    def test_dedicated_project_does_not_redirect_to_legacy_playbook(self):
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "vercel.json").read_text("utf-8")
        )
        rules = [item for item in config["redirects"]
                 if item["destination"] == "/playbook.pdf"]
        self.assertFalse(rules,"dedicated Pulse domain does not publish the legacy playbook")

    def test_cron_uses_dedicated_query_free_path_and_retry_watchdog(self):
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "vercel.json").read_text("utf-8")
        )
        self.assertEqual({c["path"] for c in config["crons"]},
            {"/api/pulse_worker","/api/pulse_publish","/api/pulse_history"})
        self.assertIn({"path":"/api/pulse_worker","schedule":"* * * * *"},config["crons"])
        self.assertEqual(len({c["path"] for c in config["crons"]}),len(config["crons"]))
        rewrites = {item["source"]: item["destination"] for item in config["rewrites"]}
        self.assertNotIn("/api/cron",rewrites)
        ignore_rules = (
            Path(__file__).resolve().parents[1] / ".vercelignore"
        ).read_text("utf-8").splitlines()
        self.assertIn("!vercel.json", ignore_rules)
        python_version = (
            Path(__file__).resolve().parents[1] / ".python-version"
        ).read_text("utf-8").strip()
        self.assertEqual(python_version, "3.12")


class GenerationQualityTests(unittest.TestCase):
    def test_complete_current_generation_passes_quality_gate(self):
        generated = [
            (
                resolution,
                horizon,
                f"USDT_{resolution}_{horizon}_True_",
                large_report(resolution, horizon),
            )
            for resolution, horizon in index.UI_COMBINATIONS
        ]
        valid, reason = index._validate_generation_set(generated)
        self.assertTrue(valid, reason)

    def test_short_history_exclusions_do_not_count_as_upstream_failures(self):
        generated = []
        for resolution, horizon in index.UI_COMBINATIONS:
            report = large_report(resolution, horizon, count=429)
            if resolution == "1D":
                report["errors"] = {
                    f"B-NEW{i}_USDT": "insufficient bars: 42"
                    for i in range(53)
                }
            generated.append((
                resolution,
                horizon,
                f"USDT_{resolution}_{horizon}_True_",
                report,
            ))
        valid, reason = index._validate_generation_set(generated)
        self.assertTrue(valid, reason)

    def test_unexpected_upstream_error_ratio_still_blocks_publish(self):
        generated = []
        for resolution, horizon in index.UI_COMBINATIONS:
            report = large_report(resolution, horizon)
            if resolution == "1D":
                report["errors"] = {
                    f"B-BROKEN{i}_USDT": "candlestick request failed: timeout"
                    for i in range(12)
                }
            generated.append((
                resolution,
                horizon,
                f"USDT_{resolution}_{horizon}_True_",
                report,
            ))
        valid, reason = index._validate_generation_set(generated)
        self.assertFalse(valid)
        self.assertIn("unexpected_error_ratio", reason)

    def test_row_count_regression_blocks_atomic_publish(self):
        generated = [
            (
                resolution,
                horizon,
                f"USDT_{resolution}_{horizon}_True_",
                large_report(resolution, horizon),
            )
            for resolution, horizon in index.UI_COMBINATIONS
        ]
        previous = {
            "combinations": [
                {
                    "resolution": resolution,
                    "horizon": horizon,
                    "summary_rows": 200,
                }
                for resolution, horizon in index.UI_COMBINATIONS
            ]
        }
        valid, reason = index._validate_generation_set(
            generated,
            previous_manifest=previous,
        )
        self.assertFalse(valid)
        self.assertIn("row_count_regression", reason)

    def test_old_model_timestamp_blocks_relabeling_fallback_as_fresh(self):
        generated = []
        for resolution, horizon in index.UI_COMBINATIONS:
            report = large_report(resolution, horizon)
            report["meta"]["last_rebuilt"] = time.time() - 3600
            generated.append((
                resolution,
                horizon,
                f"USDT_{resolution}_{horizon}_True_",
                report,
            ))
        valid, reason = index._validate_generation_set(generated)
        self.assertFalse(valid)
        self.assertIn("model_age", reason)


class AutoHealTests(unittest.TestCase):
    def setUp(self):
        self.instance = index.handler.__new__(index.handler)
        self.query = {
            "timeframe": ["60"],
            "horizon": ["1H"],
            "json": ["1"],
        }

    @patch.object(index, "_refresh_backoff_status", return_value=None)
    @patch.object(index.handler, "warm_all_combinations", return_value={
        "status": "ok",
        "published": True,
        "generation_id": "healed",
    })
    @patch.object(index, "_get_cached_or_compute")
    def test_request_recovers_after_successful_single_auto_heal(
        self,
        get_report,
        _warm,
        _backoff,
    ):
        fresh = sample_report("60", "1H", "healed")
        fresh["meta"]["freshness_status"] = "fresh"
        get_report.side_effect = [
            index.ReportUnavailableError("missing", {"reason": "missing"}),
            fresh,
        ]
        report, heal = self.instance._get_report_with_auto_heal(self.query)
        self.assertEqual(report["meta"]["generation_id"], "healed")
        self.assertEqual(report["meta"]["auto_heal"]["status"], "recovered")
        self.assertTrue(heal["published"])

    @patch.object(index, "_refresh_backoff_status", return_value=None)
    @patch.object(index.handler, "warm_all_combinations", return_value={
        "status": "locked",
        "published": False,
    })
    @patch.object(index, "_get_cached_or_compute")
    def test_lock_contender_can_only_receive_bounded_stale_report(
        self,
        get_report,
        _warm,
        _backoff,
    ):
        stale = sample_report("60", "1H", "previous")
        stale["meta"]["freshness_status"] = "stale_revalidating"
        get_report.side_effect = [
            index.ReportUnavailableError("overdue", {}),
            stale,
        ]
        report, heal = self.instance._get_report_with_auto_heal(self.query)
        self.assertEqual(report["meta"]["freshness_status"], "stale_revalidating")
        self.assertEqual(heal["status"], "locked")
        self.assertEqual(get_report.call_args_list[1].args[0]["_allow_stale"], ["1"])

    @patch.object(index, "_refresh_backoff_status", return_value=None)
    @patch.object(index.handler, "warm_all_combinations", return_value={
        "status": "generation_failed",
        "published": False,
    })
    @patch.object(index, "_get_cached_or_compute")
    def test_hard_stale_data_is_not_returned_when_repair_fails(
        self,
        get_report,
        _warm,
        _backoff,
    ):
        get_report.side_effect = [
            index.ReportUnavailableError("expired", {"age": 10801}),
            index.ReportUnavailableError("expired", {"age": 10801}),
        ]
        with self.assertRaises(index.ReportUnavailableError) as caught:
            self.instance._get_report_with_auto_heal(self.query)
        self.assertEqual(
            caught.exception.reason,
            "volatility_generation_repair_failed",
        )


class AuthorizationTests(unittest.TestCase):
    def test_local_development_can_run_without_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            authorised, _ = index._authorise_secret({}, ("CRON_SECRET",))
        self.assertTrue(authorised)

    def test_vercel_fails_closed_when_cron_secret_is_missing(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
            authorised, reason = index._authorise_secret(
                {},
                ("CRON_SECRET",),
            )
        self.assertFalse(authorised)
        self.assertEqual(reason, "missing_secret")

    def test_bearer_secret_must_match(self):
        with patch.dict(os.environ, {"CRON_SECRET": "expected"}, clear=True):
            authorised, _ = index._authorise_secret(
                {"Authorization": "Bearer expected"},
                ("CRON_SECRET",),
            )
            rejected, reason = index._authorise_secret(
                {"Authorization": "Bearer wrong"},
                ("CRON_SECRET",),
            )
        self.assertTrue(authorised)
        self.assertFalse(rejected)
        self.assertEqual(reason, "invalid_secret")


class GenerationPublicationTests(unittest.TestCase):
    def setUp(self):
        self.generated = [
            (
                resolution,
                horizon,
                f"USDT_{resolution}_{horizon}_True_",
                sample_report(resolution, horizon),
            )
            for resolution, horizon in index.UI_COMBINATIONS
        ]

    @patch.object(index, "_save_to_local_cache_layers")
    @patch.object(index.VercelStorage, "set_many_atomic", return_value=True)
    @patch.object(index.VercelStorage, "get", return_value="previous")
    @patch.object(index.VercelStorage, "is_available", return_value=True)
    def test_generation_is_published_in_one_atomic_batch(
        self,
        _available,
        _get,
        set_many,
        save_local,
    ):
        published, manifest, reason = index._publish_generation(
            "next",
            self.generated,
        )
        self.assertTrue(published)
        self.assertEqual(reason, "published")
        self.assertEqual(manifest["generation_id"], "next")
        entries = set_many.call_args.args[0]
        self.assertIn(index.ACTIVE_GENERATION_KEY, entries)
        self.assertIn(index.LAST_GOOD_GENERATION_KEY, entries)
        self.assertIn(index.PREVIOUS_GENERATION_KEY, entries)
        self.assertIsNone(entries[index.ACTIVE_GENERATION_KEY][1])
        self.assertIsNone(entries[index.LAST_GOOD_GENERATION_KEY][1])
        for _, _, cache_key, _ in self.generated:
            self.assertIn(cache_key, entries)
            self.assertIn(
                f"{index.GENERATION_KEY_PREFIX}:next:{cache_key}",
                entries,
            )
        self.assertEqual(save_local.call_count, len(self.generated) + 1)

    @patch.object(index, "_save_to_local_cache_layers")
    @patch.object(
        index,
        "_reconcile_report_signal_states",
        return_value={"accepted": True, "states": {}, "reason": "accepted"},
    )
    @patch.object(index.VercelStorage, "set_many_atomic", return_value=True)
    @patch.object(index.VercelStorage, "get", return_value="previous")
    @patch.object(index.VercelStorage, "is_available", return_value=True)
    def test_published_generation_reconciles_every_status_snapshot_with_pruning(
        self,
        _available,
        _get,
        _set_many,
        reconcile,
        _save_local,
    ):
        hydrated_at = time.time() - 5
        for _, _, _, report in self.generated:
            report["meta"]["last_hydrated"] = hydrated_at
            report["summary"][0].update({
                "trade_signal": "Range",
                "price_last": 101.25,
            })

        published, manifest, _ = index._publish_generation(
            "next",
            self.generated,
        )

        self.assertTrue(published)
        self.assertEqual(reconcile.call_count, len(index.UI_COMBINATIONS))
        for call, (resolution, horizon) in zip(
            reconcile.call_args_list,
            index.UI_COMBINATIONS,
        ):
            self.assertEqual(call.args[1:3], (resolution, horizon))
            self.assertEqual(call.kwargs["generation_id"], "next")
            self.assertEqual(
                call.kwargs["source_at"],
                hydrated_at,
            )
            self.assertTrue(call.kwargs["prune"])

    @patch.object(index, "_save_to_local_cache_layers")
    @patch.object(
        index,
        "_reconcile_report_signal_states",
        side_effect=RuntimeError("auxiliary state failure"),
    )
    @patch.object(index.VercelStorage, "set_many_atomic", return_value=True)
    @patch.object(index.VercelStorage, "get", return_value="previous")
    @patch.object(index.VercelStorage, "is_available", return_value=True)
    def test_auxiliary_status_failure_cannot_fail_primary_publication(
        self,
        _available,
        _get,
        _set_many,
        reconcile,
        save_local,
    ):
        published, manifest, reason = index._publish_generation(
            "next",
            self.generated,
        )

        self.assertTrue(published)
        self.assertEqual(reason, "published")
        self.assertEqual(manifest["generation_id"], "next")
        self.assertEqual(save_local.call_count, len(self.generated) + 1)
        reconcile.assert_called_once()

    @patch.object(index, "_save_to_local_cache_layers")
    @patch.object(index.VercelStorage, "set_many_atomic", return_value=False)
    @patch.object(index.VercelStorage, "get", return_value="previous")
    @patch.object(index.VercelStorage, "is_available", return_value=True)
    def test_failed_atomic_publish_does_not_replace_local_generation(
        self,
        _available,
        _get,
        _set_many,
        save_local,
    ):
        published, _, reason = index._publish_generation(
            "next",
            self.generated,
        )
        self.assertFalse(published)
        self.assertEqual(reason, "redis_atomic_publish_failed")
        save_local.assert_not_called()

    @patch.object(index.VercelStorage, "delete_if_value")
    @patch.object(index.VercelStorage, "setnx", return_value=True)
    @patch.object(index.VercelStorage, "is_available", return_value=True)
    @patch.object(index, "_generation_health_snapshot", return_value={
        "complete": False,
        "generation_age_seconds": None,
        "max_model_age_seconds": None,
        "manifest": None,
    })
    @patch.object(index, "_validate_generation_set", return_value=(True, "ok"))
    @patch.object(index, "_publish_generation")
    @patch.object(index, "_get_cached_or_compute")
    def test_warmer_only_builds_fixed_ui_combinations(
        self,
        get_report,
        publish,
        _validate_set,
        _health,
        _available,
        _setnx,
        _delete,
    ):
        def build(query):
            return sample_report(
                query["timeframe"][0],
                query["horizon"][0],
                query["_generation_id"][0],
            )

        get_report.side_effect = build
        publish.return_value = (
            True,
            {"generation_id": "generated"},
            "published",
        )
        result = index.handler.warm_all_combinations(None)
        self.assertTrue(result["published"])
        requested = [
            (
                call.args[0]["timeframe"][0],
                call.args[0]["horizon"][0],
            )
            for call in get_report.call_args_list
        ]
        self.assertEqual(requested, list(index.UI_COMBINATIONS))

    def test_watchdog_noops_when_complete_generation_is_young(self):
        health = {
            "complete": True,
            "generation_age_seconds": 120,
            "max_model_age_seconds": 125,
            "selected_generation": "current",
            "pointer_source": "active",
            "combinations": [],
        }
        with patch.object(index.VercelStorage, "is_available", return_value=True), \
                patch.object(index, "_generation_health_snapshot", return_value=health), \
                patch.object(index.VercelStorage, "setnx") as setnx:
            result = index.handler.warm_all_combinations(None)
        self.assertEqual(result["status"], "fresh_noop")
        self.assertTrue(result["skipped"])
        setnx.assert_not_called()

    def test_vercel_fails_fast_when_redis_is_unavailable(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True), \
                patch.object(index.VercelStorage, "is_available", return_value=False), \
                patch.object(index, "_get_cached_or_compute") as compute:
            result = index.handler.warm_all_combinations(None)
        self.assertEqual(result["status"], "redis_unavailable")
        compute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
