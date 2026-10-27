"""Native journal and independent Health coverage for the opt-in cadence."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mb_market_data import acquisition_health as health
from mb_market_data.polling_cadence import (INTERLEAVED, LEGACY, RESERVED_HOT_SECONDS,
    cadence_seconds, check_before_credentials, journal_profile)
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader
from mb_market_data.quote_observation_store import PollRunProvenance, QuoteObservationStore
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
from mb_market_data.watchlist_polling import ET, PollRequest, PollSlot, PollWindow, WatchlistKind, next_poll_slot, poll_slots_between


class CadenceTests(unittest.TestCase):
    def test_default_contract_and_reserved_seconds_remain_explicit(self):
        self.assertEqual(cadence_seconds("focus"), (5, 20, 35, 50))
        self.assertEqual(cadence_seconds("focus", INTERLEAVED), (10, 20, 40, 50))
        uni, focus, hot = (set(cadence_seconds("uni", INTERLEAVED)),
                           set(cadence_seconds("focus", INTERLEAVED)), set(RESERVED_HOT_SECONDS))
        self.assertFalse(uni & focus or uni & hot or focus & hot)
        self.assertEqual(uni | focus | hot, set(range(0, 60, 2)))
        with self.assertRaises(ValueError):
            cadence_seconds("hot", INTERLEAVED)

    def test_full_session_counts_endpoints_and_no_catchup(self):
        start = datetime(2026, 10, 28, 9, 30, tzinfo=ET)
        window = PollWindow(start, start.replace(hour=16, minute=0))
        for profile in (LEGACY, INTERLEAVED):
            slots = poll_slots_between(after=start-timedelta(microseconds=1),
                through=window.end_at, window=window, cadence_profile=profile)
            self.assertEqual(sum(s.watchlist_kind == WatchlistKind.UNI for s in slots), 780)
            self.assertEqual(sum(s.watchlist_kind == WatchlistKind.FOCUS for s in slots), 1560)
            self.assertTrue(all(s.scheduled_at < window.end_at for s in slots))
            self.assertEqual(slots[-1].scheduled_at, start.replace(hour=15, minute=59, second=50))
        self.assertEqual(next_poll_slot(WatchlistKind.FOCUS, after=start+timedelta(seconds=21),
            window=window, cadence_profile=INTERLEAVED).scheduled_at, start+timedelta(seconds=40))

    def test_slot_identity_is_still_channel_and_time_but_validation_is_explicit(self):
        t = datetime(2026, 10, 28, 9, 30, 10, tzinfo=ET)
        with self.assertRaises(ValueError):
            PollSlot(WatchlistKind.FOCUS, t)
        self.assertEqual(PollSlot(WatchlistKind.FOCUS, t, INTERLEAVED).slot_id,
                         "focus:2026-10-28T09:30:10-04:00")


class NativeCadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start = datetime(2026, 10, 28, 9, 30, tzinfo=ET)
        self.stop = self.start+timedelta(minutes=2)
        self.path = self.root/"2026-10-28.sqlite3"
        self.store = QuoteObservationStore(self.path, session_date=self.start.date(), schema_version=2)
        self.store.initialize()
        with patch("mb_market_data.quote_observation_store._utc_now", return_value=self.start-timedelta(minutes=1)):
            self.store.record_membership_revision(SamplingHierarchyRevision(session_date=self.start.date(),
                revision=0, effective_at=self.start, uni_symbols=("TEST",), focus_symbols=("TEST",),
                hot_symbols=(), source="synthetic cadence proof"))
        (self.root/"probes").mkdir()
        (self.root/"probes/probe_universe_quote_watch.py").write_text("# synthetic path only")

    def register(self, channel, profile=INTERLEAVED, label=None):
        name = label or channel
        directory = self.root/name; directory.mkdir(exist_ok=True)
        configuration = {"watchlist_kind": channel, "cadence_profile": profile,
            "poll_seconds": list(cadence_seconds(channel, profile)),
            "poll_window_start_et": self.start.isoformat(), "poll_window_end_et": self.stop.isoformat(),
            "evidence_run_directory": str(directory)}
        self.store.record_run(PollRunProvenance(name, self.start-timedelta(seconds=30), "synthetic", configuration=configuration))
        (directory/"manifest.json").write_text(json.dumps({"journal_run_id": name,
            "journal_schema_version": 2, "completed_at_et": None, "journal_errors": 0}))
        (directory/"errors.log").write_text("")
        return name

    def fill(self, profile=INTERLEAVED):
        slots = poll_slots_between(after=self.start-timedelta(microseconds=1), through=self.stop,
            window=PollWindow(self.start, self.stop), cadence_profile=profile)
        for slot in slots:
            request = PollRequest(slot, 0, ("TEST",), slot.scheduled_at+timedelta(milliseconds=1))
            done = slot.scheduled_at+timedelta(milliseconds=50)
            result = QuoteBatchResult((QuoteResult("TEST", QuoteStatus.QUOTE,
                {"symbol": "TEST", "quote": {"lastPrice": 1}}, None, 1, request.dispatched_at, done),), 1, 400, ())
            self.store.record_acquisition(slot.watchlist_kind.value, request, result, completed_at=done)

    def test_native_audit_two_fresh_replays_and_recorded_cadence(self):
        for channel in ("uni", "focus"): self.register(channel)
        self.fill()
        report = audit_quote_journal(self.path, audited_at=self.stop)
        self.assertTrue(report.passed, report.findings)
        counts = {c.channel: c.acquisition_count for c in report.channels}
        self.assertEqual(counts, {"uni": 4, "focus": 8, "hot": 0})
        self.assertEqual(QuoteJournalReplayReader(self.path).poll_seconds_by_run()["focus"], (10, 20, 40, 50))
        script = Path(__file__).resolve().parents[1]/"probes/replay_quote_observation_journal.py"
        hashes = []
        for _ in range(2):
            p = subprocess.run([sys.executable, str(script), str(self.path), "--progress-every", "0"],
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            hashes.append(re.search(r"Sequence SHA-256\s*:\s*(\w+)", p.stdout).group(1))
        self.assertEqual(hashes[0], hashes[1])

    def test_legacy_journal_still_audits_and_replays(self):
        for channel in ("uni", "focus"): self.register(channel, LEGACY)
        self.fill(LEGACY)
        before = list(QuoteJournalReplayReader(self.path).events())
        self.assertTrue(audit_quote_journal(self.path, audited_at=self.stop).passed)
        self.assertEqual(len(before), 13)
        self.assertEqual(QuoteJournalReplayReader(self.path).poll_seconds_by_run()["focus"], (5, 20, 35, 50))
        script = Path(__file__).resolve().parents[1]/"probes/replay_quote_observation_journal.py"
        output = subprocess.check_output([sys.executable, str(script), str(self.path), "--progress-every", "0"], text=True)
        # Independently reproduced with the unchanged a082f54 replay source.
        self.assertIn("10d5c9cc73e6ac503e4006bf5d7aaa4539225f4881ec3f0d0cea32748793b232", output)

    def test_mixed_registration_is_refused_without_a_new_run(self):
        self.register("uni", LEGACY)
        with self.assertRaisesRegex(ValueError, "different session cadence"):
            self.register("focus", INTERLEAVED)
        # SQLite transaction context managers do not close the connection.
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM poll_run").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "before credentials"):
            check_before_credentials(self.path, INTERLEAVED)

    def test_concurrent_registration_cannot_create_a_mixed_session(self):
        barrier = threading.Barrier(2); results = []
        def work(channel, profile):
            barrier.wait()
            try:
                self.register(channel, profile); results.append("saved")
            except ValueError:
                results.append("refused")
        threads = [threading.Thread(target=work, args=("uni", LEGACY)),
                   threading.Thread(target=work, args=("focus", INTERLEAVED))]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(sorted(results), ["refused", "saved"])

    def test_auto_health_and_restart_preserve_the_registered_profile(self):
        for channel in ("uni", "focus"): self.register(channel)
        self.fill()
        config = health.HealthConfig(self.root, self.path, self.start.date(), self.start, self.stop)
        inv = {"known": True, "error": None, "processes": []}
        status = health.inspect_health(config, now=self.start+timedelta(seconds=110), inventory=inv)
        self.assertEqual(status["cadence_profile"], INTERLEAVED)
        self.assertEqual(status["channels"]["focus"]["missing_slot_count"], 0)
        self.assertIn("--cadence-profile interleaved-v1", health.format_health(status))
        self.assertNotIn("03_prod_poll_uni_restart.cmd", health.format_health(status))
        command = health.restart_command(config, "focus", status, inv,
            now=self.start+timedelta(seconds=110), executable="python")
        self.assertEqual(command[command.index("--cadence-profile")+1], INTERLEAVED)
        wrong = health.inspect_health(replace(config, cadence_profile=LEGACY),
            now=self.start, inventory=inv)
        self.assertEqual(wrong["journal_read"], "UNAVAILABLE")

    def test_unknown_and_mismatched_metadata_are_not_guessed(self):
        with self.assertRaises(ValueError):
            journal_profile([{"configuration_json": json.dumps({"watchlist_kind": "focus",
                "cadence_profile": INTERLEAVED, "poll_seconds": [5, 20, 35, 50]})}])
        with self.assertRaises(ValueError):
            journal_profile([{"configuration_json": json.dumps({"watchlist_kind": "focus",
                "cadence_profile": "future-unknown"})}])

    def test_poller_refuses_conflicting_cadence_before_password_client_or_output(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_universe_quote_watch_credentials import main
        self.register("uni", INTERLEAVED)
        arguments = SimpleNamespace(interval=60, max_samples=None, watchlist_revision=0,
            timeout=20, journal_root=str(self.path.parent), journal_schema_version=2,
            symbols=None, start_at=self.start.isoformat(), stop_at=self.stop.isoformat(),
            ecfg="synthetic.ecfg", watchlist_kind="focus", output_root=str(self.root/"forbidden-output"),
            fields="all", batch_size=400, cadence_profile=LEGACY, universe_csv="unused.csv")
        client = Mock(side_effect=AssertionError("Client must not be constructed"))
        current = self.start-timedelta(seconds=30)
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return current.astimezone(tz) if tz else current.replace(tzinfo=None)
        with patch.dict(main.__globals__, {"parse_args": Mock(return_value=arguments),
                    "datetime": FrozenDatetime,
                    "resolve_ecfg": Mock(return_value=self.root/"synthetic.ecfg"),
                    "prepare_polling_client": client}), \
                patch("getpass.getpass") as password, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(), 2)
        password.assert_not_called(); client.assert_not_called()
        self.assertFalse((self.root/"forbidden-output").exists())


class OverlayCadenceTests(unittest.TestCase):
    def test_open_request_error_band_uses_next_declared_slot(self):
        from test_observation_overlay_view import overlay
        from mb_market_data.observation_overlay_view import resolve_request_error_segments
        sample = overlay(); point = sample.quote_points[0]
        at = point.scheduled_at_utc.replace(second=20)
        sample = replace(sample, replay_time_utc=at+timedelta(seconds=30), quote_points=(
            replace(point, scheduled_at_utc=at, available_at_utc=at,
                    status="request_error", values={}, poll_seconds=(10, 20, 40, 50)),))
        segment = resolve_request_error_segments(sample, off_hours=False)[0]
        self.assertEqual(segment.end_et, at.astimezone(ET)+timedelta(seconds=20))


if __name__ == "__main__": unittest.main()
