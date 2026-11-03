"""Offline checks for attended closeout runner; no real subprocess or API."""
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from probes.prepare_next_session_closeout import run_closeout_preparation


class NextSessionCloseoutRunnerTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.closed=date(2026,11,3)
        self.target=date(2026,11,4)
        self.now=datetime(2026,11,3,17,20,tzinfo=timezone(timedelta(hours=-5)))
        self.executor=Mock(return_value=SimpleNamespace(returncode=0))
        self.workflow=self.root/'output'/'daily_universe_production'/'2026-11-04-from-2026-11-03'
        self.readiness={'uni_members':555,'r0_content_sha256':'1'*64}

    def go(self):
        return run_closeout_preparation(self.root,self.closed,self.target,
                                        clock=lambda:self.now,run=self.executor)

    def test_new_build_uses_one_existing_password_stage_and_publishes_r0(self):
        with patch('probes.prepare_next_session_closeout.inspect_preparation',
                   return_value=self.readiness) as verify:
            self.assertTrue(self.go())
        self.executor.assert_called_once()
        command=self.executor.call_args.args[0]
        self.assertEqual(command.count('--session-date'),1)
        self.assertEqual(command.count('--target-date'),1)
        self.assertIn('--publish-journal-root',command)
        self.assertIn('run_daily_universe_production.py',command[1])
        verify.assert_called_once()

    def test_preclose_refuses_before_any_subprocess(self):
        self.now=self.now.replace(hour=15,minute=59)
        with self.assertRaisesRegex(ValueError,'16:00'):
            self.go()
        self.executor.assert_not_called()

    def test_wrong_source_date_refuses_before_any_subprocess(self):
        self.now=self.now+timedelta(days=1)
        with self.assertRaisesRegex(ValueError,'source session'):
            self.go()
        self.executor.assert_not_called()

    def test_failed_builder_does_not_claim_ready(self):
        self.executor.return_value=SimpleNamespace(returncode=1)
        with patch('probes.prepare_next_session_closeout.inspect_preparation') as verify:
            self.assertFalse(self.go())
        verify.assert_not_called()

    def test_existing_directory_is_never_rebuilt(self):
        self.workflow.mkdir(parents=True)
        with patch('probes.prepare_next_session_closeout.inspect_preparation',
                   return_value=self.readiness):
            self.assertTrue(self.go())
        self.executor.assert_not_called()

    def test_incomplete_existing_directory_fails_closed_without_overwrite(self):
        self.workflow.mkdir(parents=True)
        with patch('probes.prepare_next_session_closeout.inspect_preparation',
                   side_effect=FileNotFoundError('missing manifest')):
            self.assertFalse(self.go())
        self.executor.assert_not_called()

    def test_existing_complete_workflow_can_be_verified_next_morning(self):
        self.workflow.mkdir(parents=True)
        self.now=self.now+timedelta(days=1)
        with patch('probes.prepare_next_session_closeout.inspect_preparation',
                   return_value=self.readiness):
            self.assertTrue(self.go())
        self.executor.assert_not_called()

    def test_next_session_must_be_later(self):
        with self.assertRaisesRegex(ValueError,'follow'):
            run_closeout_preparation(self.root,self.closed,self.closed,
                clock=lambda:self.now,run=self.executor)
        self.executor.assert_not_called()
