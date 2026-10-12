from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mb_market_data.polling_health_status import read_health_status
from mb_market_data.polling_monitor_supervisor import MonitorWatchdog, stop_monitor_child

NOW = datetime(2026, 10, 8, 17, 30, tzinfo=timezone.utc)


class HealthStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.journal = self.directory / "2026-10-08.sqlite3"
        self.snapshot = {"session_date": "2026-10-08", "journal": str(self.journal),
                         "observed_at_utc": NOW.isoformat(), "monitor_pid": 101, "monitor_token": "case-a",
                         "channels": {"focus": {"state": "RUNNING", "reason": "committed", "missing_slot_count": 3}},
                         "journal_read": "OK"}

    def save(self, name="latest.json", value=None):
        (self.directory / name).write_text(json.dumps(value or self.snapshot))

    def read(self, now=NOW):
        return read_health_status(self.directory, journal=self.journal,
                                  session_date=date(2026, 10, 8), now=now)

    def test_current_and_stale_reports_keep_gaps_without_claiming_live_health(self):
        self.save()
        self.assertEqual(self.read()["state"], "CURRENT")
        old = self.read(NOW + timedelta(seconds=31))
        self.assertEqual(old["state"], "STALE")
        self.assertEqual(old["channels"]["focus"]["missing_slot_count"], 3)

    def test_wrong_journal_or_session_and_future_reports_are_refused(self):
        for change in ({"journal": str(self.directory / "other.sqlite3")}, {"session_date": "2026-10-07"},
                       {"observed_at_utc": (NOW + timedelta(minutes=1)).isoformat()}):
            self.save(value={**self.snapshot, **change})
            result = self.read()
            self.assertEqual(result["state"], "UNAVAILABLE")
            self.assertEqual(result["channels"], {})

    def test_missing_and_malformed_files_are_unavailable(self):
        self.assertEqual(self.read()["state"], "UNAVAILABLE")
        (self.directory / "latest.json").write_text("{")
        self.assertEqual(self.read()["state"], "UNAVAILABLE")

    def test_clean_stop_and_old_stop_receipts_are_distinguished(self):
        self.save()
        self.save("last_stop.json", {"monitor_pid": 101, "journal": str(self.journal),
                                    "stopped_at_utc": NOW.isoformat()})
        self.assertEqual(self.read()["state"], "STOPPED")
        self.save("last_stop.json", {"monitor_pid": 100, "journal": str(self.journal),
                                    "stopped_at_utc": NOW.isoformat()})
        self.assertEqual(self.read()["state"], "CURRENT")

    def test_supervisor_failure_and_stale_supervision_override_last_channel_state(self):
        self.save()
        watch = {"monitor_token": "case-a", "child_pid": 101, "journal": str(self.journal),
                 "observed_at_utc": NOW.isoformat(), "state": "STOPPED", "detail": "Inspector exited"}
        self.save("supervisor.json", watch)
        self.assertEqual(self.read()["state"], "STOPPED")
        self.save("supervisor.json", {**watch, "state": "RUNNING", "observed_at_utc": (NOW - timedelta(seconds=40)).isoformat()})
        self.assertEqual(self.read()["state"], "STALE")


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.journal = Path(tempfile.gettempdir()) / "synthetic-watchdog.sqlite3"
        self.watchdog = MonitorWatchdog(child_pid=101, token="case-a", journal=self.journal,
                                       started_monotonic=0, timeout_seconds=30)
        self.snapshot = {"monitor_pid": 101, "monitor_token": "case-a", "journal": str(self.journal),
                         "observed_at_utc": NOW.isoformat()}

    def inspect(self, now, snapshot=None, exit_code=None):
        return self.watchdog.inspect(snapshot, child_exit=exit_code, now_monotonic=now)

    def test_child_exit_is_detected_immediately_without_a_journal_read(self):
        self.assertEqual(self.inspect(1, exit_code=2)["state"], "STOPPED")

    def test_hung_child_is_overdue_even_when_its_process_is_alive(self):
        self.assertEqual(self.inspect(1, self.snapshot)["state"], "RUNNING")
        self.assertEqual(self.inspect(32, self.snapshot)["state"], "OVERDUE")

    def test_first_check_timeout_and_recovery_require_a_new_completed_check(self):
        self.assertEqual(self.inspect(1)["state"], "STARTING")
        self.assertEqual(self.inspect(31)["state"], "OVERDUE")
        self.assertEqual(self.inspect(32, self.snapshot)["state"], "RUNNING")

    def test_unrelated_pid_generation_and_journal_cannot_supply_a_heartbeat(self):
        for changes in ({"monitor_pid": 99}, {"monitor_token": "old-case"}, {"journal": "other.sqlite3"}):
            self.assertEqual(self.inspect(31, {**self.snapshot, **changes})["state"], "OVERDUE")

    def test_shutdown_can_terminate_only_its_own_hung_monitor_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with tempfile.TemporaryDirectory() as directory:
                stop_monitor_child(child, Path(directory) / "stop.request", grace_seconds=.03)
            self.assertIsNotNone(child.poll())
            self.assertIsNone(other.poll())
        finally:
            if child.poll() is None:
                child.kill()
            other.kill()
            child.wait()
            other.wait()


if __name__ == "__main__":
    unittest.main()
