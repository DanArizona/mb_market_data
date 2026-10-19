"""Offline four-process integration review. No brokerage or production inputs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTRACT = 'isolated-combined-synthetic-v1'
MODES = ('coordinator', 'uni', 'focus', 'hot')
GRIDS = {'uni': (0, 30), 'focus': (5, 20, 35, 50), 'hot': tuple(range(0, 60, 2))}
COORDINATOR_COMMIT = '0417b345e2725f4e1e184d3b3202bedb846dbfca'


def utc_now():
    return datetime.now(timezone.utc)


def text(at):
    return at.astimezone(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def append_line(path, value):
    with Path(path).open('a', encoding='utf-8') as file:
        file.write(json.dumps(value, sort_keys=True, allow_nan=False) + '\n')


def lines(path):
    path = Path(path)
    if not path.is_file():
        return []
    # A concurrently appended incomplete final line is not yet an available receipt.
    return [json.loads(line) for line in path.read_text(encoding='utf-8').split('\n')[:-1] if line]


def require(value, message):
    if not value:
        raise RuntimeError(message)


def deny_network():
    def denied(*args, **kwargs):
        raise RuntimeError('Network access is disabled in this synthetic review')
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied


def verify_coordinator(root=ROOT):
    pin = read_json(root / 'experiments/combined_load_20261019/coordinator_pin.json')
    require(pin['commit'] == COORDINATOR_COMMIT, 'Unexpected Coordinator source pin')
    folder = root / 'coordinator_src/mb_watchlist_coordinator'
    require(folder.is_dir(), 'Pinned Coordinator dependency is missing; use the complete review bundle')
    require({p.name for p in folder.glob('*.py')} == set(pin['files']), 'Coordinator file set differs')
    for name, expected in pin['files'].items():
        require(Path(name).name == name, 'Coordinator source path escape')
        path = folder / name
        require(path.resolve().is_relative_to(folder.resolve()), 'Coordinator source path escape')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'Coordinator source differs: ' + name)
    return pin


def bootstrap():
    verify_coordinator()
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'coordinator_src')]


def plan(duration):
    if type(duration) is not int or duration not in (60, 300):
        raise ValueError('Use the bounded 60-second verification or 300-second review')
    roster = [f'S{n:04d}' for n in range(554)]
    return {'contract': CONTRACT, 'source': 'synthetic_only', 'duration_seconds': duration,
            'opening_uni': roster, 'opening_focus': roster[:40], 'initial_hot': roster[:40],
            'outside_symbol': 'S0554', 'algorithm_symbol': 'S0040',
            'grids': {k: list(v) for k, v in GRIDS.items()}, 'batch_size': 400,
            'dispatch_deadline_seconds': .75, 'coordinator_poll_seconds': .25,
            'publication_lead_seconds': .5, 'demotion_grace_seconds': 2.0,
            'minimum_residence_seconds': 4.0,
            'batch_delays_seconds': {'uni': .18, 'focus': .20, 'hot': .25},
            'overlap_delay_seconds': 1.1,
            'overlap_at_seconds': int(duration * .2),
            'purpose': 'Actual elapsed local scheduling with synthetic responses; no provider capacity claim'}


def prepare(folder, duration):
    from mb_market_data.quote_observation_store import QuoteObservationStore
    from mb_market_data.sampling_acquisition import MARKER
    from mb_market_data.sampling_incremental import INCREMENTAL_MARKER
    from mb_market_data.sampling_membership import SamplingHierarchyRevision
    config = plan(duration)
    folder = Path(folder)
    if folder.exists():
        raise ValueError('A fresh experiment directory is required')
    folder.mkdir(parents=True)
    # Future synthetic session avoids midnight/calendar dependence. Time advances
    # at normal wall-clock speed in every process; it is never stepped during polling.
    start = (utc_now() + timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)
    config.update(start=text(start), end=text(start + timedelta(seconds=duration)),
                  utc_offset_seconds=(start - timedelta(seconds=18) - utc_now()).total_seconds(),
                  run_root=str(folder.resolve()), run_id=folder.name)
    write_json(folder / 'config.json', config)
    write_json(folder / 'identity.json', {'contract': CONTRACT, 'run_id': folder.name,
               'run_root': str(folder.resolve()),
               'config_sha256': hashlib.sha256((folder / 'config.json').read_bytes()).hexdigest()})
    store = QuoteObservationStore(folder / 'quotes.sqlite3', session_date=start.date(), schema_version=2)
    store.initialize()
    store.record_membership_revision(SamplingHierarchyRevision(session_date=start.date(), revision=0,
        effective_at=start - timedelta(seconds=60), uni_symbols=tuple(config['opening_uni']),
        focus_symbols=tuple(config['opening_focus']), hot_symbols=(), source=CONTRACT,
        metadata={MARKER: True, INCREMENTAL_MARKER: True, 'synthetic': True,
                  'clock': 'future offset; actual elapsed wall-clock speed', 'combined_load_contract': CONTRACT}))
    # Finish schema/guard initialization serially before any worker pins schema epochs.
    for channel in ('uni', 'focus', 'hot'):
        load_poller(folder, channel, Event())
    ledger, _ = load_control(folder)
    submit(ledger, config, 'manual', 'initial-hot', config['initial_hot'], 'hot')
    return config


def load_config(folder):
    folder = Path(folder).resolve()
    identity = read_json(folder / 'identity.json')
    require(identity == {'contract': CONTRACT, 'run_id': folder.name, 'run_root': str(folder),
        'config_sha256': hashlib.sha256((folder / 'config.json').read_bytes()).hexdigest()},
        'Experiment identity/configuration changed')
    config = read_json(folder / 'config.json')
    expected = plan(config['duration_seconds'])
    require(all(config.get(k) == v for k, v in expected.items()), 'Synthetic plan changed')
    require(config['run_root'] == str(folder) and config['run_id'] == folder.name, 'Run path changed')
    start, end = datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])
    require(start.tzinfo is not None and end - start == timedelta(seconds=config['duration_seconds']), 'Run window differs')
    require(start.hour == 14 and start.minute == start.second == start.microsecond == 0, 'Synthetic window grid differs')
    offset = config['utc_offset_seconds']
    require(type(offset) in (int, float) and math.isfinite(offset), 'Invalid synthetic clock offset')
    clock = lambda: utc_now() + timedelta(seconds=offset)
    return config, clock


def store_for(folder, config):
    from mb_market_data.quote_observation_store import QuoteObservationStore
    start = datetime.fromisoformat(config['start'])
    return QuoteObservationStore(Path(folder) / 'quotes.sqlite3', session_date=start.date(), schema_version=2)


class SyntheticResponse:
    status_code = 200
    ok = True
    text = ''

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class SyntheticClient:
    def __init__(self, folder, channel, config, clock, stop):
        self.folder, self.channel, self.config, self.clock, self.stop = Path(folder), channel, config, clock, stop
        self.slot = None
        self.count = 0

    def quotes(self, symbols, *, fields):
        require(self.slot is not None and fields == 'all', 'Synthetic client requires a captured slot')
        require(0 < len(symbols) <= 400 and all(s in self.config['opening_uni'] + [self.config['outside_symbol']] for s in symbols),
                'Synthetic client roster differs')
        self.count += 1
        began = self.clock()
        at = (self.slot.scheduled_at - datetime.fromisoformat(self.config['start'])).total_seconds()
        overlap = self.channel == 'hot' and at == self.config['overlap_at_seconds']
        delay = self.config['overlap_delay_seconds'] if overlap else self.config['batch_delays_seconds'][self.channel]
        record = {'slot_id': self.slot.slot_id, 'started_at_utc': text(began), 'batch': self.count,
                  'symbols': list(symbols), 'controlled_inflight_overlap': overlap, 'delay_seconds': delay}
        append_line(self.folder / (self.channel + '_calls.jsonl'), record)
        self.stop.wait(delay)
        millis = int(began.timestamp() * 1000)
        payload = {}
        for n, symbol in enumerate(symbols):
            price = 10 + int(symbol[1:]) / 100 + self.count / 1000
            payload[symbol] = {'symbol': symbol, 'assetMainType': 'EQUITY', 'assetSubType': 'COE', 'realtime': False,
                'reference': {'exchange': 'SYNTHETIC', 'description': 'Offline synthetic instrument ' + symbol},
                'quote': {'lastPrice': price, 'bidPrice': price - .01, 'askPrice': price + .01, 'mark': price,
                    'lastSize': 100, 'bidSize': 10, 'askSize': 20, 'totalVolume': self.count * 100 + n,
                    'quoteTime': millis, 'tradeTime': millis, 'securityStatus': 'Synthetic offline quote',
                    'openPrice': 10, 'highPrice': price + 1, 'lowPrice': 9, 'closePrice': 10},
                'regular': {'regularMarketLastPrice': price, 'regularMarketTradeTime': millis},
                'fundamental': {'sharesOutstanding': 10000000, 'avg10DaysVolume': 250000}}
        return SyntheticResponse(payload)


def load_poller(folder, channel, stop, *, initialize=True):
    from mb_market_data.sampling_acquisition import AcquisitionSettings
    from mb_market_data.sampling_incremental import IncrementalSamplingPoller
    from mb_market_data.sampling_membership import SamplingChannel
    from mb_market_data.watchlist_polling import PollWindow
    config, clock = load_config(folder)
    client = SyntheticClient(folder, channel, config, clock, stop)
    settings = AcquisitionSettings(SamplingChannel(channel), GRIDS[channel],
        PollWindow(datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])),
        400, 'all', CONTRACT, .75, .05)

    class InstrumentedPoller(IncrementalSamplingPoller):
        def sample(self, slot, **kwargs):
            client.slot = slot
            value = super().sample(slot, **kwargs)
            value['full_reconstructions'] = self.full_reconstructions
            return value

    poller = InstrumentedPoller(store_for(folder, config), settings=settings, client=client, clock=clock)
    if initialize:
        poller.initialize()
    return poller, config, clock


def load_control(folder):
    from mb_watchlist_coordinator.sampling_admission import AdmissionPolicy
    from mb_watchlist_coordinator.sampling_journal import SamplingJournal
    from mb_watchlist_coordinator.sampling_policy import SamplingPolicy
    from mb_watchlist_coordinator.sampling_projection import SamplingMembership
    from mb_watchlist_coordinator.sampling_request_store import SamplingRequestStore
    from mb_watchlist_coordinator.sampling_service import PublicationSettings, SamplingPublicationService
    config, clock = load_config(folder)
    store = store_for(folder, config)
    baseline = SamplingMembership(frozenset(config['opening_uni']), frozenset(config['opening_focus']), frozenset())
    admission = AdmissionPolicy(CONTRACT + '-catalog', frozenset(config['opening_uni'] + [config['outside_symbol']]),
                               frozenset({'manual', 'algorithm', 'synthetic-halt'}), 1, 'session')
    ledger = SamplingRequestStore(Path(folder) / 'requests.sqlite3', session_date=store.session_date, baseline=baseline,
        admission=admission, policy=SamplingPolicy('review-super-user', 2., 4., 'immediate_cap'), clock=clock)
    ledger.initialize()
    service = SamplingPublicationService(Path(folder) / 'publication.sqlite3', request_store=ledger,
        journal=SamplingJournal(store), settings=PublicationSettings(CONTRACT, .25, .5, datetime.fromisoformat(config['end'])), clock=clock)
    service.initialize()
    return ledger, service


def submit(ledger, config, owner, request_id, symbols, channel):
    from mb_watchlist_coordinator.models import ProducerIntent, IntentType
    result = ledger.submit_request(ProducerIntent(request_id, owner, IntentType.ENSURE_PRESENT, frozenset(symbols),
        ledger.clock(), expires_at=datetime.fromisoformat(config['end']), source_event_id=CONTRACT + ':' + request_id,
        metadata={'session_date': ledger.session_date.isoformat(), 'sampling_channel': channel, 'synthetic': True}), actor_id=owner)
    require(result.event.accepted, 'Synthetic request rejected: ' + str(result.event.rejection_reason))
    return result


def worker(folder, mode):
    import threading
    from unittest.mock import patch
    config, clock = load_config(folder)
    stop = Event()

    def watcher():
        while not stop.wait(.05):
            if (folder / 'STOP').exists():
                stop.set()
    threading.Thread(target=watcher, daemon=True).start()

    def status(value):
        append_line(folder / (mode + '_status.jsonl'), dict(value, pid=os.getpid(), at_utc=text(clock())))
    try:
        if mode == 'coordinator':
            _, service = load_control(folder)
            status({'status': 'ready'})
            status(service.run(stop, on_status=status))
        else:
            poller, _, _ = load_poller(folder, mode, stop)
            startup_reconstructions = poller.full_reconstructions
            status({'status': 'ready', 'startup_full_reconstructions': startup_reconstructions})

            class ClockDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return clock().astimezone(tz or timezone.utc)

            with patch('mb_market_data.schwab_quotes.datetime', ClockDatetime):
                result = poller.run_loop(stop, on_status=status)
            status(dict(result, full_reconstructions=poller.full_reconstructions,
                        no_full_scan_during_polling=poller.full_reconstructions == startup_reconstructions))
        return 0
    finally:
        stop.set()


def expected_slots(config, channel):
    start, end = datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])
    result = []
    at = start
    while at < end:
        if at.second in GRIDS[channel]:
            result.append(channel + ':' + at.isoformat())
        at += timedelta(seconds=1)
    return result


def metrics(rows):
    from mb_market_data.hot_provider_trial import growth_timings, timing
    keys = ('cycle_seconds', 'collector_seconds', 'journal_seconds', 'verification_seconds',
            'before_dispatch_gate_seconds', 'before_api_gate_seconds', 'recovery_before_slot_seconds')
    return {'timings': {key: timing([r[key] for r in rows if key in r]) for key in keys},
            'growth': growth_timings(rows)}


def check_children(folder, children, config, clock):
    for mode, child in children.items():
        code = child.poll()
        if code is None:
            continue
        rows = lines(folder / (mode + '_status.jsonl'))
        # Native pollers finish after their last scheduled slot, not at the
        # common window end (Uni at :30, Focus at :50, Hot at :58).
        last_slot = (datetime.fromisoformat(expected_slots(config, mode)[-1].split(':', 1)[1])
                     if mode != 'coordinator' else datetime.fromisoformat(config['end']))
        expected = 'window_ended' if mode != 'coordinator' else 'closed'
        require(code == 0 and rows and rows[-1]['status'] == expected and clock() >= last_slot,
                mode + ' exited unexpectedly; inspect its console file')


def analyse(folder, trace, config, children, elapsed):
    from mb_market_data.hot_provider_trial import replay_digest
    from mb_market_data.quote_journal_audit import audit_quote_journal
    from mb_market_data.sampling_acquisition import digest
    from mb_watchlist_coordinator.sampling_requests import plain
    store = store_for(folder, config)
    end = datetime.fromisoformat(config['end'])
    audit = audit_quote_journal(store.database_path, audited_at=end)
    first, second = replay_digest(store.database_path), replay_digest(store.database_path)
    failures = []
    if not audit.passed:
        failures.append('Journal audit failed: ' + str(audit.findings))
    if any(set(c.status_counts) != {'quote'} for c in audit.channels if c.acquisition_count):
        failures.append('Synthetic acquisitions contain unavailable quote outcomes')
    if first != second:
        failures.append('Repeated acquisition-aware replays differ')
    channels = {}
    for channel in ('uni', 'focus', 'hot'):
        rows = lines(folder / (channel + '_status.jsonl'))
        acquired = [r for r in rows if r['status'] in ('acquired', 'empty_membership')]
        # PollSlot identifiers use ET offsets; compare parsed instants, not strings.
        actual = [datetime.fromisoformat(r['slot_id'].split(':', 1)[1]) for r in acquired]
        planned = [datetime.fromisoformat(s.split(':', 1)[1]) for s in expected_slots(config, channel)]
        complete = len(actual) == len(planned) and set(actual) == set(planned)
        clean = bool(rows) and rows[-1]['status'] == 'window_ended' and rows[-1].get('no_full_scan_during_polling') is True
        poller, _, _ = load_poller(folder, channel, Event(), initialize=False)
        snapshot = poller.snapshot()
        if not complete or not clean or snapshot['pending'] is not None:
            failures.append(channel + ' coverage, steady-state scan or pending-outcome check failed')
        channels[channel] = dict(metrics(acquired), expected_slots=len(planned), committed_slots=len(acquired),
            missing_slots=len(set(planned) - set(actual)), complete=complete,
            acquisitions=sum(r['status'] == 'acquired' for r in acquired),
            empty_skips=sum(r['status'] == 'empty_membership' for r in acquired),
            retries=sum(r['status'] == 'retrying' for r in rows),
            missed_gate_events=[r for r in rows if r['status'] == 'missed'],
            no_full_scan_during_polling=clean, synthetic_batch_calls=len(lines(folder / (channel + '_calls.jsonl'))))
    revisions = store.membership_revisions_in_effective_order()
    target = config['outside_symbol']
    final = revisions[-1]
    if not (len(final.uni_symbols) == 555 and final.focus_symbols == tuple(config['opening_focus'])
            and final.hot_symbols == tuple(config['initial_hot'])):
        failures.append('Final hierarchy or session Uni retention differs')
    target_trace = [(target in r.uni_symbols, target in r.focus_symbols, target in r.hot_symbols) for r in revisions]
    compact = [v for n, v in enumerate(target_trace) if n == 0 or v != target_trace[n - 1]]
    if compact != [(False, False, False), (True, True, True), (True, False, False), (True, True, True), (True, False, False)]:
        failures.append('Admission/cap/expiry/demotion trace differs: ' + str(compact))
    overlap_call = next((r for r in lines(folder / 'hot_calls.jsonl') if r['controlled_inflight_overlap']), None)
    acquisitions = store.acquisitions_in_replay_order()
    overlapping = next((a for a in acquisitions if overlap_call and a.slot_id == overlap_call['slot_id']), None)
    published_during = []
    if overlapping:
        # published_at is actual UTC while this review's event clock has a future offset.
        published_during = [r.revision for r in revisions if overlapping.dispatched_at_utc < r.published_at + timedelta(seconds=config['utc_offset_seconds']) < overlapping.completed_at_utc]
    if not published_during:
        failures.append('No Coordinator revision was published during the controlled in-flight Hot response')
    if overlapping and overlapping.channel_revision >= min(published_during, default=overlapping.channel_revision + 1):
        failures.append('Pending Hot response was rebound to the later published revision')
    ledger, service = load_control(folder)
    before = [s.payload() for s in ledger.states()]
    reopened, reopened_service = load_control(folder)
    request_replay_same = before == [s.payload() for s in reopened.states()]
    publication_replay_same = plain(service.snapshot()) == plain(reopened_service.snapshot())
    if not request_replay_same or not publication_replay_same:
        failures.append('Request/publication restart replay differs')
    if not trace or not any(t['action'] == 'withdraw_manual_hot' and t.get('other_source_kept_hot') for t in trace):
        failures.append('Independent source withdrawal was not demonstrated')
    for name in MODES:
        if children[name].returncode != 0:
            failures.append(name + ' process failed')
    if len({c.pid for c in children.values()}) != 4 or os.getpid() in {c.pid for c in children.values()}:
        failures.append('Workers are not four independent processes')
    first_target = next((a for a in acquisitions if a.channel == 'hot' and a.channel_revision in
        {r.revision for r in revisions if target in r.hot_symbols}), None)
    report = {'result': 'PASS' if not failures else 'FAILED', 'failures': failures, 'contract': CONTRACT,
        'source': 'synthetic_only', 'timing_clock': 'actual elapsed wall-clock speed with labeled future-session offset',
        'duration_seconds': config['duration_seconds'], 'elapsed_seconds_including_startup': round(elapsed, 3),
        'logical_start': config['start'], 'logical_end': config['end'], 'actual_completed_utc': text(utc_now()),
        'pids': {k: c.pid for k, c in children.items()}, 'channels': channels, 'trace': trace,
        'hierarchy_revisions': len(revisions), 'target_membership_trace': compact,
        'published_during_hot_request': published_during,
        'inflight_hot_revision': overlapping.channel_revision if overlapping else None,
        'first_target_hot_revision': first_target.channel_revision if first_target else None,
        'audit_passed': audit.passed, 'table_counts': dict(audit.table_counts),
        'replays_identical': first == second, 'journal_replay': first,
        'request_replay_identical': request_replay_same, 'publication_replay_identical': publication_replay_same,
        'request_replay_sha256': digest(before), 'final_membership_counts': {'uni': len(final.uni_symbols),
            'focus': len(final.focus_symbols), 'hot': len(final.hot_symbols)},
        'coordinator_commit': COORDINATOR_COMMIT, 'network_disabled': True, 'live_quote_calls': 0,
        'limitations': ['Synthetic response timing and data, not Schwab or market capacity',
            'All three channels use the native guarded incremental collector; production Uni/Focus use their existing launcher',
            'Direct synthetic halt-source requests; no live Nasdaq transport or adapter latency claim',
            'No full-session history prefill, production journal access or production setting adoption']}
    write_json(folder / 'report.json', report)
    summary = ['COMBINED SYNTHETIC LOAD: ' + report['result'],
        'Four independent processes: Coordinator + Uni + Focus + Hot; actual elapsed scheduling.',
        'Opening Uni 554 / Focus 40; initial Hot request 40. Final: ' + str(report['final_membership_counts']),
        'Admission, independent source withdrawal, temporary cap, expiry reentry and delayed demotion: ' + ('PASS' if not failures else 'see findings'),
        'Revision published during in-flight Hot response: ' + str(published_during),
        'Audit: ' + str(audit.passed) + '; identical journal replays: ' + str(first == second)]
    for channel, value in channels.items():
        summary.append(channel.upper() + ': committed=' + str(value['committed_slots']) + '/' + str(value['expected_slots'])
            + '; missing=' + str(value['missing_slots']) + '; batches=' + str(value['synthetic_batch_calls']))
        summary.append('  cycle mean/p95/max ms: ' + str(value['timings']['cycle_seconds']))
        summary.append('  verification mean/p95/max ms: ' + str(value['timings']['verification_seconds']))
    summary += ['Journal replay SHA-256: ' + first['sequence_sha256'], *failures, *report['limitations']]
    (folder / 'summary.txt').write_text('\n'.join(summary) + '\n', encoding='utf-8')
    return report


def run(folder, duration, *, on_status=print):
    config = prepare(folder, duration)
    folder = Path(folder)
    write_json(ROOT / 'runs/active_combined.json', {'contract': CONTRACT, 'folder': str(folder.resolve())})
    _, clock = load_config(folder)
    ledger, _ = load_control(folder)
    children, handles, trace = {}, [], []
    started = time.perf_counter()
    finish = time.monotonic() + duration + 60
    external_stop = False
    try:
        for mode in MODES:
            handle = (folder / (mode + '_console.txt')).open('w', encoding='utf-8')
            handles.append(handle)
            children[mode] = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'worker.py'), str(folder), mode],
                                               stdout=handle, stderr=subprocess.STDOUT)
        start = datetime.fromisoformat(config['start'])
        end = datetime.fromisoformat(config['end'])
        actions = [(.10, 'algorithm_focus'), (.20, 'manual_hot'), (.30, 'halt_hot'), (.40, 'withdraw_manual_hot'),
                   (.50, 'temporary_cap'), (.75, 'withdraw_algorithm'), (.80, 'withdraw_halt')]
        done = set()
        next_progress = time.monotonic()
        while clock() < end:
            if (folder / 'STOP').exists():
                external_stop = True
                break
            require(time.monotonic() < finish, 'Experiment exceeded bounded runtime')
            check_children(folder, children, config, clock)
            elapsed = (clock() - start).total_seconds()
            if elapsed >= -2:
                require(all(any(r['status'] == 'ready' for r in lines(folder / (m + '_status.jsonl'))) for m in MODES),
                        'Workers were not ready before the opening; no late catch-up')
            for fraction, action in actions:
                if action in done or elapsed < duration * fraction:
                    continue
                target = config['outside_symbol']
                if action == 'manual_hot':
                    calls = [r for r in lines(folder / 'hot_calls.jsonl') if r['controlled_inflight_overlap']]
                    if not calls:
                        require(elapsed < duration * fraction + 2, 'Controlled Hot overlap slot was not dispatched')
                        continue
                if action == 'algorithm_focus':
                    submit(ledger, config, 'algorithm', action, [config['algorithm_symbol']], 'focus')
                elif action == 'manual_hot':
                    submit(ledger, config, 'manual', action, [target], 'hot')
                elif action == 'halt_hot':
                    submit(ledger, config, 'synthetic-halt', action, [target], 'hot')
                elif action == 'temporary_cap':
                    ledger.restrict_symbol('temporary-cap', symbol=target, max_group='uni', actor_id='review-super-user',
                        expires_at=start + timedelta(seconds=duration * .65), reason='Synthetic restriction review')
                elif action in ('withdraw_manual_hot', 'withdraw_algorithm', 'withdraw_halt'):
                    owner, rid = {'withdraw_manual_hot': ('manual', 'manual_hot'),
                        'withdraw_algorithm': ('algorithm', 'algorithm_focus'), 'withdraw_halt': ('synthetic-halt', 'halt_hot')}[action]
                    ledger.cancel_request(action, owner_id=owner, request_id=rid, actor_id=owner)
                record = {'action': action, 'at_utc': text(clock()), 'elapsed_seconds': round(elapsed, 3)}
                if action == 'withdraw_manual_hot':
                    record['other_source_kept_hot'] = target in ledger.states()[-1].applied.hot
                trace.append(record)
                append_line(folder / 'actions.jsonl', record)
                on_status('  Scenario: ' + action)
                done.add(action)
            if time.monotonic() >= next_progress:
                counts = {c: sum(r['status'] == 'acquired' for r in lines(folder / (c + '_status.jsonl'))) for c in ('uni', 'focus', 'hot')}
                on_status(('Waiting for four workers/opening' if elapsed < 0 else f'Elapsed {elapsed:.0f}/{duration}s') + '; acquisitions=' + str(counts))
                next_progress = time.monotonic() + 10
            time.sleep(.025)
        if external_stop:
            return {'result': 'STOPPED', 'contract': CONTRACT, 'trace': trace, 'live_quote_calls': 0}
        require(len(done) == len(actions), 'Scenario did not complete all inputs')
        for mode, child in children.items():
            child.wait(timeout=8)
        return analyse(folder, trace, config, children, time.perf_counter() - started)
    finally:
        # Only this parent's explicit Popen handles can be terminated; never inspect
        # or stop production PIDs, window titles or other Python processes.
        (folder / 'STOP').write_text('experiment stop\n', encoding='utf-8')
        for child in children.values():
            if child.poll() is None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        for handle in handles:
            handle.close()


def validate_run_path(folder):
    folder = Path(folder).resolve()
    require(folder.is_relative_to((ROOT / 'runs/combined').resolve()) and folder.parent == (ROOT / 'runs/combined').resolve(),
            'Worker/stop path must identify a direct experiment run')
    require(folder.is_dir(), 'Experiment run is missing')
    load_config(folder)
    return folder


def request_stop(root=ROOT):
    pointer = root / 'runs/active_combined.json'
    if not pointer.exists():
        return 'No combined experiment has been started in this package.'
    value = read_json(pointer)
    require(value.get('contract') == CONTRACT, 'Active pointer contract differs')
    folder = Path(value['folder']).resolve()
    require(folder.parent == (root / 'runs/combined').resolve(), 'Active pointer path escapes experiment runs')
    load_config(folder)
    result = folder / 'result.json'
    if result.exists():
        return 'Experiment already finished: ' + read_json(result)['result']
    (folder / 'STOP').write_text('operator requested experiment stop\n', encoding='utf-8')
    return 'Stop requested for this experiment only. Results: ' + str(folder)


def check_verification(root=ROOT):
    gate = read_json(root / 'runs/combined_verification.json')
    manifest_sha = hashlib.sha256((root / 'experiments/combined_load_20261019/source_manifest.json').read_bytes()).hexdigest()
    report_path = Path(gate['report']).resolve()
    require(report_path.is_relative_to((root / 'runs/combined').resolve()) and report_path.name == 'report.json',
            'Verification report must belong to this package')
    require(gate.get('result') == 'PASS' and gate.get('source_manifest_sha256') == manifest_sha,
            'Run 09_verify_combined.cmd on this unchanged package first')
    require(hashlib.sha256(report_path.read_bytes()).hexdigest() == gate.get('report_sha256'), 'Verification report changed')
    report = read_json(report_path)
    require(report['result'] == 'PASS' and report['contract'] == CONTRACT and report['duration_seconds'] == 60
            and report['audit_passed'] is True and report['replays_identical'] is True
            and all(v['complete'] for v in report['channels'].values()), 'Verification did not complete its healthy coverage proof')
    return report
