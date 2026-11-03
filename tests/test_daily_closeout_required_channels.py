"""Strict daily-closeout presence and coverage checks, offline."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from probes.audit_required_session_channels import (
    require_configured_session, audit_completed_session)


class RequiredPollingSessionTests(TestCase):
    def setUp(self):
        self.day=date(2026,11,3)
        self.uni=SimpleNamespace(channel="uni",revision_count=2,
            expected_slot_count=780,acquisition_count=780,skipped_slot_count=0,
            missing_slot_ids=())
        self.focus=SimpleNamespace(channel="focus",revision_count=2,
            expected_slot_count=1560,acquisition_count=1559,skipped_slot_count=0,
            missing_slot_ids=("focus:2026-11-03T13:02:50-05:00",))
        self.report=SimpleNamespace(session_date=self.day.isoformat(),
            schema_version=2,membership_contract="nested-uni-focus-hot-v1",
            runs=(SimpleNamespace(channel="uni"),SimpleNamespace(channel="focus")),
            channels=(self.uni,self.focus),findings=(SimpleNamespace(code="missing_poll_slots"),),
            passed=False)

    def test_real_missing_focus_slot_still_qualifies_as_a_recorded_audit_fail(self):
        require_configured_session(self.report,self.day)
        self.assertFalse(self.report.passed)
        self.assertEqual(len(self.focus.missing_slot_ids),1)

    def test_no_poll_runs_never_count_as_completed(self):
        self.report.runs=()
        self.uni.expected_slot_count=0
        self.focus.expected_slot_count=0
        with self.assertRaisesRegex(ValueError,"poll runs"):
            require_configured_session(self.report,self.day)

    def test_missing_focus_run_is_incomplete_not_no_missing_slots(self):
        self.report.runs=(SimpleNamespace(channel="uni"),)
        with self.assertRaisesRegex(ValueError,"poll runs"):
            require_configured_session(self.report,self.day)

    def test_wrong_cadence_slot_count_blocks(self):
        self.focus.expected_slot_count=780
        with self.assertRaisesRegex(ValueError,"expected 1560"):
            require_configured_session(self.report,self.day)

    def test_inconsistent_acquisition_and_gap_accounting_blocks(self):
        self.focus.acquisition_count=1558
        with self.assertRaisesRegex(ValueError,"accounting"):
            require_configured_session(self.report,self.day)

    def test_empty_revision_count_blocks(self):
        self.uni.revision_count=0
        with self.assertRaisesRegex(ValueError,"revisions"):
            require_configured_session(self.report,self.day)

    def test_wrong_journal_date_blocks(self):
        self.report.session_date="2026-11-02"
        with self.assertRaisesRegex(ValueError,"session"):
            require_configured_session(self.report,self.day)

    def test_before_close_grace_refuses_even_with_valid_journal(self):
        with patch('probes.audit_required_session_channels.audit_quote_journal') as read:
            with self.assertRaisesRegex(ValueError,"grace"):
                audit_completed_session(Path("synthetic.sqlite3"),self.day,
                    clock=lambda:datetime(2026,11,3,16,0,30,
                        tzinfo=timezone(timedelta(hours=-5))))
            read.assert_not_called()

    def test_after_close_grace_preserves_saved_missing_slot(self):
        with patch('probes.audit_required_session_channels.audit_quote_journal',
                   return_value=self.report) as read:
            result=audit_completed_session(Path("synthetic.sqlite3"),self.day,
                clock=lambda:datetime(2026,11,3,17,0,
                    tzinfo=timezone(timedelta(hours=-5))))
            self.assertIs(result,self.report)
            read.assert_called_once()
