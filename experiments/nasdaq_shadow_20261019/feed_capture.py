"""Bounded, non-interactive RSS transport and explicit unavailable-feed outcomes.

No cookies, browser challenges, credentials, application retries or redirects.
Only a small allowlist of response headers is retained; bodies stay private.
"""
from dataclasses import dataclass
from http.client import HTTPException, IncompleteRead
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as XML


CURRENT_FEED_URL = 'https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts'
MAX_BODY_BYTES = 4_000_000
SAFE_HEADERS = ('Content-Type', 'Content-Length', 'Content-Encoding', 'Date',
                'Server', 'Via', 'Retry-After', 'X-Iinfo', 'X-CDN', 'Location')


class NoFeedRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


@dataclass(frozen=True)
class FeedResponse:
    raw: bytes
    metadata: dict


def fetch_feed(*, opener=None):
    """One request to the fixed feed URL; retain bounded HTTP error bodies too."""
    request = Request(CURRENT_FEED_URL, headers={
        'User-Agent': 'mb_market_data bounded Nasdaq shadow/1',
        'Accept': 'application/rss+xml,application/xml,text/plain'})
    metadata = {'kind': 'http', 'requested_url': CURRENT_FEED_URL, 'method': 'GET',
                'final_url': None, 'status_code': None, 'headers': {},
                'body_complete': False, 'transport_error_type': None,
                'timeout_seconds': 3, 'body_limit_bytes': MAX_BODY_BYTES,
                'redirects_followed': False}
    opener = opener or build_opener(NoFeedRedirects())
    response = None
    raw = b''
    try:
        try:
            response = opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        metadata.update(final_url=response.geturl(), status_code=response.getcode(),
                        headers={name: str(response.headers.get(name))[:2048]
                                 for name in SAFE_HEADERS if response.headers.get(name) is not None})
        try:
            raw = response.read(MAX_BODY_BYTES + 1)
            metadata['body_complete'] = len(raw) <= MAX_BODY_BYTES
        except IncompleteRead as error:
            raw = error.partial[:MAX_BODY_BYTES + 1]
            metadata['transport_error_type'] = type(error).__name__
        except (OSError, HTTPException) as error:
            metadata['transport_error_type'] = type(error).__name__
    except (OSError, URLError, HTTPException) as error:
        metadata['transport_error_type'] = type(error).__name__
    finally:
        if response is not None:
            response.close()
    return FeedResponse(raw, metadata)


def inspect_feed(raw, metadata):
    """Validate HTTP/body/RSS structure before unchanged semantic processing."""
    result = {'status': 'valid', 'kind': 'rss', 'terminal': False}
    def unavailable(kind, terminal=False, **details):
        return dict(result, status='unavailable', kind=kind, terminal=terminal, **details)
    if metadata.get('kind') == 'http':
        code = metadata.get('status_code')
        if code in (401, 403, 429):
            return unavailable('http_access_denied_or_rate_limited', True, status_code=code)
        if code is not None and 300 <= code < 400:
            return unavailable('http_redirect_refused', True, status_code=code)
        if code is not None and code >= 400 and metadata.get('headers', {}).get('Retry-After'):
            return unavailable('server_retry_after_stop', True, status_code=code)
        if metadata.get('final_url') not in (None, CURRENT_FEED_URL):
            return unavailable('unexpected_response_url', True)
        if len(raw) > MAX_BODY_BYTES:
            return unavailable('response_size_limit', True)
        if metadata.get('transport_error_type'):
            return unavailable('transport_error', error_type=metadata['transport_error_type'])
        if code != 200:
            return unavailable('http_status_unavailable', status_code=code)
        if not metadata.get('body_complete'):
            return unavailable('incomplete_body')
        length = metadata.get('headers', {}).get('Content-Length')
        if length is not None:
            try:
                if int(length) != len(raw):
                    return unavailable('content_length_mismatch')
            except ValueError:
                return unavailable('invalid_content_length')
    if not raw:
        return unavailable('empty_body')
    decoded = raw.decode('utf-8-sig', errors='replace')
    if re.match(r'^\s*(?:<\?xml[^>]*>\s*)?(?:<!doctype\s+html\b|<html(?:\s|>))', decoded[:1024], re.I):
        return unavailable('non_feed_html')
    # Same parsing copy as the accepted Nasdaq parser. Never mutate raw evidence.
    try:
        root = XML.fromstring(re.sub(r'(<\/?)ndaq:', r'\1', decoded))
    except XML.ParseError as error:
        return unavailable('malformed_xml', xml_error_position=list(error.position))
    if root.tag != 'rss' or root.find('channel') is None:
        return unavailable('non_rss_document')
    return result


class FeedUnavailable(Exception):
    def __init__(self, outcome, capture_id):
        super().__init__(outcome['kind'])
        self.outcome, self.capture_id = outcome, capture_id


def feed_health(events):
    """Reconstruct interruption/recovery receipts without inventing halt coverage."""
    gaps, current = [], None
    consecutive = maximum = 0
    stop_reason = None
    for event in events:
        outcome = event['outcome']
        if outcome['status'] == 'valid':
            if current is not None:
                current['recovered_at_utc'] = event['acquired_at_utc']
                gaps.append(current)
                current = None
            consecutive = 0
        else:
            consecutive += 1
            maximum = max(maximum, consecutive)
            if current is None:
                current = {'first_failed_request_at_utc': event['requested_at_utc'],
                           'failed_attempts': [], 'recovered_at_utc': None}
            current['failed_attempts'].append(event['attempt'])
            if outcome.get('terminal'):
                stop_reason = outcome['kind']
            elif consecutive >= 3:
                stop_reason = 'three_consecutive_unavailable_feed_attempts'
    if current is not None:
        gaps.append(current)
    successful = sum(e['outcome']['status'] == 'valid' for e in events)
    failed = len(events) - successful
    status = ('STOP_REQUIRED' if stop_reason else 'UNAVAILABLE' if not successful else
              'INTERRUPTED' if current is not None else 'RECOVERED_WITH_GAPS' if failed else 'OK')
    return {'status': status, 'attempts': len(events), 'successful_captures': successful,
            'failed_captures': failed, 'consecutive_failures': consecutive,
            'maximum_consecutive_failures': maximum, 'stop_reason': stop_reason,
            'interruption_receipts': gaps,
            'halt_discovery_continuity': 'NOT_ESTABLISHED' if failed or not successful else 'SAMPLED_FEED_ONLY',
            'limitation': 'Unavailable responses are unknown feed intervals, never evidence of no halts.'}
