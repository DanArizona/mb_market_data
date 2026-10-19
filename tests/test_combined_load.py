"""Integration-harness boundaries and policy inputs; timing proof is a separate command."""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import sqlite3
from contextlib import closing
from threading import Event
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'experiments/combined_load_20261019'))
import combined_load as review


class PlanTests(unittest.TestCase):
    def test_real_cadences_representative_sizes_and_unchanged_deadline(self):
        config = review.plan(300)
        self.assertEqual(len(config['opening_uni']), 554)
        self.assertEqual(len(config['opening_focus']), 40)
        self.assertEqual(len(config['initial_hot']), 40)
        self.assertNotIn(config['outside_symbol'], config['opening_uni'])
        self.assertEqual(config['grids']['uni'], [0, 30])
        self.assertEqual(config['grids']['focus'], [5, 20, 35, 50])
        self.assertEqual(config['grids']['hot'], list(range(0, 60, 2)))
        self.assertEqual(config['dispatch_deadline_seconds'], .75)

    def test_only_bounded_presets_are_accepted(self):
        for invalid in (True, 0, 60.0, 180, 600, 3600):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                review.plan(invalid)

    def test_slot_plan_is_half_open_and_has_expected_counts(self):
        for duration, counts in ((60, (2, 4, 30)), (300, (10, 20, 150))):
            config = review.plan(duration)
            config.update(start='2026-10-21T14:00:00+00:00', end=f'2026-10-21T14:0{duration // 60}:00+00:00')
            for channel, count in zip(('uni', 'focus', 'hot'), counts):
                slots = review.expected_slots(config, channel)
                self.assertEqual(len(slots), count)
                self.assertEqual(len(slots), len(set(slots)))
                self.assertLess(datetime.fromisoformat(slots[-1].split(':', 1)[1]), datetime.fromisoformat(config['end']))

    def test_network_is_blocked_in_synthetic_entry_process(self):
        with patch.object(socket.socket, 'connect'), patch.object(socket.socket, 'connect_ex'), \
             patch.object(socket, 'create_connection'), patch.object(socket, 'getaddrinfo'):
            review.deny_network()
            with self.assertRaisesRegex(RuntimeError, 'disabled'):
                socket.create_connection(('example.invalid', 443))

    def test_stop_rejects_foreign_pointer_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'runs').mkdir()
            target = root / 'production'; target.mkdir()
            review.write_json(root / 'runs/active_combined.json', {'contract': review.CONTRACT, 'folder': str(target)})
            with self.assertRaisesRegex(RuntimeError, 'escapes'):
                review.request_stop(root)
            self.assertFalse((target / 'STOP').exists())

    def test_partial_receipt_line_is_not_available_until_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'status.jsonl'
            path.write_text('{"status":"ready"}\n{"status":', encoding='utf-8')
            self.assertEqual(review.lines(path), [{'status': 'ready'}])

    def test_poller_can_finish_after_its_last_slot_before_common_end(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            config = review.plan(60)
            config.update(start='2026-10-21T14:00:00+00:00', end='2026-10-21T14:01:00+00:00')
            review.append_line(folder / 'uni_status.jsonl', {'status': 'window_ended'})
            children = {'uni': SimpleNamespace(poll=lambda: 0)}
            review.check_children(folder, children, config, lambda: datetime.fromisoformat('2026-10-21T14:00:31+00:00'))
            with self.assertRaisesRegex(RuntimeError, 'unexpectedly'):
                review.check_children(folder, children, config, lambda: datetime.fromisoformat('2026-10-21T14:00:01+00:00'))

    def test_verification_does_not_accept_changed_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'runs/combined/one'; folder.mkdir(parents=True)
            source = root / 'experiments/combined_load_20261019'; source.mkdir(parents=True)
            (source / 'source_manifest.json').write_text('{}', encoding='utf-8')
            report = folder / 'report.json'; report.write_text('{}', encoding='utf-8')
            review.write_json(root / 'runs/combined_verification.json', {'result': 'PASS',
                'source_manifest_sha256': hashlib.sha256(b'{}').hexdigest(), 'report': str(report), 'report_sha256': '0' * 64})
            with self.assertRaisesRegex(RuntimeError, 'report changed'):
                review.check_verification(root)

    def test_dependency_pin_rejects_modified_source_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'experiments/combined_load_20261019'; source.mkdir(parents=True)
            package = root / 'coordinator_src/mb_watchlist_coordinator'; package.mkdir(parents=True)
            (package / 'one.py').write_text('changed', encoding='utf-8')
            review.write_json(source / 'coordinator_pin.json', {'commit': review.COORDINATOR_COMMIT, 'files': {'one.py': '0' * 64}})
            with self.assertRaisesRegex(RuntimeError, 'source differs'):
                review.verify_coordinator(root)


@unittest.skipUnless((ROOT / 'coordinator_src/mb_watchlist_coordinator').is_dir(), 'Requires the staged pinned Coordinator dependency')
class PolicyInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        review.bootstrap()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'fresh'
        self.config = review.prepare(self.folder, 60)
        self.ledger, self.service = review.load_control(self.folder)

    def test_common_admission_and_independent_withdrawal(self):
        target = self.config['outside_symbol']
        review.submit(self.ledger, self.config, 'manual', 'outside-manual', [target], 'hot')
        review.submit(self.ledger, self.config, 'synthetic-halt', 'outside-halt', [target], 'hot')
        self.ledger.cancel_request('withdraw', owner_id='manual', request_id='outside-manual', actor_id='manual')
        state = self.ledger.states()[-1]
        self.assertIn(target, state.applied.uni)
        self.assertIn(target, state.applied.focus)
        self.assertIn(target, state.applied.hot)
        self.assertEqual(len(state.applied.uni), 555)

    def test_cap_preserves_demand_and_session_uni(self):
        target = self.config['outside_symbol']
        review.submit(self.ledger, self.config, 'manual', 'outside', [target], 'hot')
        self.ledger.restrict_symbol('cap', symbol=target, max_group='uni', actor_id='review-super-user')
        state = self.ledger.states()[-1]
        self.assertIn(target, state.applied.uni)
        self.assertNotIn(target, state.applied.focus)
        self.assertNotIn(target, state.applied.hot)
        self.ledger.clear_restriction('clear', symbol=target, restriction_id='cap', actor_id='review-super-user')
        self.assertIn(target, self.ledger.states()[-1].applied.hot)

    def test_reused_or_changed_configuration_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'fresh'):
            review.prepare(self.folder, 60)
        config = review.read_json(self.folder / 'config.json')
        config['duration_seconds'] = 3600
        review.write_json(self.folder / 'config.json', config)
        with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
            review.load_config(self.folder)

    def test_stop_targets_only_identity_bound_experiment(self):
        with self.assertRaisesRegex(RuntimeError, 'direct experiment'):
            review.validate_run_path(self.folder)
        source = review.read_json(self.folder / 'identity.json')
        self.assertEqual(source['contract'], review.CONTRACT)

    def test_final_snapshot_does_not_register_an_unused_polling_run(self):
        with closing(sqlite3.connect(self.folder / 'quotes.sqlite3')) as connection:
            before = connection.execute('SELECT count(*) FROM poll_run').fetchone()[0]
        poller, _, _ = review.load_poller(self.folder, 'hot', Event(), initialize=False)
        self.assertIsNone(poller.snapshot()['pending'])
        with closing(sqlite3.connect(self.folder / 'quotes.sqlite3')) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM poll_run').fetchone()[0], before)


if __name__ == '__main__':
    unittest.main()
