"""Offline status contract for combined attended closeout.

All three subprocesses are mocked. The actual post-close builder, the
production journal, credentials, network and monitor are never used.
"""
from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock

from probes.run_daily_closeout import combine_verdicts, run_daily_closeout


class CombinedDailyCloseoutTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.closed = date(2026, 11, 3)
        self.target = date(2026, 11, 4)
        self.now = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)

    def exercise(self, codes):
        called = []
        def run(command, *, cwd):
            called.append((command, cwd))
            return SimpleNamespace(returncode=codes[len(called)-1])
        result = run_daily_closeout(self.root, self.closed, self.target,
            runner=run, clock=lambda: self.now, program_root=self.root)
        path = Path(result['receipt_path'])
        self.assertTrue(path.is_file())
        stored = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(stored['verdict'], result['verdict'])
        self.assertEqual(stored['stages_return_codes'], result['stages_return_codes'])
        self.assertEqual(len(called), 3)
        self.assertEqual([Path(command[1]).name for command, _ in called],
            ['prepare_next_session_closeout.py',
             'audit_required_session_channels.py',
             'replay_quote_observation_journal.py'])
        self.assertTrue(all(cwd == self.root for _, cwd in called))
        return result

    def test_all_three_checks_pass_gives_verified_complete(self):
        result = self.exercise([0, 0, 0])
        self.assertEqual(result['verdict'], 'VERIFIED_COMPLETE')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['current_session_audit'], 'PASS')

    def test_real_coverage_failure_is_retained_as_findings(self):
        result = self.exercise([0, 1, 0])
        self.assertEqual(result['verdict'], 'RECORDED_WITH_FINDINGS')
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['current_session_audit'], 'FAIL')
        self.assertEqual(result['current_session_replay'], 'COMPLETE')
        self.assertEqual(result['next_session_preparation'], 'READY')

    def test_missing_preparation_never_becomes_complete(self):
        result = self.exercise([2, 0, 0])
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['exit_code'], 2)
        self.assertEqual(result['next_session_preparation'], 'INCOMPLETE')

    def test_failed_replay_keeps_closeout_incomplete(self):
        result = self.exercise([0, 0, 2])
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['current_session_replay'], 'ERROR')

    def test_audit_error_is_not_treated_as_legitimate_fail(self):
        result = self.exercise([0, 2, 0])
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['current_session_audit'], 'ERROR')

    def test_invalid_date_does_not_launch_any_subprocess(self):
        runner = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'follow'):
            run_daily_closeout(self.root, self.closed, self.closed,
                runner=runner, clock=lambda:self.now)
        runner.assert_not_called()

    def test_combine_verdicts_keeps_error_separate_from_findings(self):
        self.assertEqual(combine_verdicts(0, 1, 0), ('RECORDED_WITH_FINDINGS', 1))
        self.assertEqual(combine_verdicts(0, 0, 0), ('VERIFIED_COMPLETE', 0))
        for codes in ((0, 2, 0), (0, 0, 2), (2, 0, 0), (1, 1, 0)):
            self.assertEqual(combine_verdicts(*codes), ('INCOMPLETE', 2))

    def test_verify_only_uses_candidate_scripts_and_separate_receipt(self):
        experimental = self.root / 'candidate'
        data_root = self.root / 'production_evidence'
        experimental.mkdir()
        data_root.mkdir()
        external_receipts = self.root / 'isolated_receipts'
        called = []
        def run(command, *, cwd):
            called.append((command, cwd))
            return SimpleNamespace(returncode=(0, 1, 0)[len(called)-1])
        result = run_daily_closeout(
            data_root, self.closed, self.target,
            program_root=experimental, verify_only=True,
            receipt_root=external_receipts, runner=run,
            clock=lambda: self.now)
        self.assertEqual(result['verdict'], 'RECORDED_WITH_FINDINGS')
        self.assertEqual(result['mode'], 'VERIFY_ONLY')
        self.assertEqual(result['production_writes'], 'NONE__READ_ONLY_EVIDENCE')
        self.assertEqual([Path(c[0][1]).name for c in called],
            ['verify_next_session_readiness.py',
             'audit_required_session_channels.py',
             'replay_quote_observation_journal.py'])
        self.assertTrue(all(cwd == experimental for _, cwd in called))
        self.assertEqual(Path(result['receipt_path']).parent, external_receipts)
        self.assertFalse((data_root / 'output' / 'production_closeout').exists())

    def test_attended_mode_denies_cross_checkout_production_root(self):
        experimental = self.root / 'candidate'
        external = self.root / 'production_evidence'
        executor = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'requires installed program root'):
            run_daily_closeout(
                external, self.closed, self.target,
                program_root=experimental, runner=executor,
                clock=lambda: self.now)
        executor.assert_not_called()

    def test_verify_only_default_receipt_is_under_candidate_not_production(self):
        experimental = self.root / 'candidate'
        data_root = self.root / 'production_evidence'
        experimental.mkdir()
        data_root.mkdir()
        called = []
        def run(command, *, cwd):
            called.append(command)
            return SimpleNamespace(returncode=0)
        result = run_daily_closeout(
            data_root, self.closed, self.target,
            program_root=experimental, verify_only=True,
            runner=run, clock=lambda: self.now)
        self.assertEqual(result['verdict'], 'VERIFIED_COMPLETE')
        self.assertTrue(str(result['receipt_path']).startswith(str(experimental)))
        self.assertFalse((data_root / 'output').exists())
