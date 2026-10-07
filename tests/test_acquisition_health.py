from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import closing, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import acquisition_health as health
except ModuleNotFoundError:
    from mb_market_data import acquisition_health as health

try:
    import polling_alarm as alarm
except ModuleNotFoundError:
    from mb_market_data import polling_alarm as alarm

from mb_market_data.quote_observation_store import PollRunProvenance, QuoteObservationStore
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
from mb_market_data.watchlist_polling import PollRequest, PollSlot, SkippedPollSlot, WatchlistKind


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session = date(2026, 10, 6)
        self.start = datetime(2026, 10, 6, 9, 30, tzinfo=health.ET)
        publication_clock = patch("mb_market_data.quote_observation_store._utc_now", return_value=self.start - timedelta(minutes=2))
        publication_clock.start()
        self.addCleanup(publication_clock.stop)
        self.stop = self.start + timedelta(minutes=2)
        self.journal = self.root / "output/journal" / f"{self.session}.sqlite3"
        self.config = health.HealthConfig(self.root, self.journal, self.session, self.start, self.stop)
        (self.root / "probes").mkdir()
        (self.root / "probes/probe_universe_quote_watch.py").write_text("# SYNTHETIC; never executed\n")
        self.store = QuoteObservationStore(self.journal, session_date=self.session, schema_version=2)
        self.store.initialize()
        self.store.record_membership_revision(SamplingHierarchyRevision(
            session_date=self.session, revision=0, effective_at=self.start,
            uni_symbols=("MSFT",), focus_symbols=("MSFT",), hot_symbols=(), source="SYNTHETIC health acceptance"))
        self.runs = {}
        for channel in health.CADENCES:
            self.register(channel)

    def register(self, channel, number=0, started=None):
        started = started or self.start - timedelta(minutes=1)
        run_id = f"SYNTHETIC:{channel}:{number}"
        directory = self.root / f"evidence/{channel}_{number}"
        directory.mkdir(parents=True)
        configuration = {"watchlist_kind": channel, "poll_window_start_et": self.start.isoformat(),
                         "poll_window_end_et": self.stop.isoformat(), "evidence_run_directory": str(directory)}
        self.store.record_run(PollRunProvenance(run_id=run_id, started_at=started,
                             software_version="SYNTHETIC", configuration=configuration))
        manifest = {"journal_run_id": run_id, "journal_schema_version": 2, "completed_at_et": None, "journal_errors": 0}
        health.atomic_json(directory / "manifest.json", manifest)
        (directory / "errors.log").write_text("")
        self.runs[channel] = (run_id, directory)
        return run_id

    def commit(self, channel, second, *, stale=False, status=QuoteStatus.QUOTE):
        slot = self.start + timedelta(seconds=second)
        request = PollRequest(PollSlot(WatchlistKind(channel), slot), 0, ("MSFT",), slot + timedelta(milliseconds=1))
        payload = {"symbol": "MSFT", "quote": {"lastPrice": 100, "tradeTime": 1 if stale else 1791293400000}}
        result = QuoteBatchResult((QuoteResult("MSFT", status, payload, None, 1,
                                              request.dispatched_at, slot + timedelta(milliseconds=300)),), 1, 400, ())
        self.store.record_acquisition(self.runs[channel][0], request, result, completed_at=slot + timedelta(milliseconds=300))

    def inventory(self, channels=("uni", "focus")):
        return {"known": True, "error": None, "processes": [
            {"pid": index + 100, "argv": ["python", str(self.root / "probes/probe_universe_quote_watch.py"),
                "--watchlist-kind", channel, "--start-at", self.start.isoformat(),
                "--journal-root", str(self.journal.parent), "--journal-schema-version", "2"]}
            for index, channel in enumerate(channels)]}

    def snapshot(self, second=110, channels=("uni", "focus")):
        return health.inspect_health(self.config, now=self.start + timedelta(seconds=second), inventory=self.inventory(channels))

    def seed_recent(self):
        for second in (0, 30, 60, 90):
            self.commit("uni", second, stale=True)
        for second in (5, 20, 35, 50, 65, 80, 95):
            self.commit("focus", second, stale=True)

    def fail_focus(self):
        self.commit("focus", 5)
        run_id, directory = self.runs["focus"]
        health.atomic_json(directory / "manifest.json", {"journal_run_id": run_id, "journal_schema_version": 2,
                          "completed_at_et": (self.start + timedelta(seconds=20.5)).isoformat(), "journal_errors": 1})
        (directory / "errors.log").write_text("SYNTHETIC JournalWriteError: database is locked\n")

    def test_stale_market_quotes_are_successful_polling(self):
        self.seed_recent()
        snapshot = self.snapshot()
        self.assertEqual({item["state"] for item in snapshot["channels"].values()}, {"RUNNING"})
        self.assertFalse(health.alert_conditions(snapshot))

    def test_focus_fails_while_uni_keeps_committing(self):
        self.fail_focus()
        for second in (0, 30, 60, 90):
            self.commit("uni", second)
        snapshot = self.snapshot(119, ("uni",))
        self.assertEqual(snapshot["channels"]["uni"]["state"], "RUNNING")
        self.assertEqual(snapshot["channels"]["focus"]["state"], "FAILED")
        self.assertEqual(snapshot["channels"]["focus"]["missing_slot_count"], 1)
        self.assertIn("database is locked", snapshot["channels"]["focus"]["reason"])

    def test_journal_error_detected_before_missing_slot_grace(self):
        self.fail_focus()
        snapshot = self.snapshot(21, ("uni",))
        self.assertEqual(snapshot["channels"]["focus"]["state"], "FAILED")
        self.assertEqual(snapshot["channels"]["focus"]["missing_slot_count"], 0)

    def test_missing_process_detects_abrupt_exit_without_final_manifest(self):
        self.seed_recent()
        snapshot = self.snapshot(channels=("uni",))
        self.assertEqual(snapshot["channels"]["focus"]["state"], "PROCESS_MISSING")

    def test_hung_process_is_overdue_despite_pid_presence(self):
        self.commit("focus", 5)
        snapshot = self.snapshot(119)
        self.assertEqual(snapshot["channels"]["focus"]["state"], "OVERDUE")
        self.assertEqual(snapshot["channels"]["focus"]["missing_slot_count"], 1)

    def test_restart_does_not_clear_failure_until_new_commit(self):
        self.fail_focus()
        directory = self.root / "health"
        health.persist_health(directory, self.snapshot(21, ("uni",)))
        self.register("focus", 1, self.start + timedelta(seconds=22))
        pending = self.snapshot(23)
        self.assertEqual(pending["channels"]["focus"]["state"], "AWAITING_COMMIT")
        health.persist_health(directory, pending)
        incidents = json.loads((directory / "incidents.json").read_text())
        failure = next(item for item in incidents if ":FAILED:" in item["key"])
        self.assertIsNone(failure["resolved_at_utc"])
        self.commit("focus", 35)
        recovered = self.snapshot(36)
        self.assertEqual(recovered["channels"]["focus"]["state"], "RUNNING")
        health.persist_health(directory, recovered)
        incidents = json.loads((directory / "incidents.json").read_text())
        failure = next(item for item in incidents if ":FAILED:" in item["key"])
        self.assertIsNotNone(failure["resolved_at_utc"])
        later = self.snapshot(119)
        self.assertEqual(later["channels"]["focus"]["state"], "RUNNING")
        self.assertEqual(later["channels"]["focus"]["missing_slot_count"], 1)

    def test_grace_does_not_hide_old_gaps_or_count_future_slots(self):
        self.commit("focus", 5)
        self.assertEqual(self.snapshot(110)["channels"]["focus"]["missing_slot_count"], 1)
        self.assertEqual(self.snapshot(211)["channels"]["focus"]["missing_slot_count"], 7)

    def test_empty_membership_skips_are_successful_slots(self):
        self.store.record_membership_revision(SamplingHierarchyRevision(
            session_date=self.session, revision=1, effective_at=self.start + timedelta(seconds=1),
            uni_symbols=("MSFT",), focus_symbols=(), hot_symbols=(), source="SYNTHETIC empty Focus"))
        for second in (5, 20):
            slot = self.start + timedelta(seconds=second)
            self.store.record_skipped_slot(self.runs["focus"][0], SkippedPollSlot(
                PollSlot(WatchlistKind.FOCUS, slot), 1, self.start + timedelta(seconds=1), slot + timedelta(milliseconds=1)))
        snapshot = self.snapshot(110)
        self.assertEqual(snapshot["channels"]["focus"]["state"], "RUNNING")
        self.assertEqual(snapshot["channels"]["focus"]["missing_slot_count"], 0)
        self.assertEqual(snapshot["channels"]["focus"]["committed_skips"], 2)

    def test_monitor_does_not_modify_journal_or_hold_connections(self):
        self.seed_recent()
        with closing(sqlite3.connect(self.journal)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = hashlib.sha256(self.journal.read_bytes()).hexdigest()
        for _ in range(10):
            self.snapshot()
        self.assertEqual(before, hashlib.sha256(self.journal.read_bytes()).hexdigest())
        renamed = self.journal.with_suffix(".renamed")
        self.journal.rename(renamed)  # also catches leaked connections on Windows
        renamed.rename(self.journal)

    def test_read_failure_is_alerted_and_never_fabricates_health(self):
        self.journal.rename(self.journal.with_suffix(".missing"))
        snapshot = self.snapshot()
        self.assertEqual(snapshot["journal_read"], "UNAVAILABLE")
        self.assertFalse(snapshot["channels"])
        self.assertIn("journal:unavailable", health.alert_conditions(snapshot))
        with self.assertRaisesRegex(ValueError, "journal cannot be read"):
            health.restart_command(self.config, "focus", snapshot, self.inventory(()), now=self.start + timedelta(seconds=110))

    def test_monitor_sees_committed_wal_while_writer_connection_stays_open(self):
        self.commit("uni", 0)
        with closing(sqlite3.connect(self.journal)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE quote_observation SET quote_last_price=123")
            writer.commit()
            self.assertTrue(Path(str(self.journal) + "-wal").exists())
            self.assertEqual(self.snapshot(10)["channels"]["uni"]["committed_acquisitions"], 1)
            writer.execute("BEGIN IMMEDIATE")
            self.snapshot(10)  # a short read must work during an uncommitted WAL write
            writer.rollback()

    def test_persistent_alert_survives_monitor_restart_and_read_failure(self):
        self.fail_focus()
        directory = self.root / "health"
        first = self.snapshot(21, ("uni",))
        new = health.persist_health(directory, first)
        self.assertTrue(new)
        self.assertFalse(health.persist_health(directory, first))
        self.journal.rename(self.journal.with_suffix(".missing"))
        health.persist_health(directory, self.snapshot())
        history = json.loads((directory / "incidents.json").read_text())
        self.assertIsNone(next(item for item in history if ":FAILED:" in item["key"])["resolved_at_utc"])

    def test_restart_refuses_running_unknown_duplicate_or_outside_window(self):
        self.fail_focus()
        now = self.start + timedelta(seconds=110)
        snapshot = self.snapshot()
        with self.assertRaisesRegex(ValueError, "already exist"):
            health.restart_command(self.config, "focus", snapshot, self.inventory(), now=now)
        unknown = {"known": False, "processes": [], "error": "CIM failed"}
        with self.assertRaisesRegex(ValueError, "inspection is unavailable"):
            health.restart_command(self.config, "focus", snapshot, unknown, now=now)
        with self.assertRaisesRegex(ValueError, "active session window"):
            health.restart_command(self.config, "focus", snapshot, self.inventory(()), now=self.stop)
        inventory = self.inventory(("focus", "focus"))
        self.assertEqual(health.inspect_health(self.config, now=now, inventory=inventory)["channels"]["focus"]["candidate_pids"], [100, 101])

    def test_restart_uses_original_journal_and_same_window(self):
        self.fail_focus()
        inventory = self.inventory(("uni",))
        snapshot = self.snapshot(channels=("uni",))
        command = health.restart_command(self.config, "focus", snapshot, inventory,
                                         now=self.start + timedelta(seconds=110), executable="SYNTHETIC-python")
        self.assertEqual(command[command.index("--journal-root") + 1], str(self.journal.resolve().parent))
        self.assertEqual(health.timestamp(command[command.index("--start-at") + 1]), health.timestamp(self.start))
        self.assertEqual(health.timestamp(command[command.index("--stop-at") + 1]), health.timestamp(self.stop))
        self.assertNotIn("--max-samples", command)

    def test_wrong_session_schema_and_malformed_manifest_are_refused(self):
        _, directory = self.runs["focus"]
        (directory / "manifest.json").write_text("{in-progress")
        self.assertEqual(self.snapshot()["channels"]["focus"]["state"], "EVIDENCE_UNAVAILABLE")
        with closing(sqlite3.connect(self.journal)) as connection:
            connection.execute("UPDATE store_identity SET session_date='2026-10-05'")
            connection.commit()
        self.assertEqual(self.snapshot()["journal_read"], "UNAVAILABLE")

    def test_relative_windows_path_and_equals_flags_guard_duplicates(self):
        config = self.config
        inventory = [{"pid": 345, "argv": ["python", "probes\\probe_universe_quote_watch.py",
                     "--watchlist-kind=focus", "--start-at=" + self.start.isoformat(),
                     "--journal-root=output\\journal", "--journal-schema-version=2"]}]
        self.assertEqual(health.matching_processes(config, "focus", inventory), [345])
        self.assertEqual(health.matching_processes(config, "uni", inventory), [])

    def test_independent_reader_under_concurrent_writer(self):
        errors = []
        def write():
            try:
                for second in (0, 30, 60, 90):
                    self.commit("uni", second)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=write)
        worker.start()
        for _ in range(15):
            self.snapshot(119)
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(self.snapshot(119)["channels"]["uni"]["committed_acquisitions"], 4)

    def test_restart_write_probe_refuses_live_contention_without_changing_records(self):
        self.seed_recent()
        with closing(sqlite3.connect(self.journal)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            self.assertEqual(self.snapshot()["journal_read"], "OK")
            began = time.monotonic()
            with self.assertRaisesRegex(ValueError, "write access is still unavailable"):
                health.check_write_available(self.config)
            self.assertLess(time.monotonic() - began, 1.0)
            writer.rollback()
        before = hashlib.sha256(self.journal.read_bytes()).hexdigest()
        health.check_write_available(self.config)
        self.assertEqual(before, hashlib.sha256(self.journal.read_bytes()).hexdigest())

    def test_committed_request_errors_raise_separate_data_alert(self):
        self.commit("focus", 5, status=QuoteStatus.REQUEST_ERROR)
        snapshot = self.snapshot(6)
        self.assertEqual(snapshot["channels"]["focus"]["state"], "RUNNING")
        self.assertIn("focus:request_error", health.alert_conditions(snapshot))

    def test_wrong_polling_window_does_not_stop_monitor_without_a_status(self):
        with closing(sqlite3.connect(self.journal)) as connection:
            row = connection.execute("SELECT run_id,configuration_json FROM poll_run LIMIT 1").fetchone()
            configuration = json.loads(row[1])
            configuration["poll_window_end_et"] = (self.stop + timedelta(minutes=1)).isoformat()
            connection.execute("UPDATE poll_run SET configuration_json=? WHERE run_id=?", (json.dumps(configuration), row[0]))
            connection.commit()
        snapshot = self.snapshot()
        self.assertEqual(snapshot["journal_read"], "UNAVAILABLE")
        self.assertIn("journal:unavailable", health.alert_conditions(snapshot))


    def test_operation_lock_blocks_another_process_and_releases_on_exit(self):
        source_directory = str(Path(health.__file__).parent)
        code = "import sys; sys.path.insert(0,sys.argv[1]); import acquisition_health as h;\ntry:\n with h.operation_lock(sys.argv[2]): sys.exit(3)\nexcept ValueError: sys.exit(0)"
        key = "SYNTHETIC health mutex " + str(self.root)
        with health.operation_lock(key):
            result = subprocess.run([sys.executable, "-c", code, source_directory, key], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        with health.operation_lock(key):
            pass

    def test_custom_polling_options_are_not_silently_reset_by_restart(self):
        self.fail_focus()
        _, directory = self.runs["focus"]
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest["batch_size"] = 200
        health.atomic_json(directory / "manifest.json", manifest)
        snapshot = self.snapshot(channels=("uni",))
        with self.assertRaisesRegex(ValueError, "custom poller options"):
            health.restart_command(self.config, "focus", snapshot, self.inventory(("uni",)),
                                   now=self.start + timedelta(seconds=110))

    def test_operation_lock_survives_no_stale_owner_after_process_crash(self):
        code = "import sys,os; sys.path.insert(0,sys.argv[1]); import acquisition_health as h;\nwith h.operation_lock(sys.argv[2]): os._exit(0)"
        key = "SYNTHETIC abandoned mutex " + str(self.root)
        result = subprocess.run([sys.executable, "-c", code, str(Path(health.__file__).parent), key],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with health.operation_lock(key):
            pass

    def cli(self):
        import runpy
        adjacent = Path(health.__file__).parent / "polling_health.py"
        source = adjacent if adjacent.exists() else Path(__file__).resolve().parents[1] / "probes/polling_health.py"
        return runpy.run_path(str(source), run_name="synthetic_health_cli")

    def test_restart_plan_neither_reserves_write_access_nor_launches(self):
        self.fail_focus()
        cli = self.cli()
        snapshot = self.snapshot(channels=("uni",))
        with patch.object(health, "windows_process_inventory", return_value=self.inventory(("uni",))), \
             patch.object(health, "inspect_health", return_value=snapshot), \
             patch.object(health, "restart_command", return_value=["SYNTHETIC", "not-launched"]), \
             patch.object(health, "check_write_available") as write_probe, \
             patch("subprocess.run") as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(cli["restart"](self.config, "focus", plan_only=True), 0)
            write_probe.assert_not_called()
            launch.assert_not_called()

    def test_guided_restart_refuses_unresolved_writer_lock_before_launch(self):
        self.fail_focus()
        cli = self.cli()
        snapshot = self.snapshot(channels=("uni",))
        with patch.object(health, "windows_process_inventory", return_value=self.inventory(("uni",))), \
             patch.object(health, "inspect_health", return_value=snapshot), \
             patch.object(health, "restart_command", return_value=["SYNTHETIC", "not-launched"]), \
             patch.object(health, "check_write_available", side_effect=ValueError("SYNTHETIC write access still unavailable")), \
             patch("subprocess.run") as launch, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "write access still unavailable"):
                cli["restart"](self.config, "focus")
            launch.assert_not_called()

    def test_guided_restart_records_one_launch_receipt_and_child_exit(self):
        from types import SimpleNamespace
        self.fail_focus()
        cli = self.cli()
        snapshot = self.snapshot(channels=("uni",))
        command = health.restart_command(self.config, "focus", snapshot, self.inventory(("uni",)),
                                         now=self.start + timedelta(seconds=110), executable="SYNTHETIC-python")
        with patch.object(health, "windows_process_inventory", return_value=self.inventory(("uni",))) as inventory, \
             patch.object(health, "inspect_health", return_value=snapshot), \
             patch.object(health, "restart_command", return_value=command), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(cli["restart"](self.config, "focus"), 0)
            self.assertEqual(inventory.call_count, 2)
            launch.assert_called_once_with(command, cwd=self.root, check=False)
        receipts = list((self.root / "output/polling_restart_requests").rglob("request.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["journal"], str(self.journal))
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIsNotNone(receipt["child_finished_at_utc"])


    def test_normal_finish_after_final_slot_before_window_end_is_healthy(self):
        self.seed_recent()
        self.commit("focus", 110)
        for channel, second in (("uni", 90), ("focus", 110)):
            run_id, directory = self.runs[channel]
            health.atomic_json(directory / "manifest.json", {"journal_run_id": run_id, "journal_schema_version": 2,
                              "completed_at_et": (self.start + timedelta(seconds=second + .4)).isoformat(), "journal_errors": 0})
        before = self.snapshot(119, ())
        self.assertEqual({info["state"] for info in before["channels"].values()}, {"SCHEDULE_COMPLETE"})
        self.assertFalse(health.alert_conditions(before))
        after = self.snapshot(211, ())
        self.assertEqual({info["state"] for info in after["channels"].values()}, {"WINDOW_ENDED"})
        self.assertFalse(health.alert_conditions(after))

    def test_finish_without_final_commit_is_not_declared_schedule_complete(self):
        self.seed_recent()  # final Focus slot at :50 of the second minute is missing
        run_id, directory = self.runs["focus"]
        health.atomic_json(directory / "manifest.json", {"journal_run_id": run_id, "journal_schema_version": 2,
                          "completed_at_et": (self.start + timedelta(seconds=110.4)).isoformat(), "journal_errors": 0})
        snapshot = self.snapshot(211, ())
        self.assertEqual(snapshot["channels"]["focus"]["state"], "INCOMPLETE_FINISH")
        self.assertEqual(snapshot["channels"]["focus"]["missing_slot_count"], 1)
        self.assertIn("focus:coverage", health.alert_conditions(snapshot))

    def test_explicit_monitor_stop_writes_receipt_without_opening_journal(self):
        cli = self.cli()
        stop = self.root / "SYNTHETIC.stop"
        stop.touch()
        with patch.object(health, "inspect_health") as inspect, redirect_stdout(io.StringIO()):
            self.assertEqual(cli["monitor"](self.config, popups=False, stop_file=stop), 0)
        inspect.assert_not_called()
        receipt = json.loads((self.root / "output/polling_health" / str(self.session) / "last_stop.json").read_text())
        self.assertEqual(receipt["stopped_by"], "stop request")
        with health.operation_lock("monitor:" + str(self.config.journal).casefold()):
            pass

    def test_keyboard_interrupt_closes_monitor_and_restores_signal_handler(self):
        import signal
        original = signal.getsignal(signal.SIGINT)
        cli = self.cli()
        with patch.object(health, "inspect_health", side_effect=KeyboardInterrupt), redirect_stdout(io.StringIO()):
            self.assertEqual(cli["monitor"](self.config, popups=False), 130)
        self.assertEqual(signal.getsignal(signal.SIGINT), original)
        receipt = json.loads((self.root / "output/polling_health" / str(self.session) / "last_stop.json").read_text())
        self.assertEqual(receipt["stopped_by"], "keyboard interrupt")
        with health.operation_lock("monitor:" + str(self.config.journal).casefold()):
            pass

    def test_monitor_wait_observes_stop_request_promptly(self):
        cli = self.cli()
        stop = self.root / "SYNTHETIC.stop"
        timer = threading.Timer(.1, stop.touch)
        timer.start()
        began = time.monotonic()
        try:
            self.assertEqual(cli["wait_for_stop"](5, stop), "stop request")
        finally:
            timer.join()
        self.assertLess(time.monotonic() - began, 1)

    def test_extended_console_key_is_not_mistaken_for_q_shutdown(self):
        from collections import deque
        from types import SimpleNamespace
        keys = deque(["\xe0", "Q", "q"])  # PgDn, then the actual Q stop key
        keyboard = SimpleNamespace(kbhit=lambda: bool(keys), getwch=keys.popleft)
        cli = self.cli()
        with patch("os.name", "nt"), patch.dict(sys.modules, {"msvcrt": keyboard}):
            self.assertEqual(cli["wait_for_stop"](1), "Q key")
        self.assertFalse(keys)

    def test_restarted_run_with_old_gaps_waits_for_its_own_first_slot(self):
        from dataclasses import replace
        self.config = replace(self.config, grace_seconds=5)
        self.fail_focus()
        directory = self.root / "health"
        health.persist_health(directory, self.snapshot(21, ("uni",)))
        self.register("focus", 1, self.start + timedelta(seconds=22))
        waiting = self.snapshot(26)
        self.assertEqual(waiting["channels"]["focus"]["state"], "AWAITING_COMMIT")
        self.assertEqual(waiting["channels"]["focus"]["missing_slot_count"], 1)
        health.persist_health(directory, waiting)
        history = json.loads((directory / "incidents.json").read_text())
        self.assertIsNone(next(x for x in history if ":FAILED:" in x["key"])["resolved_at_utc"])
        overdue = self.snapshot(41)
        self.assertEqual(overdue["channels"]["focus"]["state"], "OVERDUE")
        self.commit("focus", 35)
        resumed = self.snapshot(42)
        self.assertEqual(resumed["channels"]["focus"]["state"], "RUNNING")
        self.assertEqual(resumed["channels"]["focus"]["missing_slot_count"], 1)

    def test_unreadable_process_is_excluded_only_after_cim_confirms_absence(self):
        from types import SimpleNamespace
        rows = [{"ProcessId": 100, "CommandLine": "python healthy.py"},
                {"ProcessId": 200, "CommandLine": None}]
        replies = [SimpleNamespace(stdout=json.dumps(rows)), SimpleNamespace(stdout="[]")]
        with patch.object(health.subprocess, "run", side_effect=replies) as run:
            self.assertEqual(health.windows_process_rows(), rows[:1])
        self.assertIn('ProcessId=200', run.call_args_list[1].args[0][-1])
        self.assertEqual(run.call_count, 2)

    def test_unreadable_process_that_becomes_readable_is_kept(self):
        from types import SimpleNamespace
        readable = {"ProcessId": 200, "CommandLine": "python replacement.py"}
        replies = [SimpleNamespace(stdout='[{"ProcessId":200,"CommandLine":null}]'),
                   SimpleNamespace(stdout=json.dumps(readable))]
        with patch.object(health.subprocess, "run", side_effect=replies):
            self.assertEqual(health.windows_process_rows(), [readable])

    def test_still_present_unreadable_process_is_not_ignored(self):
        from types import SimpleNamespace
        reply = SimpleNamespace(stdout='[{"ProcessId":200,"CommandLine":null}]')
        with patch.object(health.subprocess, "run", return_value=reply):
            with self.assertRaisesRegex(ValueError, "unreadable command line after recheck"):
                health.windows_process_rows()

    def test_cim_recheck_failure_does_not_prove_process_absence(self):
        from types import SimpleNamespace
        replies = [SimpleNamespace(stdout='[{"ProcessId":200,"CommandLine":null}]'),
                   subprocess.TimeoutExpired("SYNTHETIC CIM", 1)]
        with patch.object(health.subprocess, "run", side_effect=replies):
            with self.assertRaises(subprocess.TimeoutExpired):
                health.windows_process_rows()

    def test_cim_recheck_shares_original_deadline(self):
        from types import SimpleNamespace
        replies = [SimpleNamespace(stdout='[{"ProcessId":200,"CommandLine":null}]'),
                   SimpleNamespace(stdout="[]")]
        with patch.object(health.subprocess, "run", side_effect=replies) as run, \
             patch.object(health.time, "monotonic", side_effect=[100, 101, 107]):
            self.assertEqual(health.windows_process_rows(), [])
        self.assertEqual([x.kwargs["timeout"] for x in run.call_args_list], [7, 1])

    def test_default_monitor_stop_request_can_stop_a_running_monitor(self):
        cli = self.cli()
        stop = self.root / "output/polling_health" / str(self.session) / "stop.request"
        stop.parent.mkdir(parents=True)
        stop.touch()  # a request left over from a previous monitor is cleared
        snapshot = self.snapshot(21)
        def inspect(config):
            self.assertFalse(stop.exists())
            stop.touch()  # a new request is honored before the next inspection
            return snapshot
        with patch.object(health, "inspect_health", side_effect=inspect) as inspection, redirect_stdout(io.StringIO()):
            self.assertEqual(cli["monitor"](self.config, interval=5, popups=False), 0)
        self.assertEqual(inspection.call_count, 1)

    def alarm_payload(self, snapshot, directory=None):
        incidents = []
        if directory is not None:
            incidents = json.loads((directory / "incidents.json").read_text())
        return alarm.alarm_payload(snapshot, health.alert_conditions(snapshot), incidents,
                                   window_active=next(iter(snapshot["channels"].values()), {}).get("polling_window_active", True),
                                   status_path=self.root / "STATUS.txt", poller_windows="SYNTHETIC windows")

    def test_alarm_repeats_until_recovery_and_latches_a_quiet_notice(self):
        self.fail_focus()
        policy = alarm.AlarmPolicy()
        policy.update(self.alarm_payload(self.snapshot(21, ("uni",))), 0)
        self.assertTrue(policy.sound_due(0))
        self.assertFalse(policy.sound_due(9))
        self.assertTrue(policy.sound_due(10))
        self.register("focus", 1, self.start + timedelta(seconds=22))
        self.commit("focus", 35)
        policy.update(self.alarm_payload(self.snapshot(36)), 11)
        self.assertFalse(policy.sound_due(20))
        self.assertTrue(policy.visible)
        self.assertIn("database is locked", policy.view(20)["text"])
        self.assertTrue(policy.dismiss())
        self.assertFalse(policy.visible)
        self.assertNotIn("database is locked", policy.view(21)["text"])

    def test_alarm_silence_expires_and_a_new_failure_breaks_silence(self):
        policy = alarm.AlarmPolicy()
        payload = {"urgent": {"focus:FAILED:old": "failure"}, "notices": {}, "text": "", "observed_at_utc": "", "status_path": ""}
        policy.update(payload, 0)
        policy.silence(1)
        policy.update(dict(payload), 50)
        self.assertFalse(policy.sound_due(300))
        self.assertTrue(policy.sound_due(301))
        policy.silence(302)
        payload = dict(payload, urgent={**payload["urgent"], "uni:FAILED:new": "new failure"})
        policy.update(payload, 303)
        self.assertTrue(policy.sound_due(303))

    def test_alarm_cannot_dismiss_an_unresolved_failure(self):
        self.fail_focus()
        policy = alarm.AlarmPolicy()
        policy.update(self.alarm_payload(self.snapshot(21, ("uni",))), 0)
        self.assertFalse(policy.dismiss())
        self.assertTrue(policy.visible)
        self.assertTrue(policy.sound_due(10))

    def test_alarm_historical_gaps_are_quiet_and_dismissal_survives_refresh(self):
        self.seed_recent()
        snapshot = self.snapshot(119)
        snapshot["channels"]["focus"].update(missing_slot_count=17, missing_slots=[self.start.isoformat()])
        policy = alarm.AlarmPolicy()
        payload = self.alarm_payload(snapshot)
        self.assertFalse(payload["urgent"])
        policy.update(payload, 0)
        self.assertTrue(policy.visible)
        self.assertFalse(policy.sound_due(0))
        self.assertTrue(policy.dismiss())
        policy.update(self.alarm_payload(snapshot), 50)
        self.assertFalse(policy.visible)

    def test_alarm_keeps_failed_run_urgent_while_replacement_awaits_commit(self):
        from dataclasses import replace
        self.config = replace(self.config, grace_seconds=5)
        self.fail_focus()
        directory = self.root / "health"
        health.persist_health(directory, self.snapshot(21, ("uni",)))
        self.register("focus", 1, self.start + timedelta(seconds=22))
        waiting = self.snapshot(26)
        health.persist_health(directory, waiting)
        payload = self.alarm_payload(waiting, directory)
        self.assertTrue(any(":FAILED:" in key for key in payload["urgent"]))
        self.commit("focus", 35)
        resumed = self.snapshot(36)
        health.persist_health(directory, resumed)
        payload = self.alarm_payload(resumed, directory)
        self.assertFalse(any(key.startswith("focus:") for key in payload["urgent"]))
        self.assertIn("focus:coverage", payload["notices"])

    def test_alarm_sound_worker_repeats_without_more_journal_inspections(self):
        service = alarm.DesktopAlarm(sound=lambda: heard.set())
        service.policy.repeat_seconds = 0.15
        heard = threading.Event()
        service.publish({"urgent": {"focus:FAILED:test": "failure"}, "notices": {}, "text": "", "observed_at_utc": "", "status_path": ""})
        worker = threading.Thread(target=service._sound)
        service.threads = [worker]
        worker.start()
        try:
            self.assertTrue(heard.wait(1))
            heard.clear()
            self.assertTrue(heard.wait(1))
        finally:
            service.close()
        self.assertFalse(worker.is_alive())

    def test_alarm_journal_read_failure_is_urgent_and_does_not_clear_channel_failure(self):
        self.fail_focus()
        directory = self.root / "health"
        health.persist_health(directory, self.snapshot(21, ("uni",)))
        self.journal.unlink()
        unreadable = self.snapshot(26)
        health.persist_health(directory, unreadable)
        payload = self.alarm_payload(unreadable, directory)
        self.assertIn("journal:unavailable", payload["urgent"])
        incidents = json.loads((directory / "incidents.json").read_text())
        self.assertIsNone(next(item for item in incidents if ":FAILED:" in item["key"])["resolved_at_utc"])

    def test_monitor_publishes_persistent_alarm_and_closes_it_on_exit(self):
        import os
        from types import SimpleNamespace
        cli = self.cli()
        globals_ = cli["monitor"].__globals__
        snapshot = self.snapshot(21)
        with patch.dict(globals_, {"os": SimpleNamespace(name="nt", getpid=os.getpid), "wait_for_stop": lambda *args: None}), \
             patch.object(alarm, "DesktopAlarm") as desktop, \
             patch.object(health, "inspect_health", return_value=snapshot), redirect_stdout(io.StringIO()):
            self.assertEqual(cli["monitor"](self.config, once=True), 0)
        desktop.return_value.start.assert_called_once()
        desktop.return_value.publish.assert_called_once()
        desktop.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
