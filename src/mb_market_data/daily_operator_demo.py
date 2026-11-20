"""Offline-only, self-contained interactive manual-membership demo.

Creates a brand-new temporary schema-v2 journal, synthetic quote rows, an
in-process heartbeat and the *real* shared membership writer. The local
operator window uses its normal submission and receipt paths. No installed
controller, Schwab credentials, network market-data calls or production paths
are read. Ctrl+C closes the window and discards the temporary session.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
from unittest.mock import patch
from zoneinfo import ZoneInfo

from .acquisition_health import atomic_json
from . import daily_membership
from .daily_operator_ui import create_server
from .opening_hierarchy import write_opening_proposal
from .quote_observation_store import PollRunProvenance, QuoteObservationStore
from .sampling_membership import SamplingHierarchyRevision
from .schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
from .watchlist_polling import PollRequest, PollSlot, WatchlistKind

UTC = timezone.utc
ET = ZoneInfo('America/New_York')
SYMBOLS = (
    'AAME', 'AARD', 'ABOS', 'ABVC', 'ACCS', 'ACET', 'ACFN', 'ACH',
    'ACON', 'ACTU', 'ACXP', 'AEI', 'AIMD', 'BACC', 'BGIN', 'BIRD',
    'BKHAU', 'CRMN', 'CURX', 'DDC', 'EDHL', 'ENGS', 'INTC', 'MSFT',
    'NVDA', 'ONFO', 'SLS', 'SMX',
)
FOCUS = ('ACH', 'AEI', 'AIMD', 'BGIN', 'CRMN', 'CURX', 'INTC', 'MSFT', 'NVDA', 'ONFO')
HOT = ('BGIN', 'INTC', 'MSFT', 'NVDA')


def synthetic_identity(_client, symbol, evidence_path):
    """Simulated identity response; NOT an assertion of real-market eligibility.

    NOTREAL is a deliberate negative fixture so the end-to-end QA can also
    prove that a well-formed ticker can be refused by the worker.
    """
    denied = symbol == 'NOTREAL'
    atomic_json(evidence_path, {'symbol': symbol, 'synthetic': True,
                                'provider_calls': 0, 'identity_confirmed': not denied,
                                'warning': 'OFFLINE TEST IDENTITY ONLY'})
    if denied:
        raise daily_membership.Rejected('SIMULATED: Exact equity identity not confirmed')
    return True


def seed_demo(root: Path, *, at: datetime | None = None, duration_minutes: int = 180) -> dict:
    """Populate only the caller-provided new, empty directory with test data."""
    root = Path(root).resolve()
    if not 10 <= duration_minutes <= 180:
        raise ValueError('Demo duration must be 10–180 minutes')
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise ValueError('Synthetic demo requires a new, empty directory')
    else:
        root.mkdir(parents=True, exist_ok=False)
    now = (at or datetime.now(UTC)).astimezone(UTC)
    minute = (now - timedelta(minutes=1)).replace(second=0, microsecond=0)
    opening = minute - timedelta(minutes=2)
    # Keep the effective opening and the synthetic acquisition on one ET date,
    # including a demo launched just after midnight Eastern.
    if opening.astimezone(ET).date() != minute.astimezone(ET).date():
        opening = minute
    closing = now + timedelta(minutes=duration_minutes)
    session_date = opening.astimezone(ET).date()
    r0 = SamplingHierarchyRevision(session_date, 0, opening,
        SYMBOLS, (), (), 'synthetic-demo-opening')
    opening_path = root / 'opening_hierarchy_r0.json'
    write_opening_proposal(opening_path, r0)
    plan = daily_membership.configure({
        'session_date': session_date.isoformat(), 'opens_at': opening.isoformat(),
        'closes_at': closing.isoformat(), 'opening_path': str(opening_path),
        'opening_content_sha256': r0.content_sha256,
        'journal': str(root / 'synthetic_only.sqlite3'), 'state_root': str(root),
        'channels': ['uni', 'focus', 'hot'], 'cadence_profile': 'interleaved-v1',
        'hot_seed_symbols': list(HOT), 'operator_demo_synthetic': True,
    }, nasdaq=False)
    store = QuoteObservationStore(plan['journal'], session_date=session_date, schema_version=2)
    store.initialize()
    r1 = SamplingHierarchyRevision(session_date, 1, opening, SYMBOLS, FOCUS, (),
        'synthetic-demo-focus', metadata={'ranked_focus_symbols': list(FOCUS)})
    r2 = SamplingHierarchyRevision(session_date, 2, opening, SYMBOLS, FOCUS, HOT,
        'synthetic-demo-hot', metadata={'ranked_focus_symbols': list(FOCUS)})
    # Only the *test fixture* uses a historical synthetic publication clock.
    # This is not a way to backdate a real session's membership.
    with patch('mb_market_data.quote_observation_store._utc_now', return_value=opening-timedelta(minutes=1)):
        for revision in (r0, r1, r2):
            store.record_membership_revision(revision)
    for kind, second, symbols in (
        (WatchlistKind.UNI, 0, SYMBOLS),
        (WatchlistKind.FOCUS, 10, FOCUS),
        (WatchlistKind.HOT, 12, HOT),
    ):
        scheduled = minute + timedelta(seconds=second)
        dispatched = scheduled + timedelta(milliseconds=5)
        received = dispatched + timedelta(milliseconds=140)
        request = PollRequest(PollSlot(kind, scheduled, cadence_profile='interleaved-v1'),
                              2, symbols, dispatched)
        run_id = 'synthetic-demo-' + kind.value
        store.record_run(PollRunProvenance(run_id, opening, 'SYNTHETIC-ONLY',
            {'provider': 'none', 'test_mode': True}, host='OFFLINE DEMO',
            command=('membership_operator_demo',)))
        results = []
        for symbol in symbols:
            number = SYMBOLS.index(symbol) + 1
            last = round(1.5 + number * 1.72, 4)
            synthetic = {'symbol': symbol, 'assetMainType': 'EQUITY',
                'quote': {'lastPrice': last, 'closePrice': round(last * .96, 4),
                    'openPrice': round(last * .98, 4), 'lowPrice': round(last * .92, 4),
                    'highPrice': round(last * 1.05, 4),
                    'totalVolume': number * 154321, 'securityStatus': 'Normal',
                    'quoteTime': int(received.timestamp()*1000),
                    'tradeTime': int((received-timedelta(seconds=2)).timestamp()*1000)},
                'fundamental': {'sharesOutstanding': 1000000 + number * 330000},
                'reference': {'description': 'SYNTHETIC ' + symbol + ' TEST SECURITY'}}
            results.append(QuoteResult(symbol, QuoteStatus.QUOTE, synthetic,
                None, 1, dispatched, received))
        store.record_acquisition(run_id, request,
            QuoteBatchResult(tuple(results), 1, 400, ()), completed_at=received+timedelta(milliseconds=20))
    atomic_json(root / 'plan.json', plan)
    return plan


class DemoSession:
    """Own temporary heartbeats and the single synthetic membership worker."""
    def __init__(self, root: Path, *, duration_minutes: int = 180, at: datetime | None = None):
        self.root = Path(root).resolve()
        self.plan = seed_demo(self.root, at=at, duration_minutes=duration_minutes)
        self.stop_requested = Event()
        self.threads: list[Thread] = []

    def heartbeat(self):
        while not self.stop_requested.is_set():
            at = datetime.now(UTC).isoformat()
            atomic_json(self.root / 'owner.json', {'pid': os.getpid(), 'active': True,
                'at': at, 'synthetic': True})
            atomic_json(self.root / 'status.json', {'phase': 'SYNTHETIC_DEMO',
                'at': at, 'final_status': False, 'provider': 'none', 'synthetic': True})
            self.stop_requested.wait(.5)
        atomic_json(self.root / 'owner.json', {'pid': os.getpid(), 'active': False,
            'at': datetime.now(UTC).isoformat(), 'synthetic': True})

    def worker(self):
        with patch.object(daily_membership, 'verify_identity', synthetic_identity):
            daily_membership.service(self.plan, None, owner_pid=os.getpid(),
                                     stop=self.stop_requested.is_set)

    def start(self):
        for name, target in (('demo-heartbeat', self.heartbeat), ('demo-membership-writer', self.worker)):
            thread = Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop_requested.set()
        for thread in self.threads:
            thread.join(timeout=3)

    def stop_server(self, server):
        """Request server shutdown from the separate stop-handler thread."""
        self.stop_requested.set()
        server.shutdown()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8054)
    parser.add_argument('--duration-minutes', type=int, default=180)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('Use a local port from 1024 through 65535')
    if not 10 <= args.duration_minutes <= 180:
        parser.error('Demo duration must be 10 through 180 minutes')
    with tempfile.TemporaryDirectory(prefix='mb_membership_operator_SYNTHETIC_') as path:
        demo = DemoSession(Path(path), duration_minutes=args.duration_minutes)
        server = None
        try:
            demo.start()
            server = create_server(demo.root, port=args.port,
                                   demo_stop=lambda: demo.stop_server(server))
            print('OFFLINE SYNTHETIC DEMO - NO SCHWAB / NO PASSWORD / NO PRODUCTION JOURNAL', flush=True)
            print(f'SYNTHETIC TEMP ROOT: {demo.root}', flush=True)
            print(f'Open Firefox: http://127.0.0.1:{args.port}/', flush=True)
            print('Try INTC Hot to Focus, then Focus to Hot; also try a symbol outside Uni.', flush=True)
            print('Use Stop Demo in Firefox for graceful shutdown and temporary journal cleanup.', flush=True)
            server.serve_forever(poll_interval=.25)
            if demo.stop_requested.is_set():
                print('Stop Demo received; closing synthetic worker and server.', flush=True)
        except KeyboardInterrupt:
            print('Synthetic demonstration stopped.', flush=True)
        finally:
            if server is not None:
                server.server_close()
            demo.close()
    print('Synthetic temporary files removed; CMD prompt should now return.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
