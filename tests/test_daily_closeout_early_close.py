"""November 27, 2026 shortened-session closeout workflow integration tests.

No real journal, provider, password or Task Scheduler access.
"""
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from mb_market_data.equity_closeout_calendar import ET
from probes.closeout_readiness_reminder import inspect_due
from probes.attended_closeout_today import run_operator_closeout
from probes.prepare_next_session_closeout import run_closeout_preparation
from probes.audit_required_session_channels import require_configured_session


class EarlyCloseoutIntegrationTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=date(2026,11,27)
        self.target=date(2026,11,30)
        self.time=datetime(2026,11,27,13,15,tzinfo=ET)
        self.journal=self.root/"output"/(
            "quote_observation_journal_v2_api_opening_2026-11-27")/"2026-11-27.sqlite3"
        self.journal.parent.mkdir(parents=True)
        self.journal.write_bytes(b"synthetic journal presence")

    def test_before_1310_reminder_not_due(self):
        result=inspect_due(self.root,now=self.time.replace(minute=9))
        self.assertEqual(result.status,"NOT_DUE")
        self.assertIn("13:10",result.explanation)

    def test_after_1310_missing_snapshot_due(self):
        result=inspect_due(self.root,now=self.time)
        self.assertEqual(result.status,"DUE")
        self.assertIn("2026-11-30",result.explanation)

    def test_early_close_operator_entrypoint_admitted_after_1300(self):
        executor=Mock(return_value=SimpleNamespace(returncode=1))
        values=iter(["2026-11-30","YES"])
        actual=run_operator_closeout(root=self.root,closed_date=self.source,
            now=self.time,ask=lambda _:next(values),run=executor)
        self.assertEqual(actual,1)
        command=executor.call_args.args[0]
        self.assertIn("2026-11-30",command)
        executor.assert_called_once()

    def test_early_close_operator_entrypoint_rejects_before_1300(self):
        executor=Mock()
        with self.assertRaisesRegex(ValueError,"13:00"):
            run_operator_closeout(root=self.root,closed_date=self.source,
                now=self.time.replace(hour=12,minute=59),
                ask=lambda _: "2026-11-30",run=executor)
        executor.assert_not_called()

    def test_early_close_preparation_uses_single_builder_call(self):
        executor=Mock(return_value=SimpleNamespace(returncode=0))
        evidence={"uni_members":555,"r0_content_sha256":"1"*64}
        with patch("probes.prepare_next_session_closeout.inspect_preparation",
                   return_value=evidence):
            success=run_closeout_preparation(
                self.root,self.source,self.target,
                clock=lambda:self.time,run=executor)
        self.assertTrue(success)
        self.assertIn("--publish-journal-root",executor.call_args.args[0])

    def test_early_close_audit_requires_420_and_840_slots(self):
        channels=(SimpleNamespace(channel="uni",revision_count=1,
                   expected_slot_count=420,acquisition_count=420,
                   skipped_slot_count=0,missing_slot_ids=()),
                  SimpleNamespace(channel="focus",revision_count=1,
                   expected_slot_count=840,acquisition_count=840,
                   skipped_slot_count=0,missing_slot_ids=()))
        report=SimpleNamespace(session_date=str(self.source),
            schema_version=2,membership_contract="nested-uni-focus-hot-v1",
            runs=(SimpleNamespace(channel="uni"),SimpleNamespace(channel="focus")),
            channels=channels)
        require_configured_session(report,self.source)

    def test_early_close_full_day_polling_counts_refuse_regular_day_780(self):
        channels=(SimpleNamespace(channel="uni",revision_count=1,
                   expected_slot_count=780,acquisition_count=780,
                   skipped_slot_count=0,missing_slot_ids=()),
                  SimpleNamespace(channel="focus",revision_count=1,
                   expected_slot_count=1560,acquisition_count=1560,
                   skipped_slot_count=0,missing_slot_ids=()))
        report=SimpleNamespace(session_date=str(self.source),
            schema_version=2,membership_contract="nested-uni-focus-hot-v1",
            runs=(SimpleNamespace(channel="uni"),SimpleNamespace(channel="focus")),
            channels=channels)
        with self.assertRaisesRegex(ValueError,"expected 420"):
            require_configured_session(report,self.source)
