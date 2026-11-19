"""Full offline UI-to-worker exercise against a private synthetic journal."""
from datetime import datetime, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from mb_market_data.daily_operator_demo import DemoSession, seed_demo
from mb_market_data.daily_operator_ui import create_server, membership_snapshot


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='membership-demo-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'synthetic_only'

    def test_seeded_journal_is_explicitly_synthetic_and_readable(self):
        plan = seed_demo(self.root)
        self.assertTrue(plan['operator_demo_synthetic'])
        self.assertNotIn('ecfg', plan)
        self.assertFalse(plan['automatic_nasdaq'])
        view = membership_snapshot(self.root, selected='INTC')
        self.assertTrue(view['synthetic_demo'])
        self.assertEqual(view['effective_revision'], 2)
        self.assertEqual(view['counts'], {'uni': 18, 'focus': 6, 'hot': 4})
        self.assertIsNotNone(view['selected']['quote_time_utc'])
        self.assertIsNotNone(view['selected']['api_response_utc'])
        self.assertEqual(view['selected']['description'], 'SYNTHETIC INTC TEST SECURITY')
        self.assertFalse(view['eligible'], 'No simulated worker is running yet')

    def test_existing_directory_is_never_overwritten(self):
        self.root.mkdir()
        (self.root / 'sentinel.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'empty directory'):
            seed_demo(self.root)
        self.assertEqual((self.root / 'sentinel.txt').read_text(), 'keep')

    def test_midnight_et_fixture_does_not_cross_session_identity(self):
        # 00:02 ET during standard time, when a naive -2 minute opening
        # would otherwise belong to the preceding market date.
        at = datetime(2026, 11, 19, 5, 2, 20, tzinfo=timezone.utc)
        seed_demo(self.root, at=at)
        view = membership_snapshot(self.root, selected='MSFT', clock=lambda: at)
        self.assertEqual(view['session_date'], '2026-11-19')
        self.assertEqual(view['selected']['tier'], 'hot')

    def test_local_http_preview_confirm_effective_and_shutdown(self):
        demo = DemoSession(self.root)
        demo.start()
        self.addCleanup(demo.close)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            status = self.root / 'membership' / 'status.json'
            if status.exists():
                value = json.loads(status.read_text())
                if value['state'] == 'RUNNING':
                    break
            time.sleep(.05)
        else:
            self.fail('Synthetic membership worker did not start')
        server = create_server(self.root, port=0)
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        self.addCleanup(conn.close)

        def exchange(method, url, data=None, headers=None):
            payload = json.dumps(data) if data is not None else None
            conn.request(method, url, body=payload, headers=headers or {})
            response = conn.getresponse()
            code = response.status
            return code, json.loads(response.read())

        code, settings = exchange('GET', '/config')
        self.assertEqual(code, 200)
        headers = {'Content-Type':'application/json',
                   'X-MB-Operator-CSRF': settings['csrf'],
                   'Origin': f'http://127.0.0.1:{server.server_port}'}
        code, before = exchange('GET', '/snapshot?symbol=INTC')
        self.assertEqual(code, 200)
        self.assertTrue(before['eligible'])
        self.assertTrue(before['synthetic_demo'])
        self.assertEqual(before['selected']['tier'], 'hot')
        code, proposal = exchange('POST', '/preview',
            {'symbol':'INTC','action':'demote','target':'focus'}, headers)
        self.assertEqual(code, 200, proposal)
        self.assertFalse(list((self.root / 'membership' / 'inbox').glob('*.json')),
                         'Preview must not queue a command')
        code, queued = exchange('POST', '/confirm',
            {'proposal_id':proposal['proposal_id']}, headers)
        self.assertEqual(code, 200, queued)
        cid = queued['request']['id']
        self.assertTrue(cid.startswith('manual-'))
        # No fake write directly edits the journal: the real shared service
        # processes the inbox and publishes its future-effective revision.
        result = None
        deadline = time.monotonic() + 9
        while time.monotonic() < deadline:
            code, result = exchange('GET', '/result?id='+cid)
            self.assertEqual(code, 200)
            if result['status'] == 'EFFECTIVE_AS_PUBLISHED':
                break
            self.assertNotIn(result['status'], ('FAILED','REJECTED','NOT_FOUND'))
            time.sleep(.1)
        self.assertEqual(result['status'], 'EFFECTIVE_AS_PUBLISHED')
        code, after = exchange('GET', '/snapshot?symbol=INTC')
        self.assertEqual(code, 200)
        self.assertEqual(after['selected']['tier'], 'focus')
        self.assertEqual(after['effective_revision'], 3)

        # Re-promotion requires its own separate preview/confirmation and
        # a second revision. Neither step talks to a market-data provider.
        code, second = exchange('POST', '/preview',
            {'symbol':'INTC','action':'promote','target':'hot'}, headers)
        self.assertEqual(code, 200, second)
        code, second_queued = exchange('POST', '/confirm',
            {'proposal_id':second['proposal_id']}, headers)
        self.assertEqual(code, 200, second_queued)
        second_id = second_queued['request']['id']
        self.assertNotEqual(second_id, cid)
        deadline = time.monotonic() + 9
        while time.monotonic() < deadline:
            code, result = exchange('GET', '/result?id='+second_id)
            self.assertEqual(code, 200)
            if result['status'] == 'EFFECTIVE_AS_PUBLISHED':
                break
            self.assertNotIn(result['status'], ('FAILED','REJECTED','NOT_FOUND'))
            time.sleep(.1)
        self.assertEqual(result['status'], 'EFFECTIVE_AS_PUBLISHED')
        code, restored = exchange('GET', '/snapshot?symbol=INTC')
        self.assertEqual(code, 200)
        self.assertEqual(restored['selected']['tier'], 'hot')
        self.assertEqual(restored['effective_revision'], 4)
        demo.close()
        read_only = membership_snapshot(self.root, selected='INTC')
        self.assertFalse(read_only['eligible'], 'Shutdown disables further commands')


if __name__ == '__main__':
    unittest.main(verbosity=2)
