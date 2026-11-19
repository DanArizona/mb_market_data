"""Offline validation of a separate local membership operator window."""
from datetime import timedelta
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from unittest.mock import patch

from mb_market_data import daily_operator_ui as ui
from mb_market_data.acquisition_health import atomic_json
from test_daily_membership import Fixture


class MembershipOperatorTests(Fixture):
    def setUp(self):
        super().setUp()
        atomic_json(self.state / 'owner.json', {'pid': 9817, 'active': True, 'at': self.at.isoformat()})
        (self.state / 'membership').mkdir(exist_ok=True)
        atomic_json(self.state / 'membership' / 'status.json', {
            'state': 'RUNNING', 'at': self.at.isoformat(), 'pid': 9021, 'owner_pid': 9817})
        atomic_json(self.state / 'status.json', {'phase': 'COLLECTING', 'at': self.at.isoformat()})
        available = patch.object(ui, 'owner_available', return_value=True)
        available.start()
        self.addCleanup(available.stop)
        self.state_obj = ui.OperatorState(self.state, clock=lambda: self.at)

    def test_three_exclusive_tiers_preserve_nested_roster(self):
        view = ui.membership_snapshot(self.state, selected='INTC', clock=lambda: self.at)
        self.assertEqual(view['effective_revision'], 2)
        self.assertEqual(view['tiers'], {'uni': ['DEF'], 'focus': ['ABC'],
                                        'hot': ['INTC', 'MSFT', 'NVDA']})
        self.assertEqual(view['selected']['tier'], 'hot')
        self.assertEqual(view['phase'], 'COLLECTING')
        self.assertTrue(view['read_only_snapshot'])

    def test_snapshot_and_preview_do_not_write_a_journal_or_inbox(self):
        journal = Path(self.plan['journal'])
        before = journal.read_bytes()
        view = ui.membership_snapshot(self.state, selected='DEF', clock=lambda: self.at)
        proposal = self.state_obj.preview('DEF', 'promote', 'hot')
        self.assertEqual(view['selected']['tier'], 'uni')
        self.assertEqual(proposal['after'], 'hot')
        self.assertFalse(proposal['request_written'])
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse((self.state / 'membership' / 'inbox').exists())
        self.assertEqual(self.client.calls, [])

    def test_published_future_change_is_not_displayed_early(self):
        self.apply('INTC', 'focus', 'demote')
        pending = ui.membership_snapshot(self.state, selected='INTC', clock=lambda: self.at)
        self.assertEqual(pending['selected']['tier'], 'hot')
        self.assertEqual(pending['effective_revision'], 2)
        self.assertEqual(pending['pending_revision']['revision'], 3)
        with self.assertRaisesRegex(ui.OperatorProblem, 'Wait for the pending'):
            self.state_obj.preview('INTC', 'demote', 'uni')
        self.step()
        effective = ui.membership_snapshot(self.state, selected='INTC', clock=lambda: self.at)
        self.assertEqual(effective['selected']['tier'], 'focus')
        self.assertIsNone(effective['pending_revision'])

    def test_ordinary_demotion_and_repromotion_are_reviewed_separately(self):
        demote = self.state_obj.preview('INTC', 'demote', 'uni')
        self.assertEqual((demote['before'], demote['after']), ('hot', 'uni'))
        self.assertIn('later', demote['note'])
        promote = self.state_obj.preview('DEF', 'promote', 'focus')
        self.assertEqual((promote['before'], promote['after']), ('uni', 'focus'))

    def test_redundant_promotion_and_illegal_demotion_rejected(self):
        with self.assertRaisesRegex(ui.OperatorProblem, 'already at or above'):
            self.state_obj.preview('INTC', 'promote', 'focus')
        with self.assertRaisesRegex(ui.OperatorProblem, 'Demotion requires'):
            self.state_obj.preview('DEF', 'demote', 'focus')
        with self.assertRaisesRegex(ui.OperatorProblem, 'Choose promote'):
            self.state_obj.preview('INTC', 'demote', 'hot')

    def test_stale_worker_or_stop_refuses_preview(self):
        atomic_json(self.state / 'membership' / 'status.json', {
            'state': 'RUNNING', 'owner_pid': 9817, 'at': (self.at-timedelta(seconds=31)).isoformat()})
        with self.assertRaisesRegex(ui.OperatorProblem, 'heartbeat'):
            self.state_obj.preview('INTC', 'demote', 'focus')
        atomic_json(self.state / 'membership' / 'status.json', {
            'state': 'RUNNING', 'owner_pid': 9817, 'at': self.at.isoformat()})
        (self.state / 'stop.request').touch()
        with self.assertRaisesRegex(ui.OperatorProblem, 'stop'):
            self.state_obj.preview('INTC', 'demote', 'focus')

    def test_finished_window_and_non_opted_in_plan_are_read_only(self):
        self.at = self.close
        finished = ui.membership_snapshot(self.state, selected='INTC', clock=lambda: self.at)
        self.assertFalse(finished['eligible'])
        with self.assertRaisesRegex(ui.OperatorProblem, 'active collection'):
            self.state_obj.preview('INTC', 'demote', 'focus')
        self.at = self.open + timedelta(minutes=10)
        plan = dict(self.plan)
        plan.pop('membership_control')
        atomic_json(self.state / 'plan.json', plan)
        inactive = ui.membership_snapshot(self.state, selected='INTC', clock=lambda: self.at)
        self.assertFalse(inactive['eligible'])
        with self.assertRaisesRegex(ui.OperatorProblem, 'not launched'):
            self.state_obj.preview('INTC', 'demote', 'focus')

    def test_changed_revision_after_preview_cannot_be_confirmed(self):
        pending = self.state_obj.preview('INTC', 'demote', 'uni')
        self.apply('INTC', 'focus', 'demote')
        with patch.object(ui, 'submit') as submit:
            with self.assertRaisesRegex(ui.OperatorProblem, 'revision'):
                self.state_obj.confirm(pending['proposal_id'])
            submit.assert_not_called()

    def test_confirm_calls_only_existing_submit_and_is_idempotent(self):
        proposal = self.state_obj.preview('INTC', 'demote', 'focus')
        request = {'id': 'manual-'+'a'*32, 'action': 'demote', 'target': 'focus', 'symbol': 'INTC'}
        with patch.object(ui, 'submit', return_value=request) as submit:
            first = self.state_obj.confirm(proposal['proposal_id'])
            second = self.state_obj.confirm(proposal['proposal_id'])
        self.assertEqual(first['request']['id'], request['id'])
        self.assertTrue(second['already_submitted'])
        submit.assert_called_once_with(self.state, action='demote', symbol='INTC',
                                       target='focus', clock=self.state_obj.clock)

    def test_full_offline_preview_confirm_worker_receipt_and_effectiveness(self):
        """Exercise the real inbox/writer; no network or authenticated client."""
        proposal = self.state_obj.preview('INTC', 'demote', 'focus')
        journal = Path(self.plan['journal'])
        before = journal.read_bytes()
        with patch('mb_market_data.daily_runtime.owner_available', return_value=True):
            response = self.state_obj.confirm(proposal['proposal_id'])
        request = response['request']
        inbox = self.state / 'membership' / 'inbox' / (request['id'] + '.json')
        self.assertTrue(inbox.is_file())
        self.assertEqual(journal.read_bytes(), before, 'UI request must not write journal')
        self.assertEqual(ui.report_result(self.state, request['id'], clock=lambda: self.at)['status'], 'PENDING')
        receipt = self.engine.apply(ui.read(inbox))
        self.assertEqual(receipt['status'], 'PUBLISHED_PENDING_EFFECTIVE')
        self.assertEqual(ui.report_result(self.state, request['id'], clock=lambda: self.at)['status'],
                         'PUBLISHED_PENDING_EFFECTIVE')
        self.assertEqual(ui.membership_snapshot(self.state, selected='INTC', clock=lambda: self.at)['selected']['tier'], 'hot')
        self.step(10)
        self.assertEqual(ui.report_result(self.state, request['id'], clock=lambda: self.at)['status'],
                         'EFFECTIVE_AS_PUBLISHED')
        self.assertEqual(ui.membership_snapshot(self.state, selected='INTC', clock=lambda: self.at)['selected']['tier'], 'focus')
        self.assertEqual(self.client.calls, [])

    def test_server_is_loopback_and_requires_local_origin_and_csrf(self):
        server = ui.create_server(self.state, port=0, clock=lambda: self.at)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        self.addCleanup(conn.close)

        def exchange(method, path, payload=None, headers=None):
            body=json.dumps(payload) if payload is not None else None
            conn.request(method, path, body=body, headers=headers or {})
            response=conn.getresponse()
            return response.status, response.read()

        code, html = exchange('GET', '/')
        self.assertEqual(code, 200)
        self.assertIn(b'Confirm and queue request', html)
        conn.putrequest('GET', '/config', skip_host=True)
        conn.putheader('Host', 'untrusted.example')
        conn.endheaders()
        untrusted = conn.getresponse()
        self.assertEqual(untrusted.status, 403)
        untrusted.read()
        code, content = exchange('GET', '/snapshot?symbol=INTC')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(content)['selected']['tier'], 'hot')
        code, content = exchange('GET', '/config')
        csrf=json.loads(content)['csrf']
        action={'symbol':'INTC','action':'demote','target':'focus'}
        code, _ = exchange('POST', '/preview', action, {'Content-Type':'application/json'})
        self.assertEqual(code, 403)
        headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{server.server_port}',
                 'X-MB-Operator-CSRF':csrf}
        code, content=exchange('POST', '/preview', action, headers)
        self.assertEqual(code, 200, content)
        preview=json.loads(content)
        with patch.object(ui, 'submit', return_value={'id':'manual-'+'b'*32}) as submit:
            code, content=exchange('POST', '/confirm', {'proposal_id':preview['proposal_id']}, headers)
            self.assertEqual(code, 200, content)
            self.assertEqual(json.loads(content)['request']['id'], 'manual-'+'b'*32)
            submit.assert_called_once()


if __name__=='__main__':
    import unittest
    unittest.main(verbosity=2)
