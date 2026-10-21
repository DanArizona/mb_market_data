"""No-network regressions for feed substitution, interruption and recovery."""
from datetime import timedelta
from http.client import IncompleteRead
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

HERE = Path(__file__).resolve().parents[1] / 'experiments/nasdaq_shadow_20261019'
sys.path.insert(0, str(HERE))
import shadow
import feed_capture as feed
shadow.bootstrap()

HTML = b'<html>\r\n<head><script src="/synthetic-check"></script></head>\r\n<body></body></html>'
RSS = b'<rss><channel><item><ndaq:IssueSymbol>ABC</ndaq:IssueSymbol></item></channel></rss>'


class FakeResponse(BytesIO):
    def __init__(self, body, code=200, headers=None, error=None):
        super().__init__(body)
        self.code, self.headers, self.error = code, headers or {}, error
        self.read_limit = None
    def geturl(self):
        return feed.CURRENT_FEED_URL
    def getcode(self):
        return self.code
    def read(self, limit):
        self.read_limit = limit
        if self.error:
            raise self.error
        return super().read(limit)


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []
    def open(self, request, *, timeout):
        self.calls.append((request.full_url, timeout))
        if self.error:
            raise self.error
        return self.response


class FeedTransportTests(unittest.TestCase):
    def test_fixed_request_retains_allowlisted_headers_without_cookies(self):
        response = FakeResponse(RSS, headers={'Content-Type': 'application/rss+xml',
            'Content-Length': str(len(RSS)), 'X-Iinfo': 'synthetic', 'Set-Cookie': 'SECRET', 'Authorization': 'SECRET'})
        opener = FakeOpener(response)
        value = feed.fetch_feed(opener=opener)
        self.assertEqual(opener.calls, [(feed.CURRENT_FEED_URL, 3)])
        self.assertEqual(response.read_limit, feed.MAX_BODY_BYTES + 1)
        self.assertTrue(response.closed)
        self.assertEqual(value.raw, RSS)
        self.assertNotIn('SECRET', json.dumps(value.metadata))
        self.assertEqual(feed.inspect_feed(value.raw, value.metadata)['status'], 'valid')

    def test_html_is_unavailable_even_with_http_200(self):
        value = feed.fetch_feed(opener=FakeOpener(FakeResponse(HTML)))
        self.assertEqual(value.raw, HTML)
        self.assertEqual(feed.inspect_feed(value.raw, value.metadata)['kind'], 'non_feed_html')

    def test_valid_rss_is_checked_by_body_instead_of_mislabeled_content_type(self):
        value = feed.fetch_feed(opener=FakeOpener(FakeResponse(RSS, headers={'Content-Type': 'text/html'})))
        self.assertEqual(feed.inspect_feed(value.raw, value.metadata)['status'], 'valid')

    def test_http_error_body_status_and_retry_after_are_retained(self):
        error = HTTPError(feed.CURRENT_FEED_URL, 503, 'private reason', {'Retry-After': '30'}, BytesIO(HTML))
        value = feed.fetch_feed(opener=FakeOpener(error=error))
        self.assertEqual(value.raw, HTML)
        self.assertEqual(value.metadata['status_code'], 503)
        self.assertEqual(value.metadata['headers']['Retry-After'], '30')
        self.assertEqual(feed.inspect_feed(value.raw, value.metadata)['kind'], 'server_retry_after_stop')
        self.assertTrue(feed.inspect_feed(value.raw, value.metadata)['terminal'])
        self.assertNotIn('private reason', json.dumps(value.metadata))

    def test_denial_rate_limit_and_redirect_require_stop_without_another_request(self):
        for code in (401, 403, 429, 302):
            opener = FakeOpener(FakeResponse(HTML, code=code, headers={'Location': 'https://elsewhere.invalid/'}))
            value = feed.fetch_feed(opener=opener)
            self.assertTrue(feed.inspect_feed(value.raw, value.metadata)['terminal'])
            self.assertEqual(len(opener.calls), 1)
        self.assertIsNone(feed.NoFeedRedirects().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid/'))

    def test_changed_final_url_is_terminal(self):
        value = feed.fetch_feed(opener=FakeOpener(FakeResponse(RSS)))
        value.metadata['final_url'] = 'https://elsewhere.invalid/'
        self.assertTrue(feed.inspect_feed(value.raw, value.metadata)['terminal'])

    def test_partial_response_and_transport_errors_are_explicit(self):
        value = feed.fetch_feed(opener=FakeOpener(FakeResponse(b'', error=IncompleteRead(b'<rss>', 50))))
        self.assertEqual(value.raw, b'<rss>')
        self.assertFalse(value.metadata['body_complete'])
        self.assertEqual(value.metadata['transport_error_type'], 'IncompleteRead')
        self.assertEqual(feed.inspect_feed(value.raw, value.metadata)['kind'], 'transport_error')
        value = feed.fetch_feed(opener=FakeOpener(error=TimeoutError('PRIVATE TOKEN TEXT')))
        self.assertEqual(value.metadata['transport_error_type'], 'TimeoutError')
        self.assertNotIn('PRIVATE', json.dumps(value.metadata))

    def test_oversized_body_is_bounded_and_terminal(self):
        value = feed.fetch_feed(opener=FakeOpener(FakeResponse(b'x' * (feed.MAX_BODY_BYTES + 20))))
        self.assertEqual(len(value.raw), feed.MAX_BODY_BYTES + 1)
        self.assertTrue(feed.inspect_feed(value.raw, value.metadata)['terminal'])

    def test_content_length_and_xml_structure_are_validated(self):
        value = feed.fetch_feed(opener=FakeOpener(FakeResponse(RSS, headers={'Content-Length': '9999'})))
        self.assertEqual(feed.inspect_feed(value.raw, value.metadata)['kind'], 'content_length_mismatch')
        for raw, kind in ((b'', 'empty_body'), (b'<rss><channel>', 'malformed_xml'),
                          (b'<html/>', 'non_rss_document'), (b'<rss/>', 'non_rss_document')):
            self.assertEqual(feed.inspect_feed(raw, {'kind': 'synthetic'})['kind'], kind)


class FeedIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing fixture, not its test suite or accepted heavy trials.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_nasdaq_shadow as fixtures
        self.case = fixtures.ShadowTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.folder, self.producer = self.case.folder, self.case.producer

    def bad_capture(self, seconds=1, raw=HTML, metadata=None):
        self.case.now = self.case.base + timedelta(seconds=seconds)
        with self.assertRaises(feed.FeedUnavailable):
            shadow.record_capture(self.folder, self.producer, raw, requested_at=self.case.now,
                acquired_at=self.case.now, source='synthetic', http_metadata=metadata)

    def test_html_does_not_establish_baseline_and_first_valid_snapshot_is_baseline(self):
        self.bad_capture()
        self.assertEqual(len(self.producer.snapshot()['receipts']), 0)
        self.assertEqual(self.case.requests(), [])
        self.case.capture(3, event=True)
        self.case.gate_all()
        self.assertEqual(self.case.requests(), [])
        health = feed.feed_health(shadow.capture_events(self.folder))
        self.assertEqual((health['status'], health['failed_captures']), ('RECOVERED_WITH_GAPS', 1))

    def test_bad_response_preserves_state_and_recovery_can_deliver_new_event(self):
        self.case.capture(0)
        before = self.producer.snapshot()
        self.bad_capture()
        self.assertEqual(self.producer.snapshot(), before)
        self.assertEqual(self.case.transport.calls, [])
        self.case.capture(3, event=True)
        self.case.gate_all()
        self.assertEqual(len(self.case.requests()), 1)
        self.assertEqual(self.case.transport.calls, [('NEWX',)])
        events = shadow.capture_events(self.folder)
        failed = self.folder / 'captures' / events[1]['capture_id']
        self.assertEqual((failed / 'raw.xml').read_bytes(), HTML)
        self.assertFalse((failed / 'snapshot.json').exists())
        self.assertEqual(events[1]['outcome']['kind'], 'non_feed_html')

    def test_three_consecutive_failures_stop_and_success_resets_streak(self):
        self.bad_capture(0)
        self.bad_capture(1)
        self.assertIsNone(feed.feed_health(shadow.capture_events(self.folder))['stop_reason'])
        self.case.capture(3)
        self.bad_capture(4)
        self.bad_capture(5)
        self.assertEqual(feed.feed_health(shadow.capture_events(self.folder))['consecutive_failures'], 2)
        self.bad_capture(6)
        health = feed.feed_health(shadow.capture_events(self.folder))
        self.assertEqual(health['stop_reason'], 'three_consecutive_unavailable_feed_attempts')
        self.assertIsNone(health['interruption_receipts'][-1]['recovered_at_utc'])

    def test_failed_capture_body_metadata_and_receipt_tampering_are_detected(self):
        self.bad_capture()
        event = shadow.capture_events(self.folder)[0]
        capture = self.folder / 'captures' / event['capture_id']
        for name in ('raw.xml', 'http.json', 'capture.json'):
            path = capture / name
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.assertRaises(ValueError):
                shadow.capture_events(self.folder)
            path.write_bytes(original)
        log = self.folder / 'feed_captures.jsonl'
        log.write_bytes(log.read_bytes().rstrip(b'\n'))
        with self.assertRaises(ValueError):
            shadow.capture_events(self.folder)

    def test_terminal_response_is_retained_without_changing_source_state(self):
        self.case.capture(0)
        before = self.producer.snapshot()
        response = feed.fetch_feed(opener=FakeOpener(FakeResponse(HTML, 429, {'Retry-After': '60'})))
        self.bad_capture(1, response.raw, response.metadata)
        self.assertEqual(self.producer.snapshot(), before)
        health = feed.feed_health(shadow.capture_events(self.folder))
        self.assertEqual(health['stop_reason'], 'http_access_denied_or_rate_limited')

    def recovered_report(self, no_event):
        self.bad_capture(-1)
        original = shadow.analyse
        def analyse(folder, cutoff, attempts, failures):
            return original(folder, cutoff, len(shadow.capture_events(folder)), failures)
        with patch.object(shadow, 'analyse', side_effect=analyse):
            return self.case.sample_report(no_event=no_event)

    def test_recovered_transition_keeps_independent_feed_warning(self):
        report = self.recovered_report(False)
        self.assertEqual(report['result'], 'PASS_WITH_WARNINGS')
        self.assertEqual(report['feed_health']['failed_captures'], 1)
        self.assertEqual(report['assessment']['availability']['result'], 'PASS')
        self.assertTrue(report['warnings'])
        self.assertTrue(report['audit_passed'] and report['replays_identical'])

    def test_recovered_no_event_is_readiness_only_with_gap_and_zero_quote_calls(self):
        report = self.recovered_report(True)
        self.assertEqual((report['result'], report['milestone']), ('READINESS_ONLY', 'READINESS_ONLY'))
        self.assertEqual(report['feed_health']['status'], 'RECOVERED_WITH_GAPS')
        self.assertEqual(report['quote_calls'], 0)
        self.assertTrue(report['warnings'])


if __name__ == '__main__':
    unittest.main()
