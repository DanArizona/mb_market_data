"""Offline missed-closeout classifications, no provider or journal writes."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from probes.assess_closeout_recovery import assess


class CloseoutRecoveryTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.closed=date(2026,11,3)
        self.next=date(2026,11,4)
        self.workflow=self.root/"output"/"daily_universe_production"/(
            "2026-11-04-from-2026-11-03")
        self.afternoon=datetime(2026,11,3,17,20,
            tzinfo=timezone(timedelta(hours=-5)))
        self.next_morning=datetime(2026,11,4,8,10,
            tzinfo=timezone(timedelta(hours=-5)))

    def check(self,when):
        with patch("probes.assess_closeout_recovery.inspect_preparation",
                   side_effect=FileNotFoundError("missing workflow")):
            return assess(self.root,self.closed,self.next,now=when)

    def test_missed_entire_snapshot_is_explicit_recovery_required(self):
        state=self.check(self.next_morning)
        self.assertEqual(state.state,"RECOVERY_REQUIRED")
        self.assertFalse(state.retained_snapshot_manifest)
        self.assertIn("do NOT acquire fresh Schwab",state.detail)

    def test_missed_with_saved_snapshot_requires_review_not_automatic_rebuild(self):
        file=self.workflow/"market_snapshot"/"manifest.json"
        file.parent.mkdir(parents=True)
        file.write_text("{}",encoding="utf-8")
        state=self.check(self.next_morning)
        self.assertEqual(state.state,"RECOVERY_REQUIRED")
        self.assertTrue(state.retained_snapshot_manifest)
        self.assertIn("qualified",state.detail)

    def test_day_of_close_before_midnight_can_still_take_action(self):
        state=self.check(self.afternoon)
        self.assertEqual(state.state,"DUE_BEFORE_ET_MIDNIGHT")

    def test_not_before_regular_close(self):
        state=self.check(self.afternoon.replace(hour=15,minute=59))
        self.assertEqual(state.state,"NOT_YET_DUE")

    def test_verified_next_day_is_ready_even_after_midnight(self):
        with patch("probes.assess_closeout_recovery.inspect_preparation",
            return_value={"uni_members":555}):
            state=assess(self.root,self.closed,self.next,now=self.next_morning)
        self.assertEqual(state.state,"READY")

    def test_invalid_date_is_rejected_before_evidence_access(self):
        with patch("probes.assess_closeout_recovery.inspect_preparation") as verify:
            with self.assertRaisesRegex(ValueError,"later"):
                assess(self.root,self.closed,self.closed,now=self.next_morning)
            verify.assert_not_called()
