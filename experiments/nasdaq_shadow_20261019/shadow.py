"""Bounded live Nasdaq shadow integration. All paths belong to one fresh run.

The pinned producer/publisher/collector remain unchanged. The selective delivery
bridge uses the producer's existing command and ACK validation, after a separate
durable shadow gate. This gate is review policy, not a production authority.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.request import Request, urlopen
import uuid
import xml.etree.ElementTree as XML
from zoneinfo import ZoneInfo
from feed_capture import FeedResponse, FeedUnavailable, fetch_feed, inspect_feed, feed_health

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTRACT = 'nasdaq-shadow-v1'
PIN_COMMIT = '0417b345e2725f4e1e184d3b3202bedb846dbfca'
SOURCE_ID = 'nasdaq-shadow'
GRID = tuple(range(0, 60, 2))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def utc_now():
    return datetime.now(timezone.utc)


def text(at):
    require(at.tzinfo is not None, 'Aware timestamp required')
    return at.astimezone(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def append_line(path, value):
    with Path(path).open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def lines(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').split('\n')[:-1] if line]


def bootstrap():
    pin = read_json(HERE / 'coordinator_pin.json')
    require(pin['commit'] == PIN_COMMIT, 'Coordinator pin changed')
    folder = ROOT / 'coordinator_src/mb_watchlist_coordinator'
    require({p.name for p in folder.glob('*.py')} == set(pin['files']), 'Coordinator inventory changed')
    for name, checksum in pin['files'].items():
        require(Path(name).name == name and sha((folder / name).read_bytes()) == checksum,
                'Coordinator bytes changed: ' + name)
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'coordinator_src')]


def deny_network():
    def denied(*args, **kwargs):
        raise RuntimeError('Offline Nasdaq review: network disabled')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = denied


def check_live_window(now, preset='short'):
    from mb_market_data.watchlist_polling import ET
    local = now.astimezone(ET)
    require(preset in ('short', 'extended'), 'Unknown shadow preset')
    latest = '15:25' if preset == 'extended' else '15:40'
    require(local.weekday() < 5 and '09:30' <= local.strftime('%H:%M') < latest,
            'Launch during regular hours: 09:30 <= ET < ' + latest + '; operator confirms a normal session')


class SecureHorizon:
    request_timeout_seconds = 3

    def __init__(self, preset='short'):
        require(preset in ('short', 'extended'), 'Unknown shadow preset')
        self.preset = preset

    def required_through(self, now):
        return now + timedelta(minutes=35 if self.preset == 'extended' else 20)

    def check_session(self, now):
        check_live_window(now, self.preset)


def fetch_bytes(url, *, limit=4_000_000):
    """Only fixed Nasdaq URLs, bounded body/timeout, no automatic application retry."""
    from mb_market_data.nasdaq_halts import build_url
    from mb_market_data.nasdaq_symbol_directory import NASDAQ_LISTED_URL, OTHER_LISTED_URL
    require(url in (build_url(), NASDAQ_LISTED_URL, OTHER_LISTED_URL), 'Unexpected Nasdaq URL')
    request = Request(url, headers={'User-Agent': 'mb_market_data bounded Nasdaq shadow/1',
                                   'Accept': 'application/rss+xml,application/xml,text/plain'})
    with urlopen(request, timeout=3) as response:
        require(response.geturl() == url, 'Unexpected Nasdaq redirect')
        body = response.read(limit + 1)
    require(0 < len(body) <= limit, 'Nasdaq response size invalid')
    return body


def parse_snapshot(raw, *, capture_id, acquired_at, provenance):
    from mb_market_data.nasdaq_halts import normalize_nasdaq_xml, parse_records
    from mb_watchlist_coordinator.nasdaq_sampling import HaltSnapshot
    root = XML.fromstring(normalize_nasdaq_xml(raw))
    require(root.tag == 'rss' and root.find('channel') is not None,
            'Expected a complete RSS channel')
    records = parse_records(raw, retrieved_at_et=acquired_at, retrieval_mode='CURRENT')
    return HaltSnapshot(capture_id, acquired_at, 'CURRENT', sha(raw),
                        tuple(asdict(row) for row in records), provenance)


def catalog_from_bytes(nasdaq, other):
    """Exact plain symbols only; no inferred punctuation aliases or screen limits."""
    from mb_market_data.nasdaq_symbol_directory import build_symbol_directory_snapshot
    snapshot = build_symbol_directory_snapshot(nasdaq, other)
    by_symbol = {}
    for row in snapshot.normalized_rows:
        symbol = row['symbol']
        if (not re.fullmatch(r'[A-Z][A-Z0-9]{0,9}', symbol)
                or row['test_issue'] != 'N' or row['etf'] != 'N'):
            continue
        aliases = {row.get('cqs_symbol', ''), row.get('nasdaq_symbol', '')} - {''}
        if aliases - {symbol}:
            continue
        require(symbol not in by_symbol, 'Ambiguous directory symbol')
        by_symbol[symbol] = row
    require(by_symbol, 'No supported exact directory symbols')
    return by_symbol


def acquire_catalog(*, fetch=fetch_bytes, clock=utc_now):
    from mb_market_data.nasdaq_symbol_directory import NASDAQ_LISTED_URL, OTHER_LISTED_URL
    bodies, receipts = {}, {}
    for label, url in [('nasdaq', NASDAQ_LISTED_URL), ('other', OTHER_LISTED_URL)]:
        began = clock()
        bodies[label] = fetch(url)
        receipts[label] = {'url': url, 'started_at_utc': text(began),
                           'acquired_at_utc': text(clock()), 'sha256': sha(bodies[label])}
    return catalog_from_bytes(bodies['nasdaq'], bodies['other']), bodies, receipts


def load_roster(opening_path, focus_path, *, now=None):
    from mb_market_data.sampling_membership import SamplingHierarchyRevision
    from mb_market_data.watchlist_polling import ET
    now = now or utc_now()
    revisions, hashes = [], {}
    for label, path in [('opening', opening_path), ('focus', focus_path)]:
        path = Path(path)
        require(path.is_file() and path.suffix.lower() == '.json' and path.stat().st_size <= 2_000_000,
                'Use existing small hierarchy proposal JSON files')
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8-sig'))
        revision = SamplingHierarchyRevision(session_date=datetime.fromisoformat(value['session_date']).date(),
            revision=value['revision'], effective_at=datetime.fromisoformat(value['effective_at']),
            uni_symbols=value['uni_symbols'], focus_symbols=value['focus_symbols'], hot_symbols=value['hot_symbols'],
            source=value['source'], reason=value.get('reason'), metadata=value.get('metadata', {}))
        revisions.append(revision)
        hashes[label] = sha(raw)
    r0, r1 = revisions
    require(r0.session_date == r1.session_date == now.astimezone(ET).date(), 'Same-day proposals required')
    require(r0.revision == 0 and r1.revision == 1 and r0.uni_symbols == r1.uni_symbols
            and not r0.focus_symbols and not r0.hot_symbols and not r1.hot_symbols
            and 1 <= len(r0.uni_symbols) <= 600 and len(r1.focus_symbols) == 40, 'Opening hierarchy differs')
    require(r0.effective_at == r1.effective_at and r0.effective_at.astimezone(ET).date() == r0.session_date
            and r0.effective_at.astimezone(ET).strftime('%H:%M:%S.%f') == '09:30:00.000000',
            'Opening effective boundary differs')
    return {'opening_uni': list(r0.uni_symbols), 'opening_focus': list(r1.focus_symbols), 'input_sha256': hashes}


def policy(source, preset='short'):
    require(source in ('synthetic', 'live'), 'Explicit source required')
    require(preset in ('short', 'extended'), 'Unknown shadow preset')
    extended = preset == 'extended'
    observation = 1830 if extended else 900 if source == 'live' else 16
    return {'contract': CONTRACT, 'source': source, 'source_id': SOURCE_ID, 'preset': preset,
            'reason_codes': ['LUDP', 'M'], 'startup': 'first_successful_current_snapshot_baseline_only',
            'request_lifetime': 'regular_close', 'resumption': 'update_identity_without_withdrawal',
            'uni_retention': 'session', 'max_admitted_symbols': 1, 'max_additional_uni': 1,
            'catalog_policy': 'fresh_directory_non_test_non_etf_exact_plain_symbol',
            'broker_resolution': 'exact_EQUITY_COE_quote_identity_not_tradability',
            # Nasdaq's TradeHaltRSS guidance: updates/queries once per minute.
            # The fast synthetic fixture has no network or source latency claim.
            'feed_interval_seconds': 60 if source == 'live' or extended else 2,
            'feed_timeout_seconds': 3, 'max_feed_attempts': 31 if extended else 15 if source == 'live' else 450,
            'feed_failure_limit': 3, 'non_feed_is_unknown': True,
            'feed_http_stop_codes': [401, 403, 429], 'feed_redirects': 'refuse',
            'max_resolution_calls': 3, 'max_hot_calls': 450 if extended else 30,
            'max_total_quote_calls': 453 if extended else 33,
            'observation_seconds': observation,
            'discovery_seconds': 900 if extended else observation,
            'publication_drain_seconds': 30 if extended else 0,
            'hot_tail_seconds': 900 if extended else 60 if source == 'live' else 6,
            'hot_seconds': list(GRID), 'dispatch_lateness_seconds': .75,
            'coordinator_poll_seconds': .25, 'publication_lead_seconds': .5,
            'demotion_grace_seconds': 60., 'minimum_residence_seconds': 120.,
            'review_only': True, 'production_hot': False}


def prepare(folder, roster, catalog, *, source, preset='short', catalog_bodies=None, catalog_receipts=None, clock=utc_now, start=None, clock_offset=0.):
    from mb_market_data.quote_observation_store import QuoteObservationStore
    from mb_market_data.sampling_acquisition import MARKER
    from mb_market_data.sampling_incremental import INCREMENTAL_MARKER
    from mb_market_data.sampling_membership import SamplingHierarchyRevision
    folder = Path(folder).resolve()
    require(not folder.exists(), 'A fresh experimental run is required')
    require(not any(part.startswith('quote_observation_journal_v2_api_opening_') for part in folder.parts),
            'Production journals are excluded')
    folder.mkdir(parents=True)
    config = policy(source, preset)
    start = start or (clock() + timedelta(seconds=20)).replace(microsecond=0)
    if start.second % 2:
        start += timedelta(seconds=1)
    config.update(roster, catalog_symbols=sorted(catalog), catalog_sha256=sha(canonical(catalog).encode()),
                  clock_offset_seconds=clock_offset,
                  run_id=folder.name, run_root=str(folder), start=text(start),
                  end=text(start + timedelta(seconds=config['observation_seconds'])))
    write_json(folder / 'catalog.json', catalog)
    write_json(folder / 'catalog_receipts.json', catalog_receipts or {'source': 'synthetic'})
    for name, raw in (catalog_bodies or {}).items():
        require(name in ('nasdaq', 'other'), 'Unknown directory source')
        (folder / (name + '_directory.txt')).write_bytes(raw)
    write_json(folder / 'config.json', config)
    write_json(folder / 'identity.json', {'contract': CONTRACT, 'run_root': str(folder),
        'run_id': folder.name, 'config_sha256': sha((folder / 'config.json').read_bytes())})
    store = QuoteObservationStore(folder / 'quotes.sqlite3', session_date=start.astimezone(ZoneInfo('America/New_York')).date(), schema_version=2)
    store.initialize()
    store.record_membership_revision(SamplingHierarchyRevision(session_date=store.session_date, revision=0,
        effective_at=clock() + timedelta(seconds=1), uni_symbols=tuple(config['opening_uni']),
        focus_symbols=tuple(config['opening_focus']), hot_symbols=(), source=CONTRACT,
        metadata={MARKER: True, INCREMENTAL_MARKER: True, 'review_only': True,
                  'quote_source': source, 'live_membership_source': source == 'live'}))
    with closing(sqlite3.connect(folder / 'budget.sqlite3')) as connection, connection:
        connection.execute('CREATE TABLE permit(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,key TEXT NOT NULL UNIQUE,at_utc TEXT NOT NULL)')
        for operation in ('UPDATE', 'DELETE'):
            connection.execute('CREATE TRIGGER permit_no_' + operation.lower() + ' BEFORE ' + operation
                + " ON permit BEGIN SELECT RAISE(ABORT,'immutable call permits'); END")
    ShadowGate(folder, config, clock).initialize()
    ledger, _ = load_control(folder, clock=clock)
    producer_for(folder, ledger, clock=clock).initialize()
    # Install schema guards before the independent workers pin their schema epoch.
    poller_for(folder, None, clock=clock).initialize()
    return config


def load_config(folder):
    folder = Path(folder).resolve()
    config = read_json(folder / 'config.json')
    require(read_json(folder / 'identity.json') == {'contract': CONTRACT, 'run_root': str(folder),
        'run_id': folder.name, 'config_sha256': sha((folder / 'config.json').read_bytes())}, 'Run identity changed')
    require(all(config.get(k) == v for k, v in policy(config['source'], config['preset']).items()), 'Shadow policy changed')
    catalog = read_json(folder / 'catalog.json')
    require(sha(canonical(catalog).encode()) == config['catalog_sha256'] and sorted(catalog) == config['catalog_symbols'],
            'Catalog changed')
    start, end = datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])
    require(start.tzinfo is not None and end - start == timedelta(seconds=config['observation_seconds'])
            and not start.microsecond and start.second in GRID, 'Run window changed')
    require(config['source'] != 'live' or config['clock_offset_seconds'] == 0., 'Live clock cannot be shifted')
    return config


def config_clock(config):
    return lambda: utc_now() + timedelta(seconds=config['clock_offset_seconds'])


def store_for(folder, config):
    from mb_market_data.quote_observation_store import QuoteObservationStore
    from mb_market_data.watchlist_polling import ET
    return QuoteObservationStore(Path(folder) / 'quotes.sqlite3',
        session_date=datetime.fromisoformat(config['start']).astimezone(ET).date(), schema_version=2)


def load_control(folder, *, clock=None):
    from mb_watchlist_coordinator.sampling_admission import AdmissionPolicy
    from mb_watchlist_coordinator.sampling_journal import SamplingJournal
    from mb_watchlist_coordinator.sampling_policy import SamplingPolicy
    from mb_watchlist_coordinator.sampling_projection import SamplingMembership
    from mb_watchlist_coordinator.sampling_request_store import SamplingRequestStore
    from mb_watchlist_coordinator.sampling_service import PublicationSettings, SamplingPublicationService
    config = load_config(folder)
    clock = clock or config_clock(config)
    store = store_for(folder, config)
    baseline = SamplingMembership(frozenset(config['opening_uni']), frozenset(config['opening_focus']), frozenset())
    admission = AdmissionPolicy('shadow-catalog-' + config['catalog_sha256'], frozenset(config['catalog_symbols']),
                               frozenset({SOURCE_ID}), 1, 'session')
    ledger = SamplingRequestStore(Path(folder) / 'requests.sqlite3', session_date=store.session_date,
        baseline=baseline, admission=admission, policy=SamplingPolicy('shadow-review-authority', 60., 120., 'immediate_cap'), clock=clock)
    ledger.initialize()
    service = SamplingPublicationService(Path(folder) / 'publication.sqlite3', request_store=ledger,
        journal=SamplingJournal(store), settings=PublicationSettings(CONTRACT, .25, .5,
            datetime.fromisoformat(config['end'])), clock=clock)
    service.initialize()
    return ledger, service


def producer_for(folder, ledger, *, clock=None):
    from mb_watchlist_coordinator.nasdaq_sampling import HaltProducerSettings, NasdaqSamplingProducer
    clock = clock or config_clock(load_config(folder))
    return NasdaqSamplingProducer(Path(folder) / 'nasdaq.sqlite3', request_store=ledger,
        settings=HaltProducerSettings(SOURCE_ID, ('LUDP', 'M'), 'regular_close', False), clock=clock)


def deliver_selected(producer, event_id, *, after_submit=None):
    """Existing producer ACK contract, restricted to a gate-approved command.

    Other observed events retain explicit terminal shadow dispositions. They do
    not become Coordinator requests and are never silently marked delivered.
    """
    from mb_watchlist_coordinator.nasdaq_sampling import _intent
    from mb_watchlist_coordinator.sampling_requests import canonical as encode, digest
    with closing(sqlite3.connect(producer.path, timeout=5)) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        _, commands, _, delivered = producer._read(connection)
        require(event_id in commands, 'Unknown Nasdaq command')
        if event_id in delivered:
            return None
        command = commands[event_id]
        result = producer.request_store.submit_request(_intent(command), actor_id=producer.settings.producer_id)
        if after_submit:
            after_submit(result)
        ack = {'command_sha256': digest(command), 'request_event_sha256': result.event.event_sha256,
               'request_sequence': result.event.sequence, 'accepted': result.event.accepted,
               'rejection_reason': result.event.rejection_reason}
        connection.execute('INSERT INTO halt_delivery VALUES (?,?)', (event_id, encode(ack)))
        return result


class ShadowGate:
    """Hash-chained review dispositions. A reserved unresolved lookup is not retried."""
    def __init__(self, folder, config, clock=None):
        clock = clock or config_clock(config)
        self.folder, self.config, self.clock = Path(folder), config, clock
        self.path = self.folder / 'shadow_gate.sqlite3'
        self.identity = {'contract': CONTRACT, 'run_id': config['run_id'], 'catalog_sha256': config['catalog_sha256']}

    def initialize(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS gate_identity(payload TEXT NOT NULL)')
            rows = connection.execute('SELECT payload FROM gate_identity').fetchall()
            if not rows:
                connection.execute('INSERT INTO gate_identity VALUES(?)', (canonical(self.identity),))
            else:
                require(rows == [(canonical(self.identity),)], 'Shadow gate identity changed')
            connection.execute('CREATE TABLE IF NOT EXISTS gate_event(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        self.events()

    def events(self):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            require(connection.execute('SELECT payload FROM gate_identity').fetchall() == [(canonical(self.identity),)], 'Gate identity changed')
            result, previous = [], sha(canonical(self.identity).encode())
            for sequence, body, checksum in connection.execute('SELECT * FROM gate_event ORDER BY sequence'):
                value = json.loads(body)
                require(sequence == len(result) + 1 == value['sequence'] and value['previous_sha256'] == previous
                        and canonical(value) == body and sha(body.encode()) == checksum, 'Shadow gate history changed')
                result.append(value)
                previous = checksum
            return result

    def append(self, kind, event_id, **values):
        old = self.events()
        value = {'sequence': len(old) + 1, 'previous_sha256': sha(canonical(old[-1] if old else self.identity).encode()),
                 'kind': kind, 'event_id': event_id, 'at_utc': text(self.clock()), **values}
        body = canonical(value)
        with closing(sqlite3.connect(self.path, timeout=3)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            require(connection.execute('SELECT count(*) FROM gate_event').fetchone()[0] == len(old), 'Gate concurrently changed')
            connection.execute('INSERT INTO gate_event VALUES(?,?,?)', (value['sequence'], body, sha(body.encode())))
        return value

    def dispositions(self):
        result = {}
        for value in self.events():
            if value['kind'] == 'resolution_reserved':
                require(value['event_id'] not in result, 'Repeated resolution reservation')
                result[value['event_id']] = dict(value, disposition='resolution_outcome_unknown_no_retry')
            elif value['kind'] == 'decision':
                require(value['event_id'] not in result or result[value['event_id']]['kind'] == 'resolution_reserved',
                        'Repeated terminal decision')
                result[value['event_id']] = value
            else:
                raise ValueError('Unknown shadow gate receipt')
        return result

    def selected_symbol(self):
        selected = {d['symbol'] for d in self.dispositions().values() if d['disposition'] == 'approved'}
        require(len(selected) <= 1, 'Shadow symbol capacity exceeded')
        return next(iter(selected), None)

    def decide(self, event_id, command, client):
        previous = self.dispositions().get(event_id)
        if previous:
            return previous
        symbol = command['symbol']
        selected = self.selected_symbol()
        if selected and selected != symbol:
            return self.append('decision', event_id, symbol=symbol, disposition='shadow_capacity_rejected')
        if symbol not in self.config['catalog_symbols']:
            return self.append('decision', event_id, symbol=symbol, disposition='catalog_unresolved')
        if selected == symbol:
            return self.append('decision', event_id, symbol=symbol, disposition='approved', resolution='reuse_exact_session_identity')
        discovery_end = datetime.fromisoformat(self.config['start']) + timedelta(seconds=self.config['discovery_seconds'])
        if self.config['preset'] == 'extended' and self.clock() >= discovery_end:
            return self.append('decision', event_id, symbol=symbol, disposition='discovery_window_ended')
        permits = budget_counts(self.folder)
        if permits.get('resolution', 0) >= self.config['max_resolution_calls']:
            return self.append('decision', event_id, symbol=symbol, disposition='resolution_budget_rejected')
        self.append('resolution_reserved', event_id, symbol=symbol)
        response = client.quotes([symbol], fields='all', purpose='resolution', key=event_id)
        payload = response.json()
        raw_path = self.folder / ('resolution_' + event_id + '.json')
        write_json(raw_path, payload)
        item = payload.get(symbol) if isinstance(payload, dict) else None
        # Invalid/missing quote responses do not establish instrument identity or a cause.
        confirmed = (response.ok and isinstance(item, dict) and item.get('symbol') == symbol
                     and item.get('assetMainType') == 'EQUITY' and item.get('assetSubType') == 'COE')
        disposition = 'approved' if confirmed else 'broker_identity_unconfirmed'
        if self.config['preset'] == 'extended' and self.clock() >= discovery_end:
            disposition = 'discovery_window_ended'
        return self.append('decision', event_id, symbol=symbol,
            disposition=disposition,
            response_file=raw_path.name, response_sha256=sha(raw_path.read_bytes()))


def budget_counts(folder):
    with closing(sqlite3.connect(Path(folder) / 'budget.sqlite3', timeout=3)) as connection:
        return dict(connection.execute('SELECT kind,count(*) FROM permit GROUP BY kind'))


class QuoteClient:
    def __init__(self, folder, transport, *, clock=None, rejection=None):
        self.folder, self.config, self.transport = Path(folder), load_config(folder), transport
        clock = clock or config_clock(self.config)
        self.clock, self.rejection, self.slot = clock, rejection, None
        self.hot_window = HotWindow(folder, self.config)

    def check_hot_window(self):
        cutoff = self.hot_window.deadline()
        require(self.slot is not None and self.slot.scheduled_at < cutoff,
                'Hot slot is outside the exclusive shadow cutoff')
        require(self.clock() < cutoff, 'Shadow Hot deadline reached')

    def quotes(self, symbols, *, fields, purpose='hot', key=None):
        from mb_market_data.hot_provider_trial import SafeResponse
        require(purpose in ('hot', 'resolution') and fields == 'all' and len(symbols) == 1,
                'One explicitly captured symbol required')
        require(not (self.folder / 'STOP').exists() and self.clock() < datetime.fromisoformat(self.config['end'])
                and not (self.rejection and self.rejection.requested), 'Shadow stopped or authorization required')
        gate = ShadowGate(self.folder, self.config, self.clock)
        if purpose == 'hot':
            require(self.slot is not None and key is None and gate.selected_symbol() == symbols[0], 'Hot request lacks approved identity')
            self.check_hot_window()
            key = self.slot.slot_id
        with closing(sqlite3.connect(self.folder / 'budget.sqlite3', timeout=3)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            counts = dict(connection.execute('SELECT kind,count(*) FROM permit GROUP BY kind'))
            require(sum(counts.values()) < self.config['max_total_quote_calls']
                    and counts.get(purpose, 0) < self.config['max_' + purpose + '_calls'], 'Shadow quote budget exhausted')
            permit = connection.execute('INSERT INTO permit(kind,key,at_utc) VALUES(?,?,?)',
                (purpose, key, text(self.clock()))).lastrowid
        metric = {'purpose': purpose, 'key': key, 'permit': permit, 'symbol': symbols[0],
                  'started_at_utc': text(self.clock()), 'dispatched': False}
        began = time.perf_counter()
        try:
            if purpose == 'hot':
                self.check_hot_window()
                require(0 <= (self.clock() - self.slot.scheduled_at).total_seconds() <= .75, 'Hot dispatch gate elapsed')
            dispatch_until = self.hot_window.deadline() if purpose == 'hot' else datetime.fromisoformat(self.config['end'])
            require(not (self.folder / 'STOP').exists() and self.clock() < dispatch_until,
                    'Shadow stopped before dispatch')
            metric['dispatched'] = True
            response = self.transport.quotes(symbols, fields=fields)
            metric['status_code'] = int(response.status_code)
            if response.status_code in (401, 403, 429) or (self.rejection and self.rejection.requested):
                (self.folder / 'STOP').write_text('Authorization or rate-limit stop\n', encoding='utf-8')
            return SafeResponse(response, metric)
        except Exception as exc:
            metric['exception_type'] = type(exc).__name__
            if self.rejection and self.rejection.requested:
                (self.folder / 'STOP').write_text('Authorization required\n', encoding='utf-8')
            raise RuntimeError('Shadow quote transport failed (' + type(exc).__name__ + ')') from None
        finally:
            metric['returned_at_utc'] = text(self.clock())
            metric['elapsed_seconds'] = time.perf_counter() - began
            append_line(self.folder / (purpose + '_calls.jsonl'), metric)


def poller_for(folder, client, *, clock=None):
    from mb_market_data.sampling_acquisition import AcquisitionSettings
    from mb_market_data.sampling_incremental import IncrementalSamplingPoller
    from mb_market_data.sampling_membership import SamplingChannel
    from mb_market_data.watchlist_polling import PollWindow
    config = load_config(folder)
    clock = clock or config_clock(config)
    settings = AcquisitionSettings(SamplingChannel.HOT, GRID,
        PollWindow(datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])),
        400, 'all', CONTRACT, .75, .05)

    class InstrumentedPoller(IncrementalSamplingPoller):
        _shadow_stop = None

        def shadow_ended(self, slot, cutoff):
            if self._shadow_stop is not None:
                self._shadow_stop.set()
            return {'status': 'shadow_window_ended', 'slot_id': slot.slot_id,
                    'exclusive_cutoff_at_utc': text(cutoff)}

        def sample(self, slot, *, fault_hook=None):
            cutoff = self.shadow_window.deadline()
            if (self._shadow_stop is not None and config['preset'] == 'extended'
                    and not self.shadow_window.bound and slot.scheduled_at >= cutoff and clock() < cutoff):
                # Discovery can still approve a selection just before its end.
                if self._shadow_stop.wait_until(slot.scheduled_at):
                    return self.shadow_ended(slot, self.shadow_window.deadline())
                cutoff = self.shadow_window.deadline()
            # This is a scheduled-slot boundary, independent of the outer
            # wall-clock stop check. No dispatch/permit is recorded for it.
            if slot.scheduled_at >= cutoff or clock() >= cutoff:
                return self.shadow_ended(slot, cutoff)
            if self._shadow_stop is not None:
                # Relative monotonic waits may finish just before a coarse wall
                # clock tick. Recheck the absolute target before native sample.
                if self._shadow_stop.wait_until(slot.scheduled_at):
                    return self.shadow_ended(slot, self.shadow_window.deadline())
            if client:
                client.slot = slot
            def hook(stage):
                if stage == 'after_journal_commit':
                    append_line(Path(folder) / 'hot_commit.jsonl', {'slot_id': slot.slot_id,
                        'journal_commit_observed_at_utc': text(clock())})
                if fault_hook:
                    fault_hook(stage)
            return super().sample(slot, fault_hook=hook)

        def run_loop(self, stop, *, on_status=None):
            require(isinstance(stop, TailStop), 'Shadow loop requires the absolute slot/deadline stop adapter')
            self._shadow_stop = stop
            try:
                return super().run_loop(stop, on_status=on_status)
            finally:
                self._shadow_stop = None

    class NoTransport:
        def quotes(self, *args, **kwargs):
            raise RuntimeError('No quote transport in initialization')

    poller = InstrumentedPoller(store_for(folder, config), settings=settings, client=client or NoTransport(), clock=clock)
    poller.shadow_window = HotWindow(folder, config)
    return poller


def capture_events(folder):
    """Verify successful and unavailable captures, including retained HTTP evidence."""
    folder = Path(folder).resolve()
    log = folder / 'feed_captures.jsonl'
    if not log.exists():
        require(not list((folder / 'captures').glob('capture-*')), 'Unreceipted feed capture')
        return []
    body = log.read_bytes()
    require(not body or body.endswith(b'\n'), 'Incomplete feed receipt line')
    events, seen = [], set()
    for receipt in [json.loads(row) for row in body.splitlines()]:
        capture_id = receipt['capture_id']
        require(re.fullmatch(r'capture-[0-9a-f]{32}', capture_id) and capture_id not in seen,
                'Invalid or duplicate feed capture identity')
        capture = (folder / 'captures' / capture_id).resolve()
        require(capture.is_relative_to(folder), 'Feed capture escaped run')
        metadata_path, raw_path = capture / 'capture.json', capture / 'raw.xml'
        require(all(p.resolve().is_relative_to(folder) for p in
                    (metadata_path, raw_path, capture / 'http.json')), 'Feed evidence escaped run')
        require(sha(metadata_path.read_bytes()) == receipt['metadata_sha256'], 'Feed metadata changed')
        event = read_json(metadata_path)
        require(event['attempt'] == len(events) + 1 and event['capture_id'] == capture_id
                and sha(raw_path.read_bytes()) == event['raw_sha256']
                and raw_path.stat().st_size == event['raw_bytes'], 'Feed evidence changed')
        require(sha((capture / 'http.json').read_bytes()) == event['http_sha256'], 'Feed HTTP evidence changed')
        require((capture / 'snapshot.json').is_file() == (event['outcome']['status'] == 'valid'),
                'Feed outcome and saved snapshot disagree')
        events.append(event)
        seen.add(capture_id)
    require(seen == {p.name for p in (folder / 'captures').glob('capture-*')}, 'Unreceipted feed capture')
    return events


def record_capture(folder, producer, raw, *, requested_at, acquired_at, source, http_metadata=None):
    # Verification precedes adding any evidence, and failed captures never enter the producer.
    prior = capture_events(folder)
    capture_id = 'capture-' + uuid.uuid4().hex
    capture_folder = Path(folder) / 'captures' / capture_id
    capture_folder.mkdir(parents=True)
    raw_path = capture_folder / 'raw.xml'
    with raw_path.open('wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    http_path = capture_folder / 'http.json'
    write_json(http_path, http_metadata or {'kind': 'synthetic', 'network_request': False})
    event = {'capture_id': capture_id, 'attempt': len(prior) + 1,
             'requested_at_utc': text(requested_at), 'acquired_at_utc': text(acquired_at),
             'source': source, 'raw_sha256': sha(raw), 'raw_bytes': len(raw),
             'http_sha256': sha(http_path.read_bytes())}
    def finish(outcome):
        event['outcome'] = outcome
        write_json(capture_folder / 'capture.json', event)
        append_line(Path(folder) / 'feed_captures.jsonl', {'capture_id': capture_id,
            'metadata_sha256': sha((capture_folder / 'capture.json').read_bytes())})
    outcome = inspect_feed(raw, http_metadata or {'kind': 'synthetic'})
    if outcome['status'] != 'valid':
        finish(outcome)
        raise FeedUnavailable(outcome, capture_id)
    provenance = {'kind': 'live_nasdaq' if source == 'live' else 'synthetic_arrival',
        'feed_requested_at_utc': text(requested_at), 'feed_acquired_at_utc': text(acquired_at),
        'raw_path': raw_path.relative_to(folder).as_posix()}
    try:
        snapshot = parse_snapshot(raw, capture_id=capture_id, acquired_at=acquired_at, provenance=provenance)
    except (ValueError, XML.ParseError) as error:
        outcome = {'status': 'unavailable', 'kind': 'invalid_feed_evidence',
                   'terminal': True, 'error_type': type(error).__name__}
        finish(outcome)
        raise FeedUnavailable(outcome, capture_id) from None
    write_json(capture_folder / 'snapshot.json', snapshot.payload())
    producer.record_snapshot(snapshot)
    finish(outcome)
    return snapshot


def gate_pending(folder, producer, client, *, clock=None, after_submit=None):
    config = load_config(folder)
    gate = ShadowGate(folder, config, clock)
    state = producer.snapshot()
    # Use event time and ID for deterministic choice when one response has several arrivals.
    pending = sorted(state['pending'], key=lambda eid: (state['commands'][eid]['metadata']['halt_at_utc'], eid))
    for event_id in pending:
        command = state['commands'][event_id]
        decision = gate.decide(event_id, command, client)
        if decision['disposition'] == 'approved':
            deliver_selected(producer, event_id, after_submit=after_submit)


def first_hot_slot(config, revisions):
    from mb_market_data.sampling_acquisition import next_slot
    from mb_market_data.sampling_acquisition import AcquisitionSettings
    from mb_market_data.sampling_membership import SamplingChannel
    from mb_market_data.watchlist_polling import PollWindow
    revision = next((r for r in revisions if r.hot_symbols), None)
    if revision is None:
        return None
    settings = AcquisitionSettings(SamplingChannel.HOT, GRID,
        PollWindow(datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])),
        400, 'all', CONTRACT, .75, .05)
    slot = next_slot(settings, after=max(revision.effective_at,
        datetime.fromisoformat(config['start'])) - timedelta(microseconds=1))
    return slot.scheduled_at if slot else None


class HotWindow:
    """Published membership defines the exclusive tail; parent notes are hints."""
    def __init__(self, folder, config=None):
        self.folder = Path(folder)
        self.config = config or load_config(folder)
        self.cutoff = datetime.fromisoformat(self.config['end'])
        self.bound = False
        self.note_checked = False

    def deadline(self):
        note_exists = (self.folder / 'hot_window.json').exists()
        if not self.bound or (note_exists and not self.note_checked):
            eligible = first_hot_slot(self.config, store_for(self.folder, self.config).membership_revisions_in_effective_order())
            if eligible is not None:
                self.cutoff = planned_cutoff(self.config, eligible)
                self.bound = True
            else:
                # Enforce the no-event discovery end in the worker itself. The
                # parent's STOP note can arrive after the endpoint's wakeup.
                self.cutoff = planned_cutoff(self.config,
                    selected=ShadowGate(self.folder, self.config).selected_symbol())
            if note_exists:
                require(eligible is not None and read_json(self.folder / 'hot_window.json') == {
                    'first_eligible_at_utc': text(eligible), 'end_at_utc': text(self.cutoff)},
                    'Hot deadline differs from published membership')
                self.note_checked = True
        return self.cutoff


class TailStop:
    """Responsive outer stop plus absolute wall-clock slot/deadline checks."""
    def __init__(self, folder, external, *, clock=utc_now):
        self.external, self.clock = external, clock
        self.window = HotWindow(folder)

    def deadline(self):
        return self.window.deadline()

    def set(self):
        self.external.set()

    def is_set(self):
        return self.external.is_set() or self.clock() >= self.deadline()

    def wait_until(self, target):
        while True:
            cutoff = self.deadline()
            now = self.clock()
            mutable = self.window.config['preset'] == 'extended' and not self.window.bound
            if self.external.is_set() or now >= cutoff or (target >= cutoff and not mutable):
                return True
            if now >= target:
                return False
            self.external.wait(min(.05, (target - now).total_seconds(), (cutoff - now).total_seconds()))

    def wait(self, seconds):
        until = time.monotonic() + max(0, seconds)
        while not self.is_set() and time.monotonic() < until:
            remaining = min(until - time.monotonic(), (self.deadline() - self.clock()).total_seconds())
            self.external.wait(max(0, min(.05, remaining)))
        return self.is_set()


def synthetic_xml(config, *, event=False, update=False, second=False):
    from mb_market_data.watchlist_polling import ET
    start = datetime.fromisoformat(config['start']).astimezone(ET)
    rows = [('BASE', start - timedelta(seconds=20))]
    if event:
        rows.append(('NEWX', start + timedelta(seconds=2)))
    if second:
        rows.append(('OTHER', start + timedelta(seconds=3)))
    items = []
    for symbol, at in rows:
        items.append('<item><HaltDate>' + at.strftime('%m/%d/%Y') + '</HaltDate><HaltTime>'
            + at.strftime('%H:%M:%S') + '</HaltTime><IssueSymbol>' + symbol
            + '</IssueSymbol><ReasonCode>LUDP</ReasonCode><ResumptionTradeTime>'
            + ('15:00:00' if update and symbol == 'NEWX' else '') + '</ResumptionTradeTime></item>')
    return ('<rss><channel><title>SYNTHETIC trade halts</title>' + ''.join(items) + '</channel></rss>').encode()


class SyntheticResponse:
    status_code, ok = 200, True
    text = ''
    def __init__(self, payload):
        self.payload = payload
    def json(self):
        return self.payload


class SyntheticTransport:
    def __init__(self, clock=utc_now):
        self.clock = clock
    def quotes(self, symbols, *, fields):
        now = self.clock()
        return SyntheticResponse({s: {'symbol': s, 'assetMainType': 'EQUITY', 'assetSubType': 'COE',
            'realtime': False, 'quote': {'lastPrice': 10., 'totalVolume': 100,
                'quoteTime': int(now.timestamp() * 1000), 'tradeTime': int(now.timestamp() * 1000),
                'securityStatus': 'Synthetic offline response'}} for s in symbols})


def worker(folder, mode, credential=None):
    folder = validate_run_path(folder)
    config = load_config(folder)
    clock = config_clock(config)
    stop = Event()
    def watch():
        while not stop.wait(.025):
            if (folder / 'STOP').exists():
                stop.set()
    Thread(target=watch, daemon=True).start()
    def status(value):
        append_line(folder / (mode + '_status.jsonl'), dict(value, pid=os.getpid(), at_utc=text(clock())))
    transport = None
    try:
        if config['source'] == 'synthetic':
            deny_network()
        if mode == 'coordinator':
            _, service = load_control(folder)
            status({'status': 'ready'})
            status(service.run(stop, on_status=status))
        elif mode == 'hot':
            rejection = None
            if config['source'] == 'live':
                from mb_market_data.hot_provider_trial import prepare_schwab_client
                require(credential is not None, 'Missing worker credentials')
                transport, rejection = prepare_schwab_client(credential['ecfg'], credential['password'], SecureHorizon(config['preset']))
                credential.clear()
            else:
                transport = SyntheticTransport(clock)
            client = QuoteClient(folder, transport, rejection=rejection)
            poller = poller_for(folder, client)
            poller.initialize()
            cold = poller.full_reconstructions
            status({'status': 'ready', 'startup_full_reconstructions': cold})
            from unittest.mock import patch
            class AcquisitionClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return clock().astimezone(tz or timezone.utc)
            with patch('mb_market_data.schwab_quotes.datetime', AcquisitionClock):
                result = poller.run_loop(TailStop(folder, stop, clock=clock), on_status=status)
            status(dict(result, no_full_scan_during_polling=poller.full_reconstructions == cold))
        else:
            raise ValueError('Unknown shadow worker mode')
        return 0
    finally:
        stop.set()
        if credential:
            credential.clear()
        if transport is not None and hasattr(transport, 'close'):
            transport.close()


def validate_run_path(folder, root=ROOT):
    folder = Path(folder).resolve()
    require(folder.parent == (root / 'runs/nasdaq_shadow').resolve(), 'Path escapes shadow runs')
    load_config(folder)
    return folder


def request_stop(root=ROOT):
    pointer = root / 'runs/active_nasdaq_shadow.json'
    if not pointer.exists():
        return 'No Nasdaq shadow run started in this package.'
    value = read_json(pointer)
    require(value.get('contract') == CONTRACT, 'Shadow pointer contract changed')
    folder = validate_run_path(value['folder'], root)
    if (folder / 'result.json').exists():
        return 'Shadow run already finished: ' + read_json(folder / 'result.json')['result']
    (folder / 'STOP').write_text('Operator requested shadow stop\n', encoding='utf-8')
    return 'Stop requested for this shadow run only: ' + str(folder)


def planned_cutoff(config, eligible=None, selected=None):
    """Keep the full tail separate from discovery; allow only a bounded publication drain."""
    start, end = datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])
    discovery_end = start + timedelta(seconds=config['discovery_seconds'])
    if eligible is not None:
        if config['preset'] == 'extended':
            require(eligible <= discovery_end + timedelta(seconds=config['publication_drain_seconds']),
                    'First Hot slot exceeded the bounded publication drain')
        return min(end, eligible + timedelta(seconds=config['hot_tail_seconds']))
    return min(end, discovery_end + timedelta(seconds=config['publication_drain_seconds'] if selected else 0))


def selected_feed_evidence(receipts, event_id):
    """Keep source fields and receipt times; a reported schedule is not proof of a trade."""
    from mb_watchlist_coordinator.nasdaq_sampling import halt_event_id
    observations = []
    for receipt in receipts:
        snapshot = receipt['snapshot']
        for record in snapshot['records']:
            if halt_event_id(record) == event_id:
                observations.append({'capture_id': snapshot['capture_id'],
                    'feed_acquired_at_utc': snapshot['retrieved_at_utc'],
                    'adapter_received_at_utc': receipt['recorded_at_utc'],
                    **{key: record.get(key, '') for key in ('symbol', 'halt_date', 'halt_time',
                        'reason_code', 'resumption_date', 'resumption_quote_time', 'resumption_trade_time')}})
    trade = next((row for row in observations if row['resumption_trade_time']), None)
    return {'event_id': event_id, 'observations': observations,
        'trade_resumption_status': 'REPORTED_BY_FEED' if trade else 'NOT_REPORTED_IN_CAPTURED_FEED',
        'first_trade_resumption_report': trade,
        'interpretation': 'Source fields in ET; quote and trade times remain separate. A reported schedule is not an observed executed trade. Blank fields or disappearance do not prove continued halt or resumption.'}


def run(folder, roster, catalog, *, source, preset='short', transport=None, rejection=None, credential=None,
        catalog_bodies=None, catalog_receipts=None, on_status=print, no_event=False,
        synthetic_feed=None):
    require(synthetic_feed is None or source == 'synthetic', 'Feed injection is synthetic-only')
    folder = Path(folder).resolve()
    offset = 0.
    start = None
    if source == 'synthetic':
        start = (utc_now() + timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)
        offset = (start - timedelta(seconds=20) - utc_now()).total_seconds()
    clock = lambda: utc_now() + timedelta(seconds=offset)
    config = prepare(folder, roster, catalog, source=source, preset=preset, catalog_bodies=catalog_bodies,
                     catalog_receipts=catalog_receipts, clock=clock, start=start, clock_offset=offset)
    write_json(ROOT / 'runs/active_nasdaq_shadow.json', {'contract': CONTRACT, 'folder': str(folder)})
    ledger, _ = load_control(folder)
    producer = producer_for(folder, ledger)
    client = QuoteClient(folder, transport or SyntheticTransport(clock), rejection=rejection)
    children, handles = {}, []
    start, end = datetime.fromisoformat(config['start']), datetime.fromisoformat(config['end'])
    deadline, planned_stop = time.monotonic() + config['observation_seconds'] + 50, planned_cutoff(config)
    discovery_end = start + timedelta(seconds=config['discovery_seconds'])
    attempts, failures = 0, []
    external_stop = False
    next_fetch, next_progress = start, time.monotonic()
    try:
        for mode in ('coordinator', 'hot'):
            handle = (folder / (mode + '_console.txt')).open('w', encoding='utf-8')
            handles.append(handle)
            children[mode] = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'worker.py'), str(folder), mode],
                stdout=handle, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if mode == 'hot' and source == 'live' else subprocess.DEVNULL)
            if mode == 'hot' and source == 'live':
                children[mode].stdin.write(json.dumps(credential).encode())
                children[mode].stdin.close()
        credential = None
        while clock() < planned_stop:
            if (folder / 'STOP').exists():
                external_stop = True
                break
            require(time.monotonic() < deadline, 'Shadow bounded runtime exceeded')
            for mode, child in children.items():
                if child.poll() is not None:
                    rows = lines(folder / (mode + '_status.jsonl'))
                    normal = mode == 'hot' and child.returncode == 0 and rows and rows[-1]['status'] in ('window_ended', 'stopped')
                    require(normal and clock() >= planned_stop - timedelta(seconds=2),
                            mode + ' worker exited unexpectedly; inspect its retained console')
            now = clock()
            if now >= start - timedelta(seconds=1):
                require(all(any(row['status'] == 'ready' for row in lines(folder / (mode + '_status.jsonl')))
                    for mode in children), 'Workers were not ready; no late catch-up')
            if (now >= next_fetch and (planned_stop - now).total_seconds() > 3
                    and (now < discovery_end or first_hot_slot(config,
                         store_for(folder, config).membership_revisions_in_effective_order()) is not None)):
                require(attempts < config['max_feed_attempts'], 'Feed-attempt budget exhausted')
                attempts += 1
                requested_at = clock()
                try:
                    if source == 'live':
                        response = fetch_feed()
                    else:
                        raw = synthetic_xml(config, event=not no_event and attempts >= 2,
                                            update=attempts >= 3, second=not no_event and attempts >= 3)
                        response = (synthetic_feed(attempts, config, raw) if synthetic_feed else
                                    FeedResponse(raw, {'kind': 'synthetic', 'network_request': False}))
                    acquired_at = clock()
                    record_capture(folder, producer, response.raw, requested_at=requested_at,
                                   acquired_at=acquired_at, source=source, http_metadata=response.metadata)
                except FeedUnavailable as exc:
                    health = feed_health(capture_events(folder))
                    append_line(folder / 'feed_failures.jsonl', {'attempt': attempts, 'at_utc': text(clock()),
                        'capture_id': exc.capture_id, 'kind': exc.outcome['kind'],
                        'consecutive': health['consecutive_failures'], 'terminal': exc.outcome['terminal']})
                    on_status('Nasdaq feed unavailable: ' + exc.outcome['kind']
                              + '; consecutive=' + str(health['consecutive_failures']) + '/3')
                    if health['stop_reason']:
                        failures.append('Feed observation stopped: ' + health['stop_reason'])
                        break
                # Next request is measured from completion: never catch up after a slow response.
                next_fetch = clock() + timedelta(seconds=config['feed_interval_seconds'])
                gate_pending(folder, producer, client)
            revisions = store_for(folder, config).membership_revisions_in_effective_order()
            eligible = first_hot_slot(config, revisions)
            planned_stop = planned_cutoff(config, eligible, ShadowGate(folder, config).selected_symbol())
            if eligible is not None:
                if not (folder / 'hot_window.json').exists():
                    write_json(folder / 'hot_window.json', {'first_eligible_at_utc': text(eligible), 'end_at_utc': text(planned_stop)})
            if time.monotonic() >= next_progress:
                selected = ShadowGate(folder, config).selected_symbol()
                on_status('Nasdaq shadow: ' + ('waiting for startup' if now < start else 'observing')
                    + '; attempts=' + str(attempts)
                    + '; valid captures=' + str(feed_health(capture_events(folder))['successful_captures'])
                    + '; selected=' + str(selected)
                    + '; quote permits=' + str(budget_counts(folder)))
                next_progress = time.monotonic() + 10
            time.sleep(.025)
        if external_stop:
            failures.append('Operator/provider stopped the bounded run; transition qualification incomplete')
    finally:
        # Only these exact child handles are owned. No production PID discovery or signals.
        (folder / 'STOP').write_text('Shadow collection finished\n', encoding='utf-8')
        planned_stop = min(planned_stop, clock())
        for mode, child in children.items():
            try:
                child.wait(timeout=6)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                failures.append(mode + ' needed forced termination')
        for handle in handles:
            handle.close()
        if credential:
            credential.clear()
    for mode, child in children.items():
        if child.returncode != 0:
            failures.append(mode + ' worker failed')
    return analyse(folder, planned_stop, attempts, failures, children=children, external_stop=external_stop)


def analyse(folder, cutoff, attempts, failures, *, children=None, external_stop=False):
    from mb_market_data.hot_provider_trial import replay_digest
    from mb_market_data.quote_journal_audit import audit_quote_journal
    from mb_market_data.sampling_assessment import assess
    from mb_watchlist_coordinator.sampling_requests import plain
    folder = Path(folder)
    config = load_config(folder)
    failures = list(failures)
    store = store_for(folder, config)
    ledger, service = load_control(folder)
    producer = producer_for(folder, ledger)
    producer_state = plain(producer.snapshot())
    feed_events = capture_events(folder)
    feed = feed_health(feed_events)
    require(len(feed_events) == attempts, 'Feed attempt count differs from retained evidence')
    require(feed['successful_captures'] == len(producer_state['receipts']), 'Feed/producer receipts differ')
    if feed['stop_reason'] and not any(feed['stop_reason'] in f for f in failures):
        failures.append('Feed stop condition: ' + feed['stop_reason'])
    gate = ShadowGate(folder, config)
    dispositions = gate.dispositions()
    selected = gate.selected_symbol()
    revisions = store.membership_revisions_in_effective_order()
    eligible = first_hot_slot(config, revisions)
    if selected and eligible is None:
        failures.append('Approved identity never reached an eligible published Hot slot')
    # Structural audit uses the last completed scheduled slot, not an invented future slot.
    audit = audit_quote_journal(store.database_path, audited_at=cutoff - timedelta(microseconds=1))
    first, second = replay_digest(store.database_path), replay_digest(store.database_path)
    if not audit.passed:
        failures.append('Journal audit failed: ' + str([f.code for f in audit.findings]))
    if first != second:
        failures.append('Repeated journal replay differs')
    for receipt in producer_state['receipts']:
        snap = receipt['snapshot']
        raw_path = (folder / snap['provenance']['raw_path']).resolve()
        require(raw_path.is_relative_to(folder.resolve()) and sha(raw_path.read_bytes()) == snap['raw_sha256'],
                'Saved feed capture changed')
    for decision in dispositions.values():
        if 'response_file' in decision:
            path = folder / decision['response_file']
            require(path.name == decision['response_file'] and sha(path.read_bytes()) == decision['response_sha256'],
                    'Saved broker identity response changed')
    unresolved = [eid for eid in producer_state['pending'] if eid not in dispositions
                  or dispositions[eid]['disposition'] == 'approved']
    if unresolved:
        failures.append('Unresolved producer delivery/ACK')
    if any(d['disposition'] == 'resolution_outcome_unknown_no_retry' for d in dispositions.values()):
        failures.append('Unresolved broker lookup retained without retry')
    if service.snapshot()['pending'] is not None:
        failures.append('Unresolved Coordinator publication')
    acquisitions = store.acquisitions_in_replay_order()
    hot = [a for a in acquisitions if a.channel == 'hot']
    outcomes = lines(folder / 'hot_status.jsonl')
    if not outcomes or outcomes[-1].get('no_full_scan_during_polling') is not True:
        failures.append('Hot worker did not finish cleanly with its incremental checkpoint')
    poller = poller_for(folder, None)
    if poller.snapshot()['pending'] is not None:
        failures.append('Unresolved Hot acquisition')
    observed_slots = {datetime.fromisoformat(row['slot_id'].split(':', 1)[1])
        for row in outcomes if row['status'] in ('acquired', 'empty_membership')}
    planned_slots = []
    cursor = datetime.fromisoformat(config['start'])
    while cursor < cutoff:
        planned_slots.append(cursor)
        cursor += timedelta(seconds=2)
    missing = sorted(set(planned_slots) - observed_slots)
    if missing:
        failures.append('Missing scheduled shadow Hot outcomes: ' + str(len(missing)))
    committed_rows = [r for r in outcomes if r['status'] in ('acquired', 'empty_membership')]
    counts_by_slot = Counter(datetime.fromisoformat(r['slot_id'].split(':', 1)[1]) for r in committed_rows)
    duplicate_slots = [text(at) for at, count in counts_by_slot.items() if count > 1]
    extra_slots = sorted(observed_slots - set(planned_slots))
    if duplicate_slots or extra_slots:
        failures.append('Duplicate or out-of-window shadow outcomes')
    expected_hot = sum(at >= eligible for at in planned_slots) if eligible is not None else 0
    if len(hot) != expected_hot:
        failures.append('Hot acquisition count differs from bounded plan')
    commits = {row['slot_id']: row['journal_commit_observed_at_utc'] for row in lines(folder / 'hot_commit.jsonl')}
    if any(a.slot_id not in commits for a in hot):
        failures.append('Hot journal commit observation receipt missing')
    calls = lines(folder / 'hot_calls.jsonl') + lines(folder / 'resolution_calls.jsonl')
    permits = budget_counts(folder)
    if len(calls) != sum(permits.values()):
        failures.append('Reserved quote-call outcome missing')
    if any(c.get('status_code', 0) != 200 or not c.get('dispatched') or 'exception_type' in c for c in calls):
        failures.append('Quote transport or dispatch failure')
    if not producer_state['receipts']:
        failures.append('No successful live feed baseline captured')
    if any(len(r.hot_symbols) > 1 for r in revisions) or any(a.requested_symbol_count != 1 for a in hot):
        failures.append('One-symbol shadow capacity violated')
    if selected and any(set(r.hot_symbols) - {selected} for r in revisions):
        failures.append('Published Hot identity differs from shadow gate')
    request_replay = plain(ledger.states()[-1].payload()) == plain(load_control(folder)[0].states()[-1].payload())
    publication_replay = plain(service.snapshot()) == plain(load_control(folder)[1].snapshot())
    producer_replay = producer_state == plain(producer_for(folder, load_control(folder)[0]).snapshot())
    if not all((request_replay, publication_replay, producer_replay)):
        failures.append('Request/publication/producer restart replay differs')
    if children and (len({c.pid for c in children.values()}) != 2 or os.getpid() in {c.pid for c in children.values()}):
        failures.append('Producer, Coordinator and Hot are not independent processes')
    counts = dict(next((a.status_counts for a in audit.channels if a.channel == 'hot'), {}))
    assessment = assess(collection_failures=failures, status_counts={'hot': counts}) if hot else {
        'result': 'FAILED' if failures else 'PASS', 'collection_integrity': {'result': 'FAILED' if failures else 'PASS', 'findings': failures},
        'transport': {'result': 'PASS' if not failures else 'UNQUALIFIED'},
        'availability': {'result': 'NOT_OBSERVED'}, 'status_counts': {}}
    timeline = selected_feed = None
    transition = bool(selected and eligible and hot and any(e.accepted and e.kind == 'request' for e in ledger.events()))
    if transition:
        request = next(e for e in ledger.events() if e.accepted and e.kind == 'request')
        selected_feed = selected_feed_evidence(producer_state['receipts'], request.command_id)
        metadata = plain(request.payload['metadata'])
        revision = next(r for r in revisions if selected in r.hot_symbols)
        first_acquisition = hot[0]
        timeline = {'halt_at_utc': metadata['halt_at_utc'], 'feed_acquired_at_utc': metadata['capture_retrieved_at_utc'],
            'first_discovered_at_utc': metadata['adapter_first_seen_at_utc'],
            'request_received_at_utc': text(request.recorded_at), 'published_at_utc': text(revision.published_at),
            'membership_effective_at_utc': text(revision.effective_at), 'first_eligible_hot_slot_utc': text(eligible),
            'first_observed_hot_slot_utc': text(first_acquisition.scheduled_at_utc),
            'provider_response_assembled_at_utc': text(first_acquisition.completed_at_utc),
            'journal_commit_observed_at_utc': commits.get(first_acquisition.slot_id)}
        selected_calls = [c for c in lines(folder / 'hot_calls.jsonl') if c['key'] == first_acquisition.slot_id]
        timeline['provider_response_returned_at_utc'] = selected_calls[0]['returned_at_utc'] if selected_calls else None
        resolution_calls = [c for c in lines(folder / 'resolution_calls.jsonl') if c['key'] == request.command_id]
        timeline['broker_resolution_started_at_utc'] = resolution_calls[0]['started_at_utc'] if resolution_calls else None
        timeline['broker_resolution_returned_at_utc'] = resolution_calls[0]['returned_at_utc'] if resolution_calls else None
        timeline['publication_at_event_clock_utc'] = text(revision.published_at + timedelta(seconds=config['clock_offset_seconds']))
        timeline['first_seen_to_commit_seconds'] = (datetime.fromisoformat(timeline['journal_commit_observed_at_utc'])
            - datetime.fromisoformat(timeline['first_discovered_at_utc'])).total_seconds() if timeline['journal_commit_observed_at_utc'] else None
    result = assessment['result']
    warnings = []
    if feed['failed_captures']:
        warnings.append('Feed interruption retained; discovery during the gap is unknown')
        if result == 'PASS':
            result = 'PASS_WITH_WARNINGS'
    if external_stop:
        result = 'STOPPED'
    elif result != 'FAILED' and not transition:
        result = 'READINESS_ONLY'
    accepted_transition = transition and result in ('PASS', 'PASS_WITH_WARNINGS')
    report = {'contract': CONTRACT, 'result': result, 'source': config['source'], 'preset': config['preset'],
        'milestone': (('LIVE_TRANSITION' if config['source'] == 'live' else 'SYNTHETIC_TRANSITION')
                      if accepted_transition else 'UNQUALIFIED' if result == 'FAILED' else 'READINESS_ONLY'),
        'timing_clock': 'actual UTC' if config['source'] == 'live' else 'labeled future UTC offset at actual elapsed speed; journal publication retains actual UTC',
        'assessment': assessment, 'failures': failures, 'selected_symbol': selected,
        'feed_attempts': attempts, 'successful_captures': len(producer_state['receipts']),
        'feed_health': feed, 'warnings': warnings,
        'gate_dispositions': dispositions, 'producer_pending_with_terminal_gate_disposition':
            {eid: dispositions[eid]['disposition'] for eid in producer_state['pending'] if eid in dispositions},
        'expected_slots': len(planned_slots), 'committed_slots': len(set(planned_slots) & observed_slots),
        'expected_hot_acquisitions': expected_hot, 'duplicate_outcome_slot_ids': duplicate_slots,
        'out_of_window_outcome_slot_ids': [text(at) for at in extra_slots],
        'missing_slots': [text(at) for at in missing], 'hot_acquisitions': len(hot),
        'empty_hot_skips': sum(r['status'] == 'empty_membership' for r in outcomes),
        'quote_call_permits': permits, 'quote_calls': len(calls), 'max_quote_calls': config['max_total_quote_calls'],
        'selected_feed_evidence': selected_feed,
        'hot_window_seconds': max(0., (cutoff - eligible).total_seconds()) if eligible else 0,
        'configured_hot_tail_seconds': config['hot_tail_seconds'],
        'timeline': timeline, 'audit_passed': audit.passed, 'replays_identical': first == second,
        'journal_replay': first, 'request_replay_identical': request_replay,
        'publication_replay_identical': publication_replay, 'producer_replay_identical': producer_replay,
        'pids': {k: v.pid for k, v in (children or {}).items()}, 'producer_pid': os.getpid(),
        'final_hierarchy': {'uni': len(revisions[-1].uni_symbols), 'focus': len(revisions[-1].focus_symbols), 'hot': len(revisions[-1].hot_symbols)},
        'cutoff_utc': text(cutoff), 'coordinator_commit': PIN_COMMIT,
        'limitations': ['Review settings only; production Hot disabled and production journals/processes untouched',
            'Only shadow Hot acquires quotes; this increment is not another combined-load capacity trial',
            'Broker quote identity confirmation is not tradability or an explanation of invalid responses',
            'No new qualifying event means readiness only, never a live-transition claim',
            'Commit timestamps are observer receipts immediately after successful journal writes',
            'No automatic live restart, missed-slot catch-up or full-day provider-capacity claim']}
    write_json(folder / 'report.json', report)
    summary = ['NASDAQ SHADOW: ' + result + ' (' + report['milestone'] + ')',
        'Source: ' + config['source'] + '; selected symbol: ' + str(selected),
        'Hot acquisitions=' + str(len(hot)) + '/' + str(expected_hot) + '; empty skips=' + str(report['empty_hot_skips'])
            + '; missing=' + str(len(missing)),
        'Quote calls/maximum: ' + str(len(calls)) + '/' + str(config['max_total_quote_calls']) + '; permits=' + str(permits),
        'Audit=' + str(audit.passed) + '; identical replays=' + str(first == second),
        'Feed=' + feed['status'] + '; valid=' + str(feed['successful_captures'])
            + '; failed=' + str(feed['failed_captures'])
            + '; discovery continuity=' + feed['halt_discovery_continuity'],
        'Replay SHA-256: ' + first['sequence_sha256'],
        'Preset=' + config['preset'] + '; captured trade-resumption status=' + (selected_feed['trade_resumption_status'] if selected_feed else 'NO_SELECTED_EVENT'),
        'Timeline: ' + json.dumps(timeline, sort_keys=True), *failures, *warnings, *report['limitations']]
    (folder / 'summary.txt').write_text('\n'.join(summary) + '\n', encoding='utf-8')
    return report
