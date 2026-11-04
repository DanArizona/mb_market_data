"""Offline operator closeout launch tests; never access provider or credentials."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from probes.attended_closeout_today import run_operator_closeout


class AttendedCloseoutTodayTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.closed=date(2026,11,3)
        self.now=datetime(2026,11,3,17,20,
            tzinfo=timezone(timedelta(hours=-5)))
        self.journal=self.root/"output"/(
            f"quote_observation_journal_v2_api_opening_{self.closed}")/(
            f"{self.closed}.sqlite3")
        self.journal.parent.mkdir(parents=True)
        self.journal.write_bytes(b"test journal sentinel")
        self.executor=Mock(return_value=SimpleNamespace(returncode=1))

    def exercise_operator(self,answers):
        values=iter(answers)
        return run_operator_closeout(
            root=self.root,closed_date=self.closed,now=self.now,
            ask=lambda _:next(values),run=self.executor)

    def test_confirmed_attended_launch_uses_one_command_no_password_capture(self):
        self.assertEqual(self.exercise_operator(["2026-11-04","YES"]),1)
        self.executor.assert_called_once()
        command=self.executor.call_args.args[0]
        self.assertIn("run_daily_closeout.py",str(command[1]))
        self.assertEqual(command.count("--closed-session"),1)
        self.assertEqual(command.count("--next-session"),1)
        self.assertEqual(command[-1],str(self.root))
        self.assertNotIn("password",str(command).lower())

    def test_declined_launch_never_calls_provider(self):
        self.assertEqual(self.exercise_operator(["2026-11-04","no"]),2)
        self.executor.assert_not_called()

    def test_invalid_date_fails_before_any_launch(self):
        with self.assertRaisesRegex(ValueError,"Invalid"):
            self.exercise_operator(["tomorrow"])
        self.executor.assert_not_called()

    def test_same_date_rejected(self):
        with self.assertRaisesRegex(ValueError,"later"):
            self.exercise_operator(["2026-11-03"])
        self.executor.assert_not_called()

    def test_weekend_rejected(self):
        with self.assertRaisesRegex(ValueError,"weekend"):
            self.exercise_operator(["2026-11-07"])
        self.executor.assert_not_called()

    def test_before_close_rejected(self):
        self.now=self.now.replace(hour=15,minute=59)
        with self.assertRaisesRegex(ValueError,"16:00"):
            self.exercise_operator([])
        self.executor.assert_not_called()

    def test_other_source_date_rejected(self):
        self.now=self.now+timedelta(days=1)
        with self.assertRaisesRegex(ValueError,"source trading date"):
            self.exercise_operator([])
        self.executor.assert_not_called()

    def test_missing_journal_rejected(self):
        self.journal.unlink()
        with self.assertRaisesRegex(ValueError,"journal"):
            self.exercise_operator([])
        self.executor.assert_not_called()

    def test_confirmation_exactly_yes(self):
        self.assertEqual(self.exercise_operator(["2026-11-04","yes"]),2)
        self.executor.assert_not_called()
