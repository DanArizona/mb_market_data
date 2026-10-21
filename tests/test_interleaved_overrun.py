"""Offline boundary regressions through native guarded acquisition and journal."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import json
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'coordinator_src'),
               str(ROOT/'experiments/interleaved_load_20261021')]
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.sampling_acquisition import (
    AcquisitionSettings, IsolatedSamplingPoller, MARKER)
from mb_market_data.sampling_incremental import IncrementalSamplingPoller, INCREMENTAL_MARKER
from mb_market_data.sampling_membership import SamplingChannel, SamplingHierarchyRevision
from mb_market_data.watchlist_polling import PollWindow
from overrun_scheduler import run_bounded_late, slow_proof_qualified, POLICY
import interleaved_load as harness


class ClockStop:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False
        self.oversleep = 0.

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        if not self.stopped:
            self.clock[0] += timedelta(seconds=seconds)
            if seconds > 0 and self.oversleep:
                self.clock[0] += timedelta(seconds=self.oversleep)
                self.oversleep = 0.
        return self.stopped


class Response:
    status_code = 200
    ok = True

    def __init__(self, symbols):
        self.symbols = symbols

    def json(self):
        return {s: {'quote': {'lastPrice': 1., 'securityStatus': 'Synthetic offline'}}
                for s in self.symbols}


class OverrunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.base = datetime(2026, 10, 23, 14, tzinfo=timezone.utc)
        self.clock = [self.base+timedelta(seconds=37)]
        self.stop = ClockStop(self.clock)
        self.delay = 3.818881
        self.post_delay = .542644
        self.callback = None
        self.calls = []
        self.rows = []
        self.store = QuoteObservationStore(self.folder/'quotes.sqlite3',
                                          session_date=self.base.date(), schema_version=2)
        self.store.initialize()
        self.store.record_membership_revision(SamplingHierarchyRevision(
            session_date=self.base.date(), revision=0, effective_at=self.base-timedelta(seconds=1),
            uni_symbols=('AAPL', 'MSFT', 'SPY'), focus_symbols=('AAPL', 'MSFT'),
            hot_symbols=('MSFT',), source='offline-overrun-boundary',
            metadata={MARKER: True, INCREMENTAL_MARKER: True, 'synthetic': True}))
        settings = AcquisitionSettings(SamplingChannel.HOT, (38,42,44,46),
            PollWindow(self.base, self.base+timedelta(seconds=48)),
            2, 'all', POLICY, .75, .05)
        test = self

        class Client:
            def quotes(self, symbols, *, fields):
                test.calls.append((self.slot.scheduled_at, test.clock[0], tuple(symbols)))
                test.clock[0] += timedelta(seconds=test.delay if self.slot.scheduled_at.second == 38 else .1)
                if test.callback:
                    test.callback(self.slot)
                return Response(symbols)

        client = Client()

        class Poller(IncrementalSamplingPoller):
            def sample(self, slot, **kwargs):
                client.slot = slot
                def after(stage):
                    if stage == 'after_assembled' and slot.scheduled_at.second == 38:
                        test.clock[0] += timedelta(seconds=test.post_delay)
                return super().sample(slot, fault_hook=after, **kwargs)

        self.poller = Poller(self.store, settings=settings, client=client,
                             clock=lambda:self.clock[0])
        self.poller.initialize()
        clock = self.clock

        class QuoteClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz or timezone.utc)

        self.patch = patch('mb_market_data.schwab_quotes.datetime', QuoteClock)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def run_loop(self):
        return run_bounded_late(self.poller, self.stop, on_status=self.rows.append)

    def audit(self):
        return audit_quote_journal(self.store.database_path,
                                  audited_at=self.poller.settings.window.end_at)

    def test_saved_boundary_reproduces_original_future_only_skip(self):
        result = IsolatedSamplingPoller.run_loop(self.poller, self.stop, on_status=self.rows.append)
        self.assertEqual(result['status'], 'window_ended')
        self.assertEqual([s.second for s,_,_ in self.calls], [38,44,46])
        self.assertEqual(next(c for c in self.audit().channels if c.channel=='hot').missing_slot_ids,
                         ('hot:2026-10-23T10:00:42-04:00',))

    def test_same_boundary_retains_slot_42_inside_original_750ms_budget(self):
        self.assertEqual(self.run_loop()['status'], 'window_ended')
        self.assertEqual([s.second for s,_,_ in self.calls], [38,42,44,46])
        self.assertEqual(self.calls[1][1], self.base+timedelta(seconds=42.361525))
        self.assertTrue(self.audit().passed)
        late = [r for r in self.rows if r.get('selected_after_overrun') and r['status']=='acquired']
        self.assertEqual(len(late),1)
        self.assertEqual(late[0]['dispatch_lateness_seconds'], .361525)

    def test_expired_slot_is_durable_miss_and_coverage_still_fails(self):
        self.post_delay = .931120  # finish at :42.750001
        self.run_loop()
        self.assertEqual([s.second for s,_,_ in self.calls], [38,44,46])
        missed = [r for r in self.rows if r['status']=='missed']
        self.assertEqual(len(missed),1)
        self.assertEqual(missed[0]['gate'],'before_dispatch_deadline')
        self.assertFalse(self.audit().passed)
        with closing(sqlite3.connect(self.poller.path)) as c:
            self.assertEqual(sum(json.loads(r[0])['kind']=='missed'
                for r in c.execute('select payload from acquisition_event')),1)

    def test_exact_dispatch_boundary_is_inclusive(self):
        self.post_delay = .931119  # finish at :42.750000
        self.run_loop()
        self.assertEqual(self.calls[1][1],self.base+timedelta(seconds=42.75))
        self.assertTrue(self.audit().passed)

    def test_long_overrun_records_all_expired_slots_without_backlog_requests(self):
        self.delay=9.1; self.post_delay=0.
        self.run_loop()
        self.assertEqual([s.second for s,_,_ in self.calls],[38])
        self.assertEqual(len([r for r in self.rows if r['status']=='missed']),3)
        self.assertEqual(len([r for r in self.rows if r['status']=='scheduler_overrun']),3)
        self.assertFalse(self.audit().passed)

    def test_window_end_blocks_all_later_api_dispatch(self):
        self.delay=12.; self.post_delay=0.
        self.run_loop()
        self.assertEqual(len(self.calls),1)
        self.assertTrue(all(r['gate']=='window_ended' for r in self.rows if r['status']=='missed'))

    def test_stop_during_slow_call_stops_before_any_late_dispatch(self):
        self.callback=lambda slot:setattr(self.stop,'stopped',True)
        self.assertEqual(self.run_loop()['status'],'stopped')
        self.assertEqual(len(self.calls),1)

    def test_stop_while_waiting_does_not_dispatch(self):
        self.stop.stopped=True
        self.assertEqual(self.run_loop()['status'],'stopped')
        self.assertEqual(self.calls,[])

    def test_starting_at_boundary_after_a_pause_does_not_replay_elapsed_slot(self):
        self.clock[0]=self.base+timedelta(seconds=42.361525)
        self.run_loop()
        self.assertEqual([s.second for s,_,_ in self.calls],[44,46])

    def test_wakeup_delayed_beyond_budget_cannot_bypass_native_gate(self):
        self.stop.oversleep=.8
        self.run_loop()
        self.assertNotIn(38,[s.second for s,_,_ in self.calls])
        self.assertEqual([r['gate'] for r in self.rows if r['status']=='missed'],['before_dispatch_deadline'])

    def test_busy_retry_retains_cursor_but_does_not_dispatch_expired_slot(self):
        original=self.poller.sample
        first=[True]
        def sample(slot,**kwargs):
            if first[0]:
                first[0]=False;self.clock[0]+=timedelta(seconds=.9)
                raise sqlite3.OperationalError('synthetic busy')
            return original(slot,**kwargs)
        with patch.object(self.poller,'sample',side_effect=sample):self.run_loop()
        self.assertEqual([s.second for s,_,_ in self.calls],[42,44,46])
        self.assertEqual(len([r for r in self.rows if r['status']=='missed']),1)
        self.assertEqual(len([r for r in self.rows if r['status']=='retrying']),1)

    def test_late_dispatch_keeps_membership_effective_at_scheduled_slot(self):
        def revise(slot):
            if slot.scheduled_at.second==38:
                previous=self.store.membership_revisions_in_effective_order()[-1]
                self.store.record_membership_revision(replace(previous,revision=1,published_at=None,
                    effective_at=self.base+timedelta(seconds=42.1),hot_symbols=('AAPL',)))
        self.callback=revise
        self.run_loop()
        self.assertEqual([symbols for _,_,symbols in self.calls],[('MSFT',),('MSFT',),('AAPL',),('AAPL',)])
        self.assertEqual([a.channel_revision for a in self.store.acquisitions_in_replay_order()],[0,0,1,1])
        self.assertTrue(self.audit().passed)

    def test_unknown_dispatched_response_is_not_issued_again_after_recovery(self):
        original=self.poller.sample
        first=[True]
        def sample(slot,**kwargs):
            if first[0]:
                first[0]=False
                def interrupted(stage):
                    if stage=='after_dispatch':
                        self.clock[0]+=timedelta(seconds=.1)
                        raise sqlite3.OperationalError('synthetic interrupted dispatch')
                return IncrementalSamplingPoller.sample(self.poller,slot,fault_hook=interrupted)
            return original(slot,**kwargs)
        with patch.object(self.poller,'sample',side_effect=sample):self.run_loop()
        self.assertEqual([s.second for s,_,_ in self.calls],[42,44,46])
        self.assertEqual(len([r for r in self.rows if r['status']=='unresolved_dispatch']),1)
        self.assertFalse(self.audit().passed)

    def test_saved_response_recovery_commits_once_then_handles_late_next_slot(self):
        original=self.poller.sample
        first=[True]
        def sample(slot,**kwargs):
            if first[0]:
                first[0]=False;self.poller.client.slot=slot
                def interrupted(stage):
                    if stage=='after_assembled':
                        self.clock[0]+=timedelta(seconds=self.post_delay)
                        raise sqlite3.OperationalError('synthetic post-response interruption')
                return IncrementalSamplingPoller.sample(self.poller,slot,fault_hook=interrupted)
            return original(slot,**kwargs)
        with patch.object(self.poller,'sample',side_effect=sample):self.run_loop()
        self.assertEqual([s.second for s,_,_ in self.calls],[38,42,44,46])
        self.assertTrue(self.audit().passed)
        self.assertEqual(len([r for r in self.rows if r.get('recovered')]),1)

    def test_completed_work_must_reconstruct_from_all_durable_sidecar_events(self):
        self.run_loop()
        checkpoint=self.poller.snapshot()
        self.assertEqual(len(checkpoint['finished']),4)
        self.assertEqual(len(checkpoint['events']),12)
        reopened=IncrementalSamplingPoller(self.store,settings=self.poller.settings,
            client=self.poller.client,clock=lambda:self.clock[0])
        reopened.initialize()
        self.assertEqual(reopened.snapshot()['head_sha256'],checkpoint['head_sha256'])
        self.assertIsNone(reopened.snapshot()['pending'])


class QualificationTests(unittest.TestCase):
    def report(self):
        return dict(source='synthetic_only',contract=harness.CONTRACT,duration_seconds=60,
            result='PASS_WITH_WARNINGS',slot_selection_policy=POLICY,
            logical_start='2026-10-23T14:00:00+00:00',
            slow_fixture=dict(enabled=True,at_seconds=18,call_seconds=3.818881,post_response_seconds=.35,focus_call_seconds=1.461032),
            audit_passed=True,replays_identical=True,request_replay_identical=True,
            publication_replay_identical=True,assessment=dict(all_observations_are_quotes=True),
            combined_symbol_cadence=dict(outcome_coverage_complete=True,result='WARNING'),failures=[],
            channels={c:dict(committed_slots=n,missing_slots=0,missed_gate_events=[],
                       late_acquisitions=['hot:2026-10-23T10:00:22-04:00'] if c=='hot' else [])
                      for c,n in (('uni',2),('focus',4),('hot',24))})

    def test_deliberate_gap_warning_is_retained_without_failing_complete_coverage_proof(self):
        report=self.report()
        self.assertTrue(slow_proof_qualified(report))
        self.assertEqual(report['result'],'PASS_WITH_WARNINGS')
        self.assertEqual(report['combined_symbol_cadence']['result'],'WARNING')

    def test_filling_late_slot_does_not_erase_preceding_delayed_response_gap(self):
        import cadence_analysis as cadence
        start=datetime(2026,10,23,14,tzinfo=timezone.utc)
        config=harness.plan(60);config.update(start=start.isoformat(),
            end=(start+timedelta(seconds=60)).isoformat(),initial_hot=['S0000'])
        rows=[];commits={}
        delayed={('hot',38):41.985346,('focus',40):41.763769,('hot',42):42.7}
        for channel,seconds in harness.GRIDS.items():
            for second in seconds:
                at=start+timedelta(seconds=second)
                receipt=start+timedelta(seconds=delayed.get((channel,second),second+.259947))
                rows.append(dict(symbol='S0000',status='quote',channel=channel,
                    scheduled_at_utc=at.isoformat(),response_received_at_utc=receipt.isoformat()))
                commits[channel,at]=receipt+timedelta(seconds=.05)
        report=cadence.analyse_rows(config,rows,commits)
        self.assertTrue(report['outcome_coverage_complete'])
        self.assertEqual(report['result'],'WARNING')
        self.assertEqual(report['all_channels']['response_gaps']['max_seconds'],5.503822)

    def test_missing_slot_failed_audit_or_replay_cannot_unlock_live(self):
        for key in ('audit_passed','replays_identical','request_replay_identical','publication_replay_identical'):
            with self.subTest(key=key):
                report=self.report();report[key]=False
                self.assertFalse(slow_proof_qualified(report))
        report=self.report();report['channels']['hot']['committed_slots']=23
        self.assertFalse(slow_proof_qualified(report))

    def test_absent_recovered_slot_or_nonquote_cannot_qualify(self):
        report=self.report();report['channels']['hot']['late_acquisitions']=[]
        self.assertFalse(slow_proof_qualified(report))
        report=self.report();report['assessment']['all_observations_are_quotes']=False
        self.assertFalse(slow_proof_qualified(report))

    def test_live_has_no_injected_delay_and_cannot_be_used_as_offline_proof(self):
        synthetic=harness.plan(60)
        roster={k:synthetic[k] for k in ('opening_uni','opening_focus','outside_symbol','algorithm_symbol','input_sha256')}
        live=harness.plan(1800,roster,'schwab')
        self.assertEqual(live['slow_fixture_call_seconds'],0)
        self.assertEqual(live['slow_fixture_post_response_seconds'],0)
        self.assertEqual(live['slow_fixture_focus_call_seconds'],0)
        self.assertEqual(live['overlap_delay_seconds'],0)
        self.assertEqual(live['total_quote_call_budget'],960)
        self.assertEqual(live['dispatch_deadline_seconds'],.75)
        report=self.report();report['source']='schwab'
        self.assertFalse(slow_proof_qualified(report))

    def test_verification_gate_accepts_only_declared_changed_path_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=root/'runs/interleaved_provider/example';run.mkdir(parents=True)
            source=root/'experiments/interleaved_load_20261021/source_manifest.json'
            source.parent.mkdir(parents=True);source.write_text('{}')
            report=self.report();path=run/'report.json';harness.write_json(path,report)
            gate=dict(result='PASS',proof_result='PASS_WITH_WARNINGS',report=str(path),
                qualification_scope='bounded-late-synthetic-slow-call-v1',
                source_manifest_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                report_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            harness.write_json(root/'runs/interleaved_provider_verification.json',gate)
            self.assertEqual(harness.check_verification(root)['result'],'PASS_WITH_WARNINGS')
            gate['qualification_scope']='old-proof'
            harness.write_json(root/'runs/interleaved_provider_verification.json',gate)
            with self.assertRaises(RuntimeError):harness.check_verification(root)


if __name__=='__main__':unittest.main()
