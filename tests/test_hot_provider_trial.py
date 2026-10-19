from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from mb_market_data.hot_provider_trial import (
    DeadlineStop, MeasuredClient, RejectInteractiveAuthorization, SafeResponse,
    SyntheticClient, SyntheticResponse, TrialSettings, phase_start,
    prepare_schwab_client, run_trial, timing, validate_output_path,
)
from mb_market_data.sampling_acquisition import IsolatedSamplingPoller


class Clock:
    def __init__(self):
        self.value = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    def __call__(self): return self.value
    def wait(self, seconds): self.value += timedelta(seconds=seconds)


def settings(**kwargs):
    return replace(TrialSettings(("AAPL", "MSFT", "SPY"), (2, 4), 4, 3, 3, "synthetic"), **kwargs)


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.clock = Clock()
        clock = self.clock
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls, tz=None): return clock().astimezone(tz or timezone.utc)
        p = patch("mb_market_data.schwab_quotes.datetime", LogicalDatetime)
        p.start(); self.addCleanup(p.stop)

    def run_proof(self, client=None, **kwargs):
        return run_trial(self.folder / "trial", kwargs.pop("settings", settings()), client or SyntheticClient(),
            clock=self.clock, wait=self.clock.wait, clock_mode="logical_offline", on_status=lambda _: None, **kwargs)

    def test_invalid_settings_are_rejected(self):
        for kwargs in ({"symbols": ()}, {"symbols": ("aapl",)}, {"symbols": ("AAPL", "AAPL")},
                       {"symbols": ["AAPL"]}, {"intervals": (1,)}, {"intervals": (2,2)},
                       {"intervals": (True,)}, {"phase_seconds": 5}, {"phase_seconds": 604},
                       {"phase_seconds": True}, {"quote_call_budget": 2}, {"quote_call_budget": 601},
                       {"request_timeout_seconds": 0}, {"request_timeout_seconds": True}, {"source": "auto"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): settings(**kwargs)

    def test_midnight_guard_precedes_output_creation(self):
        self.clock.value = self.clock().astimezone(timezone(timedelta(hours=-4))).replace(hour=23, minute=59, second=59)
        with self.assertRaises(ValueError): self.run_proof()
        self.assertFalse((self.folder / "trial").exists())

    def test_production_paths_and_existing_output_are_preserved(self):
        with self.assertRaises(ValueError): validate_output_path(self.folder)
        for name in ("quote_observation_journal_v2_api_opening_2026-10-19", "QUOTE_OBSERVATION_JOURNAL"):
            with self.assertRaises(ValueError): validate_output_path(self.folder / name / "new")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_deadline_bounds_retry_waits(self):
        event = Event()
        end = self.clock() + timedelta(seconds=2)
        stop = DeadlineStop(end, event, clock=self.clock, wait=self.clock.wait)
        self.assertTrue(stop.wait(999))
        self.assertEqual(self.clock(), end)
        self.assertFalse(event.is_set())

    def test_external_stop_is_immediate(self):
        event = Event(); event.set()
        stop = DeadlineStop(self.clock()+timedelta(hours=1), event, clock=self.clock, wait=self.clock.wait)
        before = self.clock(); self.assertTrue(stop.wait(50)); self.assertEqual(self.clock(), before)

    def test_phase_start_is_future_and_on_exact_grid(self):
        for interval in (2, 3, 4):
            now = self.clock() + timedelta(microseconds=999999)
            start = phase_start(now, interval)
            self.assertGreaterEqual((start-now).total_seconds(), 2)
            self.assertEqual((start.second % interval, start.microsecond), (0, 0))

    def test_metrics_use_nearest_rank_and_do_not_sum_nested_costs(self):
        self.assertEqual(timing([])["count"], 0)
        self.assertEqual(timing([.01,.03,.02]), {"count":3,"mean_ms":20.0,"p95_ms":30.0,"max_ms":30.0})

    def test_native_collector_journal_audit_and_replay_for_both_phases(self):
        client = SyntheticClient(); report = self.run_proof(client)
        self.assertTrue(report["passed"], report)
        self.assertEqual(client.count, 3)
        self.assertEqual([p["channel_audit"]["acquisition_count"] for p in report["phases"]], [2,1])
        self.assertEqual([p["channel_audit"]["observation_count"] for p in report["phases"]], [6,3])
        self.assertTrue(all(p["repeated_replay_identical"] and not p["pending_dispatch"] for p in report["phases"]))
        for p in report["phases"]:
            self.assertEqual(p["request_latency"]["count"], p["planned_slots"])
            self.assertEqual(p["channel_audit"]["missing_slot_ids"], [])
        self.assertEqual(json.loads((self.folder/"trial"/"manifest.json").read_text())["quote_client_calls"], 3)
        self.assertEqual(len(report["source_sha256"]), 2)

    def test_slow_response_leaves_gap_and_never_reissues_missed_slot(self):
        client = SyntheticClient()
        original = client.quotes
        def slow(symbols, *, fields):
            self.clock.wait(3)
            return original(symbols, fields=fields)
        client.quotes = slow
        report = self.run_proof(client, settings=settings(intervals=(2,), quote_call_budget=2))
        self.assertFalse(report["passed"])
        self.assertEqual(client.count, 1)
        phase = report["phases"][0]
        self.assertEqual(len(phase["channel_audit"]["missing_slot_ids"]), 1)
        self.assertTrue(phase["repeated_replay_identical"])

    def test_interrupt_retains_unknown_dispatch_without_another_call(self):
        client = Mock()
        client.quotes.side_effect = KeyboardInterrupt()
        report = self.run_proof(client)
        self.assertTrue(report["interrupted"])
        self.assertFalse(report["passed"])
        self.assertEqual(client.quotes.call_count, 1)
        self.assertTrue(report["phases"][0]["pending_dispatch"])
        self.assertEqual(report["phases"][0]["replay"]["acquisitions"], 0)

    def test_persistent_sqlite_retry_ends_at_deadline(self):
        with patch.object(IsolatedSamplingPoller, "recover", side_effect=sqlite3.OperationalError("locked")):
            report = self.run_proof(settings=settings(intervals=(2,), quote_call_budget=2))
        self.assertFalse(report["passed"])
        self.assertEqual(report["quote_client_calls"], 0)
        phase = report["phases"][0]
        self.assertEqual(phase["finished_at_utc"], phase["end_at_utc"])
        self.assertEqual(len(phase["channel_audit"]["missing_slot_ids"]), 2)

    def test_http_auth_failure_is_recorded_and_stops_future_phases(self):
        client = Mock()
        response = Mock(status_code=401, ok=False, text="secret-token", json=Mock(return_value={"secret":"token"}))
        client.quotes.return_value = response
        report = self.run_proof(client)
        self.assertEqual(report["halt_reason"], "authorization_required")
        self.assertEqual(client.quotes.call_count, 1)
        self.assertEqual(report["phases"][0]["channel_audit"]["status_counts"], {"request_error":3})
        self.assertFalse(response.json.called)
        for path in (self.folder/"trial").rglob("*"):
            if path.is_file(): self.assertNotIn(b"secret-token", path.read_bytes())

    def test_transport_failure_is_explicit_and_sanitized(self):
        client = Mock(); client.quotes.side_effect = OSError("password=do-not-save")
        report = self.run_proof(client, settings=settings(intervals=(2,), quote_call_budget=2))
        self.assertTrue(report["complete_coverage"])
        self.assertFalse(report["clean_transport"])
        self.assertEqual(report["phases"][0]["channel_audit"]["status_counts"], {"request_error":6})
        for path in (self.folder/"trial").rglob("*"):
            if path.is_file(): self.assertNotIn(b"do-not-save", path.read_bytes())

    def test_budget_and_deadline_block_before_underlying_client(self):
        client = SyntheticClient()
        wrapper = MeasuredClient(client, budget=1, clock=self.clock)
        wrapper.deadline = self.clock()+timedelta(seconds=1)
        wrapper.quotes(["SPY"],fields="all")
        with self.assertRaises(RuntimeError): wrapper.quotes(["SPY"],fields="all")
        self.assertEqual(client.count, 1)
        wrapper = MeasuredClient(client, budget=3, clock=self.clock)
        wrapper.deadline = self.clock()
        with self.assertRaises(RuntimeError): wrapper.quotes(["SPY"],fields="all")
        self.assertEqual(client.count, 1)

    def test_auth_callback_request_blocks_before_underlying_client(self):
        reject = RejectInteractiveAuthorization(); self.assertEqual(reject("secret-url"), "")
        client = Mock(); wrapper = MeasuredClient(client, budget=1, clock=self.clock, auth_rejection=reject)
        with self.assertRaises(RuntimeError): wrapper.quotes(["SPY"],fields="all")
        self.assertFalse(client.quotes.called)
        self.assertFalse(hasattr(reject, "url"))

    def test_decode_errors_do_not_preserve_exception_secrets(self):
        response = Mock(status_code=200, ok=True)
        response.json.side_effect = ValueError("secret-password")
        metric = {}; safe = SafeResponse(response, metric)
        with self.assertRaisesRegex(RuntimeError, r"Quote JSON decode failed \(ValueError\)") as caught: safe.json()
        self.assertNotIn("secret-password", str(caught.exception))
        self.assertEqual(metric["decode_error_type"], "ValueError")


class CredentialAndCliTests(unittest.TestCase):
    def test_secure_client_uses_existing_interface_timeout_and_reject_callback(self):
        secure = types.ModuleType("mb_tools.schwab_secure")
        secure.DEFAULT_POLLING_REFRESH_MARGIN = timedelta(minutes=76)
        secure.load_secure_schwab_config = Mock(return_value=object())
        status = Mock(); secure.read_schwab_token_status = Mock(return_value=status)
        client = Mock(); secure.make_client_from_config = Mock(return_value=client)
        with patch.dict(sys.modules, {"mb_tools":types.ModuleType("mb_tools"), "mb_tools.schwab_secure":secure}):
            result, reject = prepare_schwab_client("config.ecfg", "input-secret", settings(source="schwab"), clock=Clock())
        self.assertIs(result, client)
        kwargs = secure.make_client_from_config.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 3)
        self.assertIs(kwargs["call_on_auth"], reject)
        self.assertEqual(status.require_refresh_valid_through.call_count, 2)

    def test_authorization_request_closes_client_before_trial(self):
        secure = types.ModuleType("mb_tools.schwab_secure")
        secure.DEFAULT_POLLING_REFRESH_MARGIN = timedelta(minutes=76)
        secure.load_secure_schwab_config = Mock()
        secure.read_schwab_token_status = Mock()
        client = Mock()
        def make(config, **kw): kw["call_on_auth"]("private-url"); return client
        secure.make_client_from_config = make
        with patch.dict(sys.modules, {"mb_tools":types.ModuleType("mb_tools"), "mb_tools.schwab_secure":secure}):
            with self.assertRaises(RuntimeError): prepare_schwab_client("config", "secret", settings(source="schwab"), clock=Clock())
        client.close.assert_called_once()

    def test_expired_refresh_never_constructs_client(self):
        secure = types.ModuleType("mb_tools.schwab_secure")
        secure.DEFAULT_POLLING_REFRESH_MARGIN = timedelta(minutes=76)
        secure.load_secure_schwab_config = Mock()
        status = Mock(); status.require_refresh_valid_through.side_effect=RuntimeError("expired")
        secure.read_schwab_token_status = Mock(return_value=status)
        secure.make_client_from_config = Mock()
        with patch.dict(sys.modules, {"mb_tools":types.ModuleType("mb_tools"), "mb_tools.schwab_secure":secure}):
            with self.assertRaises(RuntimeError): prepare_schwab_client("config", "secret", settings(source="schwab"), clock=Clock())
        secure.make_client_from_config.assert_not_called()

    def probe(self):
        path = Path(__file__).parents[1]/"probes"/"probe_hot_quote_trial.py"
        spec = importlib.util.spec_from_file_location("trial_probe", path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_cli_requires_explicit_source_before_password_prompt(self):
        probe = self.probe()
        with patch.object(probe.getpass,"getpass") as password:
            with self.assertRaises(SystemExit): probe.main([])
        password.assert_not_called()

    def test_failed_preflight_does_not_create_output_or_echo_secret(self):
        from contextlib import redirect_stdout
        import io
        probe = self.probe()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = root/"config.ecfg"; config.write_text("encrypted-placeholder")
            args = ["--schwab","--symbols","SPY","--intervals","2","--phase-seconds","4",
                    "--quote-call-budget","2","--request-timeout-seconds","3","--output-root",str(root/"results"),"--ecfg",str(config)]
            out = io.StringIO()
            with patch.object(probe.getpass,"getpass",return_value="private-password"), patch.object(probe,"prepare_schwab_client",side_effect=RuntimeError("private-password")), redirect_stdout(out):
                self.assertEqual(probe.main(args), 1)
            self.assertFalse((root/"results").exists())
            self.assertNotIn("private-password", out.getvalue())


if __name__ == "__main__": unittest.main()
