"""Local, opt-in manual membership operator window.

This is a separate operator surface, not the historical Overlay. All reads use
the existing journal in SQLite read-only mode; the only mutation available to
the web process is ``daily_membership.submit`` (a local worker inbox request).
The authenticated membership worker remains the sole hierarchy publisher.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import math
from pathlib import Path
import secrets
import sqlite3
from threading import Lock, Thread
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .daily_membership import CONTRACT, LEVELS, normalized, read, report_result, submit, timestamp, utcnow
from .daily_runtime import owner_available
from .sampling_membership import SAMPLING_HIERARCHY_CONTRACT

UTC = timezone.utc
TIERS = ('uni', 'focus', 'hot')


class OperatorProblem(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _utc_text(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _optional_json(path: Path) -> dict:
    try:
        result = read(path)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) else None


def _provider_time(milliseconds):
    if not isinstance(milliseconds, int):
        return None
    try:
        return _utc_text(datetime.fromtimestamp(milliseconds / 1000, UTC))
    except (OverflowError, OSError, ValueError):
        return None


def _eligibility(root: Path, plan: dict, at: datetime) -> tuple[bool, str]:
    """Advisory gate; the authoritative checks run again inside submit()."""
    if plan.get('membership_control') != CONTRACT:
        return False, 'This session was not launched with manual membership controls.'
    if (root / 'stop.request').exists() or (root / 'exit.request').exists():
        return False, 'Collection has a stop or exit request.'
    if not timestamp(plan['opens_at']) <= at < timestamp(plan['closes_at']):
        return False, 'Manual changes require the active collection window.'
    if not owner_available(root):
        return False, 'The daily controller is not currently verified as active.'
    owner = _optional_json(root / 'owner.json')
    worker = _optional_json(root / 'membership' / 'status.json')
    if not owner or not worker:
        return False, 'The membership worker has no current heartbeat.'
    try:
        age = (at - timestamp(worker['at'])).total_seconds()
    except (ValueError, KeyError, TypeError):
        return False, 'The membership worker heartbeat is unreadable.'
    if (worker.get('owner_pid') != owner.get('pid') or not -5 <= age <= 30
            or worker.get('state') not in ('RUNNING', 'DEGRADED')):
        return False, 'The membership worker heartbeat is stale or belongs to another controller.'
    return True, 'Worker and controller are available; each request is rechecked on submission.'


def membership_snapshot(root: Path, *, selected: str | None = None, clock=utcnow) -> dict:
    """Read a causal, effective-as-of-now roster and one selected quote."""
    root = Path(root).resolve()
    plan = read(root / 'plan.json')
    if Path(plan['state_root']).resolve() != root:
        raise ValueError('Session state directory does not match its plan')
    at = clock().astimezone(UTC)
    asof = _utc_text(at)
    if selected:
        selected = normalized(selected)
    path = Path(plan['journal']).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        identity = db.execute('SELECT * FROM store_identity WHERE singleton=1').fetchone()
        if (not identity or identity['schema_version'] != 2
                or identity['session_date'] != plan['session_date']
                or identity['membership_contract'] != SAMPLING_HIERARCHY_CONTRACT):
            raise ValueError('Journal identity does not match the selected session')
        latest = db.execute('''SELECT revision,effective_at_utc FROM sampling_membership_revision
            WHERE session_date=? ORDER BY revision DESC LIMIT 1''', (plan['session_date'],)).fetchone()
        effective = db.execute('''SELECT revision,effective_at_utc FROM sampling_membership_revision
            WHERE session_date=? AND effective_at_utc<=?
            ORDER BY effective_at_utc DESC,revision DESC LIMIT 1''', (plan['session_date'], asof)).fetchone()
        members = {tier: set() for tier in TIERS}
        if effective:
            for entry in db.execute('''SELECT channel,symbol FROM sampling_channel_member
                    WHERE session_date=? AND revision=?''', (plan['session_date'], effective['revision'])):
                if entry['channel'] in members:
                    members[entry['channel']].add(entry['symbol'])
        if not (members['hot'] <= members['focus'] <= members['uni']):
            raise ValueError('Recorded membership violates the nested hierarchy')
        tiers = {
            'uni': sorted(members['uni'] - members['focus']),
            'focus': sorted(members['focus'] - members['hot']),
            'hot': sorted(members['hot']),
        }
        quote = None
        if selected:
            quote = db.execute('''SELECT o.status,o.description,o.quote_last_price,
                    o.quote_close_price,o.quote_open_price,o.quote_low_price,
                    o.quote_high_price,o.quote_total_volume,o.quote_security_status,
                    o.quote_time_ms,o.quote_trade_time_ms,o.response_received_at_utc,
                    o.shares_outstanding,a.channel,a.completed_at_utc
                FROM quote_observation o JOIN quote_acquisition a USING(acquisition_id)
                WHERE o.symbol=? AND a.completed_at_utc<=?
                ORDER BY a.completed_at_utc DESC,a.acquisition_id DESC LIMIT 1''', (selected, asof)).fetchone()
            quote = dict(quote) if quote else None
    rank = 'outside'
    if selected:
        rank = next((tier for tier in ('hot', 'focus', 'uni') if selected in members[tier]), 'outside')
    details = None
    if selected:
        details = {'symbol': selected, 'tier': rank, 'quote_available': quote is not None}
        if quote:
            last = _number(quote['quote_last_price'])
            shares = quote['shares_outstanding']
            details.update(description=quote['description'], status=quote['status'],
                security_status=quote['quote_security_status'], last=last,
                close=_number(quote['quote_close_price']), open=_number(quote['quote_open_price']),
                low=_number(quote['quote_low_price']), high=_number(quote['quote_high_price']),
                volume=quote['quote_total_volume'],
                estimated_market_cap=(last * shares if last is not None and isinstance(shares, int)
                                      and shares > 0 else None),
                quote_time_utc=_provider_time(quote['quote_time_ms']),
                last_trade_time_utc=_provider_time(quote['quote_trade_time_ms']),
                api_response_utc=quote['response_received_at_utc'],
                recorded_at_utc=quote['completed_at_utc'], channel=quote['channel'])
    allowed, reason = _eligibility(root, plan, at)
    status = _optional_json(root / 'status.json')
    pending = (dict(revision=latest['revision'], effective_at_utc=latest['effective_at_utc'])
               if latest and (not effective or latest['revision'] != effective['revision']) else None)
    return {'session_date': plan['session_date'], 'as_of_utc': asof,
            'synthetic_demo': bool(plan.get('operator_demo_synthetic')),
            'phase': status.get('phase', 'UNAVAILABLE'), 'final': status.get('final_status', False),
            'eligible': allowed, 'eligibility_message': reason,
            'effective_revision': effective['revision'] if effective else None,
            'published_revision': latest['revision'] if latest else None,
            'pending_revision': pending, 'tiers': tiers, 'counts': {k: len(v) for k, v in tiers.items()},
            'selected': details, 'read_only_snapshot': True, 'provider_calls': 0}


class OperatorState:
    def __init__(self, root: Path, *, clock=utcnow):
        self.root = Path(root).resolve()
        self.clock = clock
        self.csrf = secrets.token_urlsafe(32)
        self.proposals: dict[str, dict] = {}
        self.lock = Lock()

    def preview(self, symbol: str, action: str, target: str) -> dict:
        try:
            symbol = normalized(symbol)
        except ValueError as error:
            raise OperatorProblem(str(error), 400) from error
        if action not in ('promote', 'demote') or target not in LEVELS or (action == 'demote' and target == 'hot'):
            raise OperatorProblem('Choose promote to Uni/Focus/Hot or demote to Uni/Focus.', 400)
        view = membership_snapshot(self.root, selected=symbol, clock=self.clock)
        if not view['eligible']:
            raise OperatorProblem(view['eligibility_message'])
        if view['pending_revision'] or view['effective_revision'] is None:
            raise OperatorProblem('Wait for the pending membership revision to become effective, then preview again.')
        current = view['selected']['tier']
        current_index = LEVELS.index(current) if current in LEVELS else -1
        desired = LEVELS.index(target)
        if action == 'demote' and (current_index < 0 or desired > current_index):
            raise OperatorProblem('Demotion requires membership at the destination tier or higher.')
        if action == 'promote' and desired <= current_index:
            raise OperatorProblem('The symbol is already at or above that tier.')
        if (action == 'promote' and target == 'hot' and current != 'hot'
                and view['counts']['hot'] >= read(self.root / 'plan.json').get('membership_max_hot', 40)):
            raise OperatorProblem('The configured Hot capacity is already reached.')
        after = target if action == 'demote' else LEVELS[max(current_index, desired)]
        note = ('This ordinary demotion also supersedes existing/older Nasdaq halt episodes; '
                'a later, distinct halt may promote the symbol again.' if action == 'demote' else
                'New outside-Universe symbols require worker-side equity identity validation and capacity checks.')
        at = self.clock().astimezone(UTC)
        proposal_id = secrets.token_urlsafe(24)
        proposal = {'symbol': symbol, 'action': action, 'target': target,
                    'before': current, 'after': after, 'revision': view['published_revision'],
                    'session_date': view['session_date'], 'expires_at': at + timedelta(seconds=90)}
        with self.lock:
            self.proposals = {k: v for k, v in self.proposals.items()
                              if v['expires_at'] > at and v.get('state') != 'denied'}
            if len(self.proposals) >= 100:
                raise OperatorProblem('Too many unexpired previews. Wait briefly and try again.')
            self.proposals[proposal_id] = proposal
        return {'proposal_id': proposal_id, 'symbol': symbol, 'action': action,
                'target': target, 'before': current, 'after': after,
                'based_on_revision': view['published_revision'],
                'expires_at_utc': _utc_text(proposal['expires_at']), 'note': note,
                'request_written': False}

    def confirm(self, proposal_id: str) -> dict:
        if not isinstance(proposal_id, str):
            raise OperatorProblem('Invalid proposal identifier.', 400)
        with self.lock:
            record = self.proposals.get(proposal_id)
            if not record:
                raise OperatorProblem('Proposal not found or expired. Preview again.')
            if record.get('state') == 'submitted':
                return {'request': record['request'], 'already_submitted': True}
            if record.get('state') == 'unknown':
                raise OperatorProblem('Request outcome uncertain. Inspect the inbox before any new attempt.')
            if self.clock().astimezone(UTC) >= record['expires_at']:
                raise OperatorProblem('Proposal expired. Preview again.')
            view = membership_snapshot(self.root, selected=record['symbol'], clock=self.clock)
            if not view['eligible'] or view['pending_revision']:
                raise OperatorProblem('Session unavailable or another revision is pending. Preview again.')
            if (view['session_date'] != record['session_date']
                    or view['published_revision'] != record['revision']):
                raise OperatorProblem('Membership changed since preview. Preview again.')
            try:
                command = submit(self.root, action=record['action'],
                                 symbol=record['symbol'], target=record['target'], clock=self.clock)
            except ValueError as error:
                record['state'] = 'denied'
                raise OperatorProblem(f'No request accepted: {error}') from error
            except OSError as error:
                record['state'] = 'unknown'
                raise OperatorProblem('Request outcome uncertain; inspect the inbox before retrying.') from error
            record['state'] = 'submitted'
            record['request'] = command
            return {'request': command, 'already_submitted': False}


PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market State · Membership Operator</title>
<link rel="stylesheet" href="/operator.css"><script src="/operator.js" defer></script>
</head><body>
<header><span class="monogram">MB</span><div><h1>Market State</h1>
<p>MANUAL MEMBERSHIP OPERATOR · LOCAL EXPERIMENT</p></div>
<div class="header-status"><strong id="session-date">Session unavailable</strong>
<small id="session-phase">Checking local collector…</small></div></header>
<main><div id="demo-banner" class="demo-banner" hidden>
<span>OFFLINE SYNTHETIC DEMO — FABRICATED QUOTES · NO SCHWAB CONNECTION · NO PRODUCTION FILES</span>
<button id="stop-demo" class="stop-demo" type="button">Stop Demo</button></div>
<div id="gate" role="status" class="gate">Checking controller and worker…</div>
<div class="intro"><h2>Membership</h2><span id="revision">—</span></div>
<p class="muted">Each symbol appears once at its highest tier. Underlying membership is nested: Hot ⊆ Focus ⊆ Uni.
This window does not start, stop, or authenticate a collector.</p>
<div class="tier-grid">
<section class="tier uni"><h3>Uni only <span id="uni-count">0</span></h3><input id="uni-filter" aria-label="Filter Uni symbols" placeholder="Filter symbols"><div id="uni-list" class="tier-list"></div></section>
<section class="tier focus"><h3>Focus only <span id="focus-count">0</span></h3><input id="focus-filter" aria-label="Filter Focus symbols" placeholder="Filter symbols"><div id="focus-list" class="tier-list"></div></section>
<section class="tier hot"><h3>Hot <span id="hot-count">0</span></h3><input id="hot-filter" aria-label="Filter Hot symbols" placeholder="Filter symbols"><div id="hot-list" class="tier-list"></div></section>
</div>
<div class="bottom-grid">
<section class="card"><div class="section-head"><h2>Symbol inspector</h2><span id="selected-tier" class="badge">—</span></div>
<div class="field"><label for="symbol">Selected symbol, or enter a new equity symbol</label>
<input id="symbol" maxlength="20" autocapitalize="characters" autocomplete="off" placeholder="Enter symbol"></div>
<div id="detail" class="details muted">Select a member above or enter a symbol.</div></section>
<section class="card controls"><h2>Request membership change</h2>
<p class="muted">Requests are submitted to the existing membership worker, never directly to the journal.</p>
<div class="two-fields"><div class="field"><label for="action">Action</label><select id="action"><option value="promote">Promote</option><option value="demote">Ordinary demote</option></select></div>
<div class="field"><label for="target">Destination tier</label><select id="target"><option value="focus">Focus</option><option value="hot">Hot</option><option value="uni">Uni</option></select></div></div>
<button id="preview" class="primary">Preview action</button>
<div id="review" class="review" hidden><strong>Review before submitting</strong><p id="review-text"></p><p id="review-note" class="muted"></p>
<button id="confirm" class="danger">Confirm and queue request</button><button id="cancel" class="secondary">Cancel</button></div>
<div id="receipt" class="receipt" aria-live="polite">No command submitted.</div></section>
</div><p id="page-error" class="error" role="alert"></p>
<footer>Local-only operator interface · Historical and backtest charts remain read-only ·
Closing this window does not stop collection.</footer></main></body></html>'''


def create_server(root: Path, *, port: int = 8054, clock=utcnow,
                  demo_stop: Callable[[], None] | None = None) -> ThreadingHTTPServer:
    """Bind to loopback; only an explicitly synthetic session can stop itself."""
    state = OperatorState(root, clock=clock)
    if demo_stop is not None and not read(state.root / 'plan.json').get('operator_demo_synthetic'):
        raise ValueError('Demo shutdown is only available for synthetic sessions')
    demo_shutdown_started = False
    assets = files('mb_market_data').joinpath('assets')
    css = assets.joinpath('membership_operator.css').read_bytes()
    js = assets.joinpath('membership_operator.js').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, code: int, body: bytes, kind: str = 'application/json'):
            self.send_response(code)
            for key, value in (('Content-Type', kind), ('Cache-Control', 'no-store'),
                               ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'),
                               ('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"),
                               ('Content-Length', str(len(body)))):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, code: int, data: dict):
            self.send(code, json.dumps(data, allow_nan=False).encode('utf-8'))

        def fail(self, error: Exception):
            code = error.status if isinstance(error, OperatorProblem) else 503
            message = str(error) if isinstance(error, OperatorProblem) else 'Session data unavailable; no action performed.'
            self.json(code, {'error': message})

        def do_GET(self):
            if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                self.json(403, {'error': 'Local loopback host required'})
                return
            path = urlparse(self.path)
            try:
                if path.path == '/':
                    self.send(200, PAGE.encode(), 'text/html; charset=utf-8')
                elif path.path == '/operator.css':
                    self.send(200, css, 'text/css; charset=utf-8')
                elif path.path == '/operator.js':
                    self.send(200, js, 'application/javascript; charset=utf-8')
                elif path.path == '/config':
                    self.json(200, {'csrf': state.csrf, 'mode': 'local-manual-operator'})
                elif path.path == '/snapshot':
                    selected = parse_qs(path.query).get('symbol', [None])[0]
                    self.json(200, membership_snapshot(state.root, selected=selected, clock=clock))
                elif path.path == '/result':
                    cid = parse_qs(path.query).get('id', [''])[0]
                    self.json(200, report_result(state.root, cid, clock=clock))
                else:
                    self.json(404, {'error': 'Unknown page'})
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
                self.fail(error)

        def do_POST(self):
            nonlocal demo_shutdown_started
            path = urlparse(self.path).path
            if path == '/stop-demo' and demo_stop is None:
                self.json(404, {'error': 'Demo shutdown is unavailable for this session'})
                return
            if path not in ('/preview', '/confirm', '/stop-demo'):
                self.json(405, {'error': 'Unsupported action'})
                return
            base = 'http://127.0.0.1:' + str(self.server.server_port)
            if (self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port)
                    or self.headers.get('Origin') != base
                    or self.headers.get('X-MB-Operator-CSRF') != state.csrf
                    or self.headers.get('Content-Type', '').split(';', 1)[0] != 'application/json'):
                self.json(403, {'error': 'Local same-origin confirmation required'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 4096:
                    raise OperatorProblem('Invalid request size.', 400)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise OperatorProblem('Expected an object.', 400)
                if path == '/stop-demo':
                    if data:
                        raise OperatorProblem('Demo stop takes no parameters.', 400)
                    self.json(202, {'status': 'STOP_REQUESTED', 'synthetic_only': True})
                    # shutdown() must run outside the serving thread. Only
                    # the synthetic runner supplies this callback; the normal
                    # production viewer has no shutdown endpoint.
                    with state.lock:
                        start_shutdown = not demo_shutdown_started
                        demo_shutdown_started = True
                    if start_shutdown:
                        Thread(target=demo_stop, name='synthetic-demo-shutdown', daemon=True).start()
                    return
                if path == '/preview':
                    answer = state.preview(data.get('symbol', ''), data.get('action', ''), data.get('target', ''))
                else:
                    answer = state.confirm(data.get('proposal_id', ''))
                self.json(200, answer)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
                self.fail(error)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True,
                        help='Existing daily controller state directory; this tool never starts collection')
    parser.add_argument('--port', type=int, default=8054)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('Use a local port from 1024 through 65535')
    if not (args.state_root / 'plan.json').is_file():
        parser.error('Existing state root and plan.json required')
    server = create_server(args.state_root, port=args.port)
    print('LOCAL EXPERIMENTAL MEMBERSHIP OPERATOR; no credentials or provider calls.', flush=True)
    print(f'Open http://127.0.0.1:{args.port}/', flush=True)
    print('Preview alone makes no request. Confirm queues a command through the existing worker.', flush=True)
    print('Ctrl+C stops this local operator window only. Collectors continue.', flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        print('Operator window stopped; collection unchanged.')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
