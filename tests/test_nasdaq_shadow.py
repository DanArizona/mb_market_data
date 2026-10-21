"""New shadow boundaries; no credentials, sockets, or accepted heavy trial reruns."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / 'experiments/nasdaq_shadow_20261019'
sys.path.insert(0, str(HERE))
import shadow
shadow.bootstrap()

from mb_market_data.sampling_acquisition import ExplicitPollSlot
from mb_market_data.sampling_membership import SamplingChannel
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.hot_provider_trial import replay_digest
from mb_watchlist_coordinator.sampling_requests import plain


NASDAQ = b'''Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
NEWX|Synthetic Common Stock|G|N|H|100|N|N
QQQ|Synthetic ETF|Q|N|N|100|Y|N
TEST|Test|S|Y|N|100|N|N
File Creation Time: 1019202610:00|||||||
'''
OTHER = b'''ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BASE|Synthetic Common Stock|N|BASE|N|100|N|BASE
BRK.B|Punctuation|N|BRK.B|N|100|N|BRK.B
ALIAS|Ambiguous alias|N|ALIA.S|N|100|N|ALIAS
File Creation Time: 1019202610:00|||||||
'''


class RecordingTransport(shadow.SyntheticTransport):
    def __init__(self, clock, payload=None, error=False):
        super().__init__(clock)
        self.calls = []
        self.payload, self.error = payload, error
    def quotes(self, symbols, *, fields):
        self.calls.append(tuple(symbols))
        if self.error:
            raise TimeoutError('Private SDK text must never be persisted')
        return shadow.SyntheticResponse(self.payload) if self.payload is not None else super().quotes(symbols, fields=fields)


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nasdaq shadow tests ')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'run'
        self.base = (shadow.utc_now() + timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)
        self.now = self.base - timedelta(seconds=10)
        self.clock = lambda: self.now
        self.catalog = {s: {'symbol': s, 'synthetic': True} for s in ('BASE', 'NEWX', 'OTHER')}
        self.roster = {'opening_uni': ['BASE'], 'opening_focus': ['BASE'], 'input_sha256': {}}
        self.config = shadow.prepare(self.folder, self.roster, self.catalog, source='synthetic', clock=self.clock, start=self.base)
        self.ledger, self.service = shadow.load_control(self.folder, clock=self.clock)
        self.producer = shadow.producer_for(self.folder, self.ledger, clock=self.clock)
        self.transport = RecordingTransport(self.clock)
        self.client = shadow.QuoteClient(self.folder, self.transport, clock=self.clock)
        self.gate = shadow.ShadowGate(self.folder, self.config, self.clock)

    def capture(self, seconds, **kwargs):
        self.now = self.base + timedelta(seconds=seconds)
        return shadow.record_capture(self.folder, self.producer, shadow.synthetic_xml(self.config, **kwargs),
            requested_at=self.now, acquired_at=self.now, source='synthetic')

    def arrivals(self):
        self.capture(0)
        self.capture(3, event=True)

    def pending(self):
        return plain(self.producer.snapshot())['pending']

    def requests(self):
        return [e for e in self.ledger.events() if e.kind == 'request']

    def gate_all(self, **kwargs):
        return shadow.gate_pending(self.folder, self.producer, self.client, clock=self.clock, **kwargs)

    def test_settings_have_hard_review_budgets_and_live_unshifted_clock(self):
        value = shadow.policy('live')
        self.assertEqual((value['max_resolution_calls'], value['max_hot_calls'], value['max_total_quote_calls']), (3, 30, 33))
        self.assertEqual((value['observation_seconds'], value['hot_tail_seconds'], value['max_admitted_symbols']), (900, 60, 1))
        self.assertEqual((value['feed_interval_seconds'], value['max_feed_attempts']), (60, 15))
        self.assertEqual(shadow.policy('synthetic')['feed_interval_seconds'], 2)
        self.assertEqual(value['hot_seconds'], list(range(0, 60, 2)))
        self.assertFalse(value['production_hot'])

    def use_extended(self):
        self.folder = Path(self.temp.name) / 'extended'
        self.now = self.base - timedelta(seconds=10)
        self.config = shadow.prepare(self.folder, self.roster, self.catalog, source='synthetic',
            preset='extended', clock=self.clock, start=self.base)
        self.ledger, self.service = shadow.load_control(self.folder, clock=self.clock)
        self.producer = shadow.producer_for(self.folder, self.ledger, clock=self.clock)
        self.client = shadow.QuoteClient(self.folder, self.transport, clock=self.clock)

    def test_extended_late_admission_keeps_full_tail_and_bounds_publication_wait(self):
        self.use_extended()
        self.capture(0)
        self.capture(899, event=True)
        self.gate_all()
        discovery_end = self.base + timedelta(seconds=900)
        self.assertEqual(shadow.planned_cutoff(self.config), discovery_end)
        self.assertEqual(shadow.planned_cutoff(self.config, selected='NEWX'), discovery_end + timedelta(seconds=30))
        self.now = discovery_end
        self.service.cycle()
        eligible = shadow.first_hot_slot(self.config, shadow.store_for(self.folder, self.config).membership_revisions_in_effective_order())
        self.assertGreater(eligible, discovery_end)
        self.assertEqual(shadow.planned_cutoff(self.config, eligible), eligible + timedelta(seconds=900))
        self.assertEqual(shadow.HotWindow(self.folder).deadline(), eligible + timedelta(seconds=900))
        with self.assertRaises(ValueError):
            shadow.planned_cutoff(self.config, discovery_end + timedelta(seconds=32))

    def test_extended_discovery_closure_does_not_dispatch_or_admit_late_identity(self):
        self.use_extended()
        self.capture(0)
        self.capture(900, event=True)
        self.gate_all()
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(next(iter(shadow.ShadowGate(self.folder, self.config).dispositions().values()))['disposition'], 'discovery_window_ended')
        stop = shadow.TailStop(self.folder, Event(), clock=self.clock)
        self.assertEqual(stop.deadline(), self.base + timedelta(seconds=900))
        self.assertTrue(stop.is_set())
        poller = shadow.poller_for(self.folder, self.client, clock=self.clock)
        outcome = poller.sample(ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID))
        self.assertEqual(outcome['status'], 'shadow_window_ended')
        self.assertEqual(shadow.lines(self.folder / 'hot_calls.jsonl'), [])
        self.assertEqual(poller.snapshot()['finished'], {})

    def test_extended_resolution_returning_after_discovery_closure_is_not_admitted(self):
        self.use_extended()
        self.capture(0)
        self.capture(899, event=True)
        original = self.transport.quotes
        def slow(symbols, *, fields):
            self.now = self.base + timedelta(seconds=900)
            return original(symbols, fields=fields)
        self.transport.quotes = slow
        self.gate_all()
        self.assertEqual(self.requests(), [])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIsNone(shadow.ShadowGate(self.folder, self.config).selected_symbol())

    def test_extended_boundary_wait_can_observe_selection_before_discovery_closes(self):
        self.use_extended()
        self.capture(0)
        self.capture(899, event=True)
        test = self
        class BoundaryWake:
            def is_set(self):
                return False
            def wait(self, seconds):
                test.now += timedelta(seconds=.5)
                if not test.requests():
                    test.gate_all()
                    test.service.cycle()
        stop = shadow.TailStop(self.folder, BoundaryWake(), clock=self.clock)
        poller = shadow.poller_for(self.folder, self.client, clock=self.clock)
        poller.initialize()
        poller._shadow_stop = stop
        slot = ExplicitPollSlot(SamplingChannel.HOT, self.base + timedelta(seconds=900), shadow.GRID)
        test_clock = self.clock
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return test_clock().astimezone(tz or timezone.utc)
        with patch('mb_market_data.schwab_quotes.datetime', LogicalDatetime):
            outcome = poller.sample(slot)
        self.assertEqual(outcome['status'], 'acquired')
        self.assertEqual(shadow.HotWindow(self.folder).deadline(), self.base + timedelta(seconds=1800))

    def test_extended_full_logical_tail_replays_and_endpoint_dispatch_is_refused(self):
        # Logical clock coverage/budgets, not 15 minutes of elapsed/provider capacity.
        self.use_extended()
        self.capture(0)
        poller = shadow.poller_for(self.folder, self.client, clock=self.clock)
        poller.initialize()
        test_clock = self.clock
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return test_clock().astimezone(tz or timezone.utc)
        with patch('mb_market_data.schwab_quotes.datetime', LogicalDatetime):
            for seconds in (0, 2):
                self.now = self.base + timedelta(seconds=seconds)
                shadow.append_line(self.folder / 'hot_status.jsonl', poller.sample(ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID)))
            self.capture(3, event=True)
            self.gate_all()
            self.service.cycle()
            eligible = shadow.first_hot_slot(self.config, shadow.store_for(self.folder, self.config).membership_revisions_in_effective_order())
            for number in range(450):
                self.now = eligible + timedelta(seconds=number * 2)
                if number == 150:
                    self.capture((self.now - self.base).total_seconds(), event=True, update=True)
                    self.gate_all()
                    self.service.cycle()
                outcome = poller.sample(ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID))
                self.assertEqual(outcome['status'], 'acquired')
                shadow.append_line(self.folder / 'hot_status.jsonl', outcome)
            self.now = eligible + timedelta(seconds=900)
            before = len(self.transport.calls)
            endpoint = poller.sample(ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID))
            self.assertEqual(endpoint['status'], 'shadow_window_ended')
            self.assertEqual(len(self.transport.calls), before)
        shadow.append_line(self.folder / 'hot_status.jsonl', {'status': 'stopped', 'no_full_scan_during_polling': True})
        report = shadow.analyse(self.folder, self.now, 3, [])
        self.assertEqual((report['result'], report['milestone']), ('PASS', 'SYNTHETIC_TRANSITION'))
        self.assertEqual((report['hot_acquisitions'], report['expected_hot_acquisitions'], report['hot_window_seconds']), (450, 450, 900))
        self.assertEqual((report['quote_calls'], report['max_quote_calls']), (451, 453))
        self.assertEqual(report['missing_slots'], [])
        self.assertTrue(report['audit_passed'] and report['replays_identical'])
        self.assertEqual(report['selected_feed_evidence']['trade_resumption_status'], 'REPORTED_BY_FEED')
        self.assertEqual(len(self.requests()), 1)

    def test_extended_quote_cap_blocks_call_451_even_without_tail_membership(self):
        self.use_extended()
        shadow.ShadowGate(self.folder, self.config, self.clock).append('decision', 'controlled-budget-selection', symbol='NEWX', disposition='approved')
        for number in range(450):
            self.now = self.base + timedelta(seconds=number * 2)
            self.client.slot = ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID)
            self.client.quotes(['NEWX'], fields='all')
        self.now += timedelta(seconds=2)
        self.client.slot = ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID)
        with self.assertRaises(ValueError):
            self.client.quotes(['NEWX'], fields='all')
        self.assertEqual(len(self.transport.calls), 450)

    def test_selected_feed_report_matches_halt_identity_and_keeps_quote_trade_separate(self):
        self.arrivals()
        receipts = plain(self.producer.snapshot())['receipts']
        eid = self.pending()[0]
        value = shadow.selected_feed_evidence(receipts, eid)
        self.assertEqual(value['trade_resumption_status'], 'NOT_REPORTED_IN_CAPTURED_FEED')
        self.capture(5, event=True, update=True)
        value = shadow.selected_feed_evidence(plain(self.producer.snapshot())['receipts'], eid)
        self.assertEqual(value['trade_resumption_status'], 'REPORTED_BY_FEED')
        self.assertEqual(len(value['observations']), 2)
        self.assertEqual(value['first_trade_resumption_report']['adapter_received_at_utc'], shadow.text(self.now))
        self.assertEqual(shadow.selected_feed_evidence(receipts, 'different-event')['observations'], [])

    def test_extended_launch_cutoff_and_credential_horizon_cover_full_run(self):
        from zoneinfo import ZoneInfo
        et = ZoneInfo('America/New_York')
        at = datetime(2026, 10, 21, 15, 24, 59, tzinfo=et)
        shadow.check_live_window(at, 'extended')
        with self.assertRaises(ValueError):
            shadow.check_live_window(at + timedelta(seconds=1), 'extended')
        self.assertGreaterEqual(shadow.SecureHorizon('extended').required_through(at) - at, timedelta(seconds=1830 + 50))
        with self.assertRaises(ValueError):
            shadow.policy('live', 'unbounded')

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            shadow.policy('production')

    def test_directory_identity_excludes_test_etf_alias_and_punctuation(self):
        self.assertEqual(set(shadow.catalog_from_bytes(NASDAQ, OTHER)), {'NEWX', 'BASE'})

    def test_duplicate_directory_identity_rejected(self):
        with self.assertRaises(ValueError):
            shadow.catalog_from_bytes(NASDAQ, OTHER.replace(b'BASE|', b'NEWX|'))

    def test_malformed_or_unrelated_xml_never_establishes_baseline(self):
        for raw in (b'<html/>', b'<rss/>', b'not XML'):
            with self.assertRaises(Exception):
                shadow.parse_snapshot(raw, capture_id='bad', acquired_at=self.now, provenance={})
        self.assertEqual(len(self.producer.snapshot()['receipts']), 0)

    def test_startup_active_halt_is_baseline_without_quote_calls(self):
        self.capture(3, event=True)
        self.gate_all()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.transport.calls, [])

    def test_late_discovery_of_prestartup_event_stays_suppressed(self):
        self.capture(3)
        self.capture(5, event=True)
        self.gate_all()
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.transport.calls, [])
        self.assertIn('predates_startup', str(plain(self.producer.snapshot())['outcomes']))

    def test_new_outside_uni_event_gets_exact_resolution_and_owned_request(self):
        self.arrivals()
        self.gate_all()
        request = self.requests()[0]
        self.assertTrue(request.accepted)
        self.assertEqual(request.actor_id, shadow.SOURCE_ID)
        self.assertEqual(list(request.payload['symbols']), ['NEWX'])
        self.assertEqual(request.payload['metadata']['request_lifetime'], 'regular_close')
        self.assertEqual(self.transport.calls, [('NEWX',)])
        self.assertEqual(self.pending(), [])

    def test_repeat_and_resumption_update_do_not_duplicate_or_withdraw(self):
        self.arrivals()
        self.gate_all()
        self.capture(5, event=True, update=True)
        self.gate_all()
        self.capture(7, event=True, update=True)
        self.gate_all()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.transport.calls, [('NEWX',)])
        self.assertEqual(self.gate.selected_symbol(), 'NEWX')
        self.assertIn('updated', str(plain(self.producer.snapshot())['outcomes']))

    def test_later_distinct_halt_same_symbol_keeps_distinct_request_and_reuses_identity(self):
        self.arrivals()
        self.gate_all()
        self.now = self.base + timedelta(seconds=5)
        raw = shadow.synthetic_xml(self.config, event=True)
        from mb_market_data.watchlist_polling import ET
        old = (self.base + timedelta(seconds=2)).astimezone(ET).strftime('%H:%M:%S').encode()
        new = (self.base + timedelta(seconds=4)).astimezone(ET).strftime('%H:%M:%S').encode()
        shadow.record_capture(self.folder, self.producer, raw.replace(old, new),
            requested_at=self.now, acquired_at=self.now, source='synthetic')
        self.gate_all()
        self.assertEqual(len(self.requests()), 2)
        self.assertEqual(len({e.command_id for e in self.requests()}), 2)
        self.assertEqual(self.transport.calls, [('NEWX',)])

    def test_capacity_rejection_is_visible_without_second_symbol_lookup_or_request(self):
        self.arrivals()
        self.gate_all()
        self.capture(5, event=True, second=True)
        self.gate_all()
        dispositions = self.gate.dispositions()
        self.assertIn('shadow_capacity_rejected', [d['disposition'] for d in dispositions.values()])
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.transport.calls, [('NEWX',)])

    def test_catalog_unresolved_event_does_not_reach_broker_or_coordinator(self):
        self.arrivals()
        state = self.producer.snapshot()
        eid = state['pending'][0]
        command = dict(state['commands'][eid], symbol='UNKNOWN')
        result = self.gate.decide(eid, command, self.client)
        self.assertEqual(result['disposition'], 'catalog_unresolved')
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.requests(), [])

    def test_invalid_broker_response_is_unconfirmed_without_cause_inference(self):
        self.arrivals()
        self.transport.payload = {'errors': {'invalidSymbols': ['NEWX']}}
        self.gate_all()
        self.gate_all()
        self.assertEqual(len(self.transport.calls), 1)
        decision = next(iter(self.gate.dispositions().values()))
        self.assertEqual(decision['disposition'], 'broker_identity_unconfirmed')
        self.assertEqual(self.requests(), [])
        self.assertNotIn('tradable', json.dumps(decision))

    def test_broker_alias_or_wrong_asset_cannot_approve(self):
        self.arrivals()
        self.transport.payload = {'NEWX': {'symbol': 'DIFFERENT', 'assetMainType': 'EQUITY', 'assetSubType': 'COE'}}
        self.gate_all()
        self.assertIsNone(self.gate.selected_symbol())

    def test_timeout_retains_unknown_lookup_and_never_reissues_it(self):
        self.arrivals()
        self.transport.error = True
        with self.assertRaises(RuntimeError):
            self.gate_all()
        self.transport.error = False
        self.gate_all()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(next(iter(self.gate.dispositions().values()))['disposition'], 'resolution_outcome_unknown_no_retry')
        self.assertNotIn('Private SDK text', (self.folder / 'resolution_calls.jsonl').read_text())

    def test_request_commit_lost_ack_recovers_original_receipt_without_lookup(self):
        self.arrivals()
        def crash(_):
            raise RuntimeError('Injected post-request commit exit')
        with self.assertRaises(RuntimeError):
            self.gate_all(after_submit=crash)
        original = self.requests()[0]
        self.assertEqual(len(self.pending()), 1)
        self.now += timedelta(seconds=1)
        self.gate_all()
        self.assertEqual(self.requests()[0], original)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.pending(), [])

    def test_gate_history_tampering_fails_closed(self):
        self.arrivals()
        self.gate_all()
        with closing(sqlite3.connect(self.gate.path)) as connection, connection:
            connection.execute("UPDATE gate_event SET payload='{}' WHERE sequence=1")
        with self.assertRaises((ValueError, KeyError)):
            self.gate.selected_symbol()

    def test_catalog_or_configuration_changes_fail_closed(self):
        shadow.write_json(self.folder / 'catalog.json', {})
        with self.assertRaises(ValueError):
            shadow.load_config(self.folder)

    def test_duplicate_call_permit_cannot_repeat_provider_request(self):
        self.client.quotes(['NEWX'], fields='all', purpose='resolution', key='lookup-one')
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.quotes(['NEWX'], fields='all', purpose='resolution', key='lookup-one')
        self.assertEqual(len(self.transport.calls), 1)

    def test_lookup_call_budget_is_hard(self):
        for number in range(3):
            self.client.quotes(['NEWX'], fields='all', purpose='resolution', key='lookup-' + str(number))
        with self.assertRaises(ValueError):
            self.client.quotes(['NEWX'], fields='all', purpose='resolution', key='lookup-four')
        self.assertEqual(len(self.transport.calls), 3)

    def test_owned_stop_prevents_provider_request(self):
        (self.folder / 'STOP').write_text('test stop')
        with self.assertRaises(ValueError):
            self.client.quotes(['NEWX'], fields='all', purpose='resolution', key='stopped')
        self.assertEqual(self.transport.calls, [])

    def test_worker_and_stop_path_cannot_escape_its_package(self):
        with self.assertRaises(ValueError):
            shadow.validate_run_path(self.folder)
        root = Path(self.temp.name) / 'package'
        (root / 'runs').mkdir(parents=True)
        shadow.write_json(root / 'runs/active_nasdaq_shadow.json', {'contract': shadow.CONTRACT, 'folder': str(self.folder)})
        with self.assertRaises(ValueError):
            shadow.request_stop(root)
        self.assertFalse((self.folder / 'STOP').exists())

    def test_atomic_admission_drives_native_hot_and_commit_receipt_with_exact_replays(self):
        self.arrivals()
        self.gate_all()
        result = self.service.cycle()
        self.assertEqual(result['status'], 'published')
        revisions = shadow.store_for(self.folder, self.config).membership_revisions_in_effective_order()
        self.assertEqual(revisions[-1].hot_symbols, ('NEWX',))
        self.assertEqual(revisions[-1].focus_symbols, ('BASE', 'NEWX'))
        self.assertEqual(revisions[-1].uni_symbols, ('BASE', 'NEWX'))
        self.assertEqual(shadow.first_hot_slot(self.config, revisions), self.base + timedelta(seconds=4))
        self.now = self.base + timedelta(seconds=4)
        poller = shadow.poller_for(self.folder, self.client, clock=self.clock)
        poller.initialize()
        test_clock = self.clock
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return test_clock().astimezone(tz or timezone.utc)
        with patch('mb_market_data.schwab_quotes.datetime', LogicalDatetime):
            outcome = poller.sample(ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID))
        self.assertEqual(outcome['status'], 'acquired')
        self.assertEqual(len(shadow.lines(self.folder / 'hot_commit.jsonl')), 1)
        store = shadow.store_for(self.folder, self.config)
        self.assertEqual(store.acquisitions_in_replay_order()[0].channel_revision, revisions[-1].revision)
        self.assertEqual(replay_digest(store.database_path), replay_digest(store.database_path))
        self.assertEqual(poller.snapshot()['pending'], None)

    def test_tail_deadline_is_derived_from_real_publication_and_enforced_independently(self):
        self.arrivals()
        self.gate_all()
        self.service.cycle()
        eligible = shadow.first_hot_slot(self.config, shadow.store_for(self.folder, self.config).membership_revisions_in_effective_order())
        cutoff = eligible + timedelta(seconds=self.config['hot_tail_seconds'])
        shadow.write_json(self.folder / 'hot_window.json', {'first_eligible_at_utc': shadow.text(eligible), 'end_at_utc': shadow.text(cutoff)})
        stop = shadow.TailStop(self.folder, Event(), clock=self.clock)
        self.assertEqual(stop.deadline(), cutoff)
        self.now = cutoff
        self.assertTrue(stop.is_set())

    def test_forged_tail_deadline_fails_closed(self):
        shadow.write_json(self.folder / 'hot_window.json', {'first_eligible_at_utc': shadow.text(self.base), 'end_at_utc': shadow.text(self.base)})
        with self.assertRaises(ValueError):
            shadow.TailStop(self.folder, Event(), clock=self.clock).deadline()

    def test_endpoint_slot_cannot_enter_when_clock_crosses_cutoff_after_stop_check(self):
        self.arrivals()
        self.gate_all()
        self.service.cycle()
        eligible = shadow.first_hot_slot(self.config, shadow.store_for(self.folder, self.config).membership_revisions_in_effective_order())
        cutoff = eligible + timedelta(seconds=self.config['hot_tail_seconds'])
        shadow.write_json(self.folder / 'hot_window.json', {'first_eligible_at_utc': shadow.text(eligible), 'end_at_utc': shadow.text(cutoff)})
        poller = shadow.poller_for(self.folder, self.client, clock=self.clock)
        poller.initialize()
        self.now = cutoff - timedelta(microseconds=1)
        self.assertFalse(shadow.TailStop(self.folder, Event(), clock=self.clock).is_set())
        # The normal outer stop check has passed, then the wall clock crosses
        # the boundary before sample() is called. This is independent of OS.
        self.now = cutoff
        prior = dict(shadow.budget_counts(self.folder))
        test_clock = self.clock
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return test_clock().astimezone(tz or timezone.utc)
        with patch('mb_market_data.schwab_quotes.datetime', LogicalDatetime):
            outcome = poller.sample(ExplicitPollSlot(SamplingChannel.HOT, cutoff, shadow.GRID))
        self.assertEqual(outcome['status'], 'shadow_window_ended')
        self.assertEqual(shadow.budget_counts(self.folder), prior)
        self.assertEqual(shadow.store_for(self.folder, self.config).acquisitions_in_replay_order(), ())
        self.assertIsNone(poller.snapshot()['pending'])

    def published_cutoff(self):
        self.arrivals()
        self.gate_all()
        self.service.cycle()
        eligible = shadow.first_hot_slot(self.config, shadow.store_for(self.folder, self.config).membership_revisions_in_effective_order())
        return eligible, eligible + timedelta(seconds=self.config['hot_tail_seconds'])

    def test_future_endpoint_slot_is_excluded_before_any_native_dispatch(self):
        _, cutoff = self.published_cutoff()
        poller = shadow.poller_for(self.folder, self.client, clock=self.clock)
        poller.initialize()
        self.now = cutoff - timedelta(microseconds=1)
        before = len(self.transport.calls)
        result = poller.sample(ExplicitPollSlot(SamplingChannel.HOT, cutoff, shadow.GRID))
        self.assertEqual(result['status'], 'shadow_window_ended')
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(shadow.budget_counts(self.folder), {'resolution': 1})
        self.assertEqual(poller.snapshot()['finished'], {})

    def test_quote_guard_rejects_endpoint_without_consuming_a_permit(self):
        _, cutoff = self.published_cutoff()
        self.now = cutoff
        self.client.slot = ExplicitPollSlot(SamplingChannel.HOT, cutoff, shadow.GRID)
        with self.assertRaises(ValueError):
            self.client.quotes(['NEWX'], fields='all')
        self.assertEqual(shadow.budget_counts(self.folder), {'resolution': 1})
        self.assertEqual(self.transport.calls, [('NEWX',)])

    def test_quote_guard_rechecks_tail_after_budget_reservation(self):
        eligible, cutoff = self.published_cutoff()
        self.now = eligible + timedelta(seconds=4)
        self.client.slot = ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID)
        ticks = [0]
        def clock():
            ticks[0] += 1
            return cutoff if ticks[0] >= 5 else self.now
        self.client.clock = clock
        with self.assertRaises(RuntimeError):
            self.client.quotes(['NEWX'], fields='all')
        self.assertEqual(self.transport.calls, [('NEWX',)])
        self.assertEqual(shadow.budget_counts(self.folder), {'hot': 1, 'resolution': 1})
        self.assertFalse(shadow.lines(self.folder / 'hot_calls.jsonl')[-1]['dispatched'])

    def test_deadline_is_derived_without_parent_hint_file(self):
        _, cutoff = self.published_cutoff()
        self.assertFalse((self.folder / 'hot_window.json').exists())
        stop = shadow.TailStop(self.folder, Event(), clock=self.clock)
        self.assertEqual(stop.deadline(), cutoff)
        self.now = cutoff
        self.assertTrue(stop.is_set())

    def test_absolute_slot_wait_handles_an_early_wall_clock_tick(self):
        eligible, cutoff = self.published_cutoff()
        self.now = eligible - timedelta(microseconds=1)
        test = self
        class EarlyWake:
            waits = 0
            def is_set(self):
                return False
            def wait(self, seconds):
                self.waits += 1
                test.now += timedelta(microseconds=1)
                return False
        external = EarlyWake()
        stop = shadow.TailStop(self.folder, external, clock=self.clock)
        self.assertFalse(stop.wait_until(eligible))
        self.assertEqual(external.waits, 1)
        self.assertGreaterEqual(self.clock(), eligible)
        self.now = cutoff - timedelta(microseconds=1)
        self.assertTrue(stop.wait_until(cutoff))
        self.assertEqual(external.waits, 1)

    def test_live_window_requires_regular_weekday_before_cutoff(self):
        from zoneinfo import ZoneInfo
        et = ZoneInfo('America/New_York')
        shadow.check_live_window(datetime(2026, 10, 19, 13, 0, tzinfo=et))
        for value in (datetime(2026, 10, 19, 9, 29, tzinfo=et), datetime(2026, 10, 19, 15, 40, tzinfo=et),
                      datetime(2026, 10, 18, 13, 0, tzinfo=et)):
            with self.assertRaises(ValueError):
                shadow.check_live_window(value)

    def sample_report(self, *, invalid=False, no_event=False):
        self.capture(0)
        poller = shadow.poller_for(self.folder, self.client, clock=self.clock)
        poller.initialize()
        test_clock = self.clock
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return test_clock().astimezone(tz or timezone.utc)
        with patch('mb_market_data.schwab_quotes.datetime', LogicalDatetime):
            for seconds in range(0, 16 if no_event else 10, 2):
                if seconds == 4 and not no_event:
                    self.capture(3, event=True)
                    self.gate_all()
                    self.service.cycle()
                    if invalid:
                        self.transport.payload = {'errors': {'invalidSymbols': ['NEWX']}}
                self.now = self.base + timedelta(seconds=seconds)
                outcome = poller.sample(ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID))
                shadow.append_line(self.folder / 'hot_status.jsonl', outcome)
        shadow.append_line(self.folder / 'hot_status.jsonl', {'status': 'stopped', 'no_full_scan_during_polling': True})
        return shadow.analyse(self.folder, self.base + timedelta(seconds=16 if no_event else 10), 1 if no_event else 2, [])

    def test_no_event_report_is_readiness_only_and_does_not_call_provider(self):
        report = self.sample_report(no_event=True)
        self.assertEqual((report['result'], report['milestone']), ('READINESS_ONLY', 'READINESS_ONLY'))
        self.assertEqual((report['quote_calls'], report['hot_acquisitions'], report['empty_hot_skips']), (0, 0, 8))
        self.assertTrue(report['audit_passed'] and report['replays_identical'])

    def test_invalid_hot_outcomes_keep_independent_warning_after_confirmed_identity(self):
        report = self.sample_report(invalid=True)
        self.assertEqual(report['result'], 'PASS_WITH_WARNINGS')
        self.assertEqual(report['assessment']['availability']['invalid_observations'], 3)
        self.assertEqual(report['assessment']['collection_integrity']['result'], 'PASS')
        self.assertEqual(report['assessment']['transport']['result'], 'PASS')

    def test_hot_call_budget_prevents_thirty_first_request(self):
        folder = Path(self.temp.name) / 'live-shaped-budget'
        self.now = self.base - timedelta(seconds=10)
        config = shadow.prepare(folder, self.roster, self.catalog, source='live', clock=self.clock, start=self.base)
        gate = shadow.ShadowGate(folder, config, self.clock)
        gate.append('decision', 'controlled-budget-selection', symbol='NEWX', disposition='approved')
        client = shadow.QuoteClient(folder, self.transport, clock=self.clock)
        for number in range(30):
            self.now = self.base + timedelta(seconds=number * 2)
            client.slot = ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID)
            client.quotes(['NEWX'], fields='all')
        self.now += timedelta(seconds=2)
        client.slot = ExplicitPollSlot(SamplingChannel.HOT, self.now, shadow.GRID)
        with self.assertRaises(ValueError):
            client.quotes(['NEWX'], fields='all')
        self.assertEqual(len(self.transport.calls), 30)

    def test_authorization_callback_on_transport_exception_stops_the_shadow(self):
        from types import SimpleNamespace
        rejection = SimpleNamespace(requested=False)
        def fail(symbols, *, fields):
            rejection.requested = True
            raise RuntimeError('Private authorization URL')
        self.transport.quotes = fail
        self.client.rejection = rejection
        with self.assertRaises(RuntimeError):
            self.client.quotes(['NEWX'], fields='all', purpose='resolution', key='auth-lookup')
        self.assertTrue((self.folder / 'STOP').exists())
        self.assertNotIn('Private authorization URL', (self.folder / 'resolution_calls.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
