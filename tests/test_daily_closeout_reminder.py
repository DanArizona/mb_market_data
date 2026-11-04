"""Offline notification-state tests; no UI, Task Scheduler, API or password."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from probes.closeout_readiness_reminder import inspect_due, ReminderDecision


class CloseoutReminderTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.nov3=datetime(2026,11,3,17,15,tzinfo=timezone(timedelta(hours=-5)))
        self.journal=self.root/"output"/"quote_observation_journal_v2_api_opening_2026-11-03"/"2026-11-03.sqlite3"
        self.workflow=self.root/"output"/"daily_universe_production"/"2026-11-04-from-2026-11-03"

    def journal_present(self):
        self.journal.parent.mkdir(parents=True,exist_ok=True)
        self.journal.write_bytes(b"existing journal sentinel; no SQLite read")

    def test_before_close_is_not_due(self):
        self.journal_present()
        result=inspect_due(self.root,now=self.nov3.replace(hour=15,minute=59))
        self.assertEqual(result.status,"NOT_DUE")

    def test_after_close_before_reminder_window_is_not_due(self):
        self.journal_present()
        result=inspect_due(self.root,now=self.nov3.replace(hour=16,minute=9))
        self.assertEqual(result.status,"NOT_DUE")

    def test_no_journal_is_not_mistaken_for_live_market_session(self):
        result=inspect_due(self.root,now=self.nov3)
        self.assertEqual(result.status,"NO_SESSION")

    def test_production_session_without_workflow_is_due(self):
        self.journal_present()
        result=inspect_due(self.root,now=self.nov3)
        self.assertEqual(result.status,"DUE")
        self.assertIn("no dated next-session workflow",result.explanation)

    def test_verified_next_session_suppresses_reminder(self):
        self.journal_present()
        self.workflow.mkdir(parents=True)
        with patch("probes.closeout_readiness_reminder.inspect_preparation",
            return_value={"status":"READY"}) as verify:
            result=inspect_due(self.root,now=self.nov3)
        self.assertEqual(result.status,"READY")
        self.assertIn("2026-11-04",result.explanation)
        verify.assert_called_once()

    def test_incomplete_workflow_remains_due(self):
        self.journal_present()
        self.workflow.mkdir(parents=True)
        with patch("probes.closeout_readiness_reminder.inspect_preparation",
            side_effect=FileNotFoundError("missing manifest")):
            result=inspect_due(self.root,now=self.nov3)
        self.assertEqual(result.status,"DUE")
        self.assertIn("FileNotFoundError",result.explanation)

    def test_wrong_date_directory_does_not_count_ready(self):
        self.journal_present()
        bad=self.root/"output"/"daily_universe_production"/"2026-11-02-from-2026-11-03"
        bad.mkdir(parents=True)
        with patch("probes.closeout_readiness_reminder.inspect_preparation") as verify:
            result=inspect_due(self.root,now=self.nov3)
        self.assertEqual(result.status,"DUE")
        verify.assert_not_called()

    def test_non_weekday_does_not_prompt(self):
        self.journal_present()
        friday=self.nov3+timedelta(days=4)
        result=inspect_due(self.root,now=friday)
        self.assertEqual(result.status,"NOT_DUE")

    def test_time_zone_uses_eastern_not_local_timestamp(self):
        self.journal_present()
        # 15:15 Central = 16:15 Eastern on this date
        central=datetime(2026,11,3,15,15,
                         tzinfo=timezone(timedelta(hours=-6)))
        result=inspect_due(self.root,now=central)
        self.assertEqual(result.status,"DUE")
