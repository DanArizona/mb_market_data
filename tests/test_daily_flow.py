"""Daily flow: synthetic API responses, real artifacts/journals; no credentials."""
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import csv, io, json, os, sqlite3, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mb_market_data.daily_flow import *
from mb_market_data.daily_runtime import BackoffClient, worker_argv, owner_available, readable_status, verified_resume
from mb_market_data.quote_observation_store import PollRunProvenance
from test_volume_proxy_experiment import Client,Clock,Response
from test_nasdaq_symbol_directory import NASDAQ_RAW,OTHER_RAW
from mb_market_data.postclose_universe import build_with_client
from mb_market_data.daily_universe_snapshot import validate_acquisition_time

class DailyFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.day=date(2026,11,5);self.clock=Clock(hour=9,minute=25)
        self.r0=self.root/'r0.json';self.opening(555)
        self.plan=make_plan(self.r0,self.root,self.root/'synthetic-unused.ecfg')
        self.output=self.root/'selection'
        patcher=patch('mb_market_data.quote_observation_store._utc_now',side_effect=lambda:self.clock().astimezone(timezone.utc))
        patcher.start();self.addCleanup(patcher.stop)
    def opening(self,count):
        self.r0.unlink(missing_ok=True)
        r=SamplingHierarchyRevision(session_date=self.day,revision=0,
            effective_at=datetime.combine(self.day,time(9,30),ET),
            uni_symbols=tuple(f'X{i:03}' for i in range(count)),focus_symbols=(),hot_symbols=(),source='offline-fixture')
        write_opening_proposal(self.r0,r)
    def acquire(self,client=None):
        client=client or Client(self.clock)
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(io.StringIO()):
            return acquire_selection(client,self.plan,output=self.output,clock=self.clock,sleep=self.clock.sleep)
    def test_555_symbols_two_calls_top40_top100_full555_no_candles(self):
        c=Client(self.clock);r=self.acquire(c)
        self.assertEqual(r['quote_requests'],2);self.assertEqual(r['history_requests'],0)
        self.assertEqual([len(x[1]) for x in c.calls],[400,155])
        self.assertEqual(len(r['selected_focus']),40);self.assertEqual(len(r['top_100']),100)
        with (self.output/'ranking.csv').open() as f:self.assertEqual(len(list(csv.DictReader(f))),555)
        with (self.output/'top100.csv').open() as f:self.assertEqual(len(list(csv.DictReader(f))),100)
        self.assertEqual(r['selected_focus'],[f'X{i:03}' for i in range(40)])
    def test_publish_real_r0_and_current_volume_r1(self):
        ensure_opening(self.plan);r=self.acquire()
        self.assertTrue(publish_selection(self.plan,r,now=self.clock()))
        con=sqlite3.connect(self.plan['journal']);self.addCleanup(con.close)
        self.assertEqual(con.execute('SELECT count(*) FROM sampling_membership_revision').fetchone()[0],2)
        self.assertEqual(read_revision(Path(r['proposal'])).source,METHOD)
        self.assertEqual(read_revision(Path(r['proposal'])).hot_symbols,())
    def test_missing_volume_is_not_zero_and_does_not_prevent_remaining_selection(self):
        r=self.acquire(Client(self.clock,missing=['X000']))
        self.assertEqual(r['status'],'DEGRADED');self.assertEqual(r['missing_symbols'],['X000'])
        self.assertNotIn('X000',r['selected_focus']);self.assertEqual(r['quote_requests'],3)
    def test_all_missing_keeps_r0_and_creates_no_fake_r1(self):
        ensure_opening(self.plan)
        r=self.acquire(Client(self.clock,missing=[f'X{i:03}' for i in range(555)]))
        self.assertEqual(r['status'],'FOCUS_UNAVAILABLE');self.assertNotIn('proposal',r)
        self.assertFalse(publish_selection(self.plan,r,now=self.clock()))
    def test_zero_volumes_remain_eligible_with_symbol_tie_break(self):
        r=self.acquire(Client(self.clock,values={f'X{i:03}':0 for i in range(555)}))
        self.assertEqual(r['valid_symbols'],555);self.assertEqual(r['selected_focus'][0],'X000')
    def test_waits_to_0925_before_any_request(self):
        self.clock.now=self.clock.now.replace(minute=24,second=59)
        r=self.acquire();self.assertEqual(r['started_at'],datetime.combine(self.day,time(9,25),ET).isoformat())
    def test_late_preopen_start_is_reported(self):
        self.clock.now=self.clock.now.replace(minute=26)
        self.assertEqual(self.acquire()['start_lateness_seconds'],60)
    def test_no_new_snapshot_at_or_after_0929(self):
        self.clock.now=self.clock.now.replace(minute=29);c=Client(self.clock)
        with self.assertRaises(ValueError):self.acquire(c)
        self.assertFalse(c.calls)
    def test_batch_returned_after_deadline_not_ranked(self):
        self.clock.now=self.clock.now.replace(minute=28,second=59)
        c=Client(self.clock);old=c.quotes
        def late(*a,**k):self.clock.sleep(11);return old(*a,**k)
        c.quotes=late;r=self.acquire(c)
        self.assertEqual(r['valid_symbols'],0);self.assertNotIn('proposal',r)
    def test_fewer_than40_valid_symbols_gives_smaller_roster(self):
        r=self.acquire(Client(self.clock,missing=[f'X{i:03}' for i in range(10,555)]))
        self.assertEqual(len(r['selected_focus']),10);self.assertEqual(r['status'],'DEGRADED')
    def test_no_publication_after_open(self):
        ensure_opening(self.plan);r=self.acquire()
        with self.assertRaisesRegex(ValueError,'backdate'):
            publish_selection(self.plan,r,now=self.clock().replace(hour=9,minute=30))
    def test_repeated_exact_publication_is_idempotent(self):
        ensure_opening(self.plan);r=self.acquire()
        publish_selection(self.plan,r,now=self.clock());publish_selection(self.plan,r,now=self.clock())
        con=sqlite3.connect(self.plan['journal']);self.addCleanup(con.close)
        self.assertEqual(con.execute('SELECT count(*) FROM sampling_membership_revision').fetchone()[0],2)
    def test_modified_roster_refused_before_market_request(self):
        self.opening(100);c=Client(self.clock)
        with self.assertRaisesRegex(ValueError,'changed'):self.acquire(c)
        self.assertFalse(c.calls)
    def test_existing_output_not_overwritten(self):
        self.acquire()
        before=(self.output/'result.json').read_bytes()
        with self.assertRaises(FileExistsError):self.acquire()
        self.assertEqual((self.output/'result.json').read_bytes(),before)
    def test_fresh_launcher_refuses_existing_poll_runs(self):
        ensure_opening(self.plan)
        store=QuoteObservationStore(self.plan['journal'],session_date=self.day,schema_version=2)
        store.record_run(PollRunProvenance('existing',self.clock(),'test',
           {'watchlist_kind':'uni','poll_seconds':[0,30],'cadence_profile':'interleaved-v1'}))
        with self.assertRaisesRegex(ValueError,'already has polling'):ensure_opening(self.plan)
    def test_legacy_profile_refused_without_changing_journal(self):
        ensure_opening(self.plan)
        store=QuoteObservationStore(self.plan['journal'],session_date=self.day,schema_version=2)
        store.record_run(PollRunProvenance('legacy',self.clock(),'test',
           {'watchlist_kind':'focus','poll_seconds':[5,20,35,50],'cadence_profile':'legacy-v1'}))
        with self.assertRaises(ValueError):make_plan(self.r0,self.root,self.root/'unused')
    def test_carried_roster_requires_explicit_acknowledgment(self):
        r=replace(load_opening_proposal(self.r0),metadata={'test_only':True,'roster_origin_session':'2026-11-04'})
        self.r0.unlink();write_opening_proposal(self.r0,r)
        with self.assertRaisesRegex(ValueError,'allow-carried'):make_plan(self.r0,self.root,self.root/'unused')
        p=make_plan(self.r0,self.root,self.root/'unused',allow_carried=True)
        self.assertEqual(p['opening_metadata']['roster_origin_session'],'2026-11-04')
    def test_exact_calendar_normal_early_holiday_next_session(self):
        self.assertEqual(schedule_for(date(2026,11,6)).closes_at.hour,16)
        early=schedule_for(date(2026,11,27));self.assertEqual(early.closes_at.hour,13)
        self.assertEqual(early.next_day,date(2026,11,30))
        with self.assertRaises(ValueError):schedule_for(date(2026,11,26))
    def test_early_close_snapshot_gate_matches_calendar(self):
        d=date(2026,11,27);close=schedule_for(d).closes_at
        validate_acquisition_time(d,close,exchange_close=close)
        with self.assertRaises(ValueError):validate_acquisition_time(d,close-timedelta(seconds=1),exchange_close=close)
        with self.assertRaises(ValueError):validate_acquisition_time(d,close,exchange_close=close-timedelta(hours=1))
    def test_plan_and_workers_force_same_interleaved_profile(self):
        for c in ('uni','focus'):
            a=worker_argv(self.plan,c)
            self.assertEqual(a[a.index('--cadence-profile')+1],'interleaved-v1')
            self.assertEqual(a[a.index('--stop-at')+1],self.plan['closes_at'])
            self.assertNotIn('password',' '.join(a).lower())
    def test_postclose_build_and_publication_uses_supplied_client(self):
        self.clock.now=self.clock.now.replace(hour=16,minute=1)
        c=Client(self.clock)
        def builder(*a,**kw):
            return build_with_client(*a,**kw,directory_fetch=lambda u,**k:NASDAQ_RAW if 'nasdaqlisted' in u else OTHER_RAW)
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(io.StringIO()):
            result=build_next_opening(c,self.plan,clock=self.clock,builder=builder)
        self.assertEqual(result['status'],'READY');self.assertTrue(result['membership_published'])
        self.assertEqual(c.closed,0);self.assertEqual([x[0] for x in c.calls],['quotes'])
        self.assertTrue(journal_path(self.root,date(2026,11,6)).is_file())
    def test_early_close_build_publishes_actual_next_trading_date(self):
        day=date(2026,11,27);r=load_opening_proposal(self.r0)
        self.r0.unlink();write_opening_proposal(self.r0,replace(r,session_date=day,effective_at=datetime.combine(day,time(9,30),ET)))
        self.plan=make_plan(self.r0,self.root,self.root/'unused')
        self.clock.now=datetime.combine(day,time(13,1),ET)
        c=Client(self.clock)
        def builder(*a,**kw):
            return build_with_client(*a,**kw,directory_fetch=lambda u,**k:NASDAQ_RAW if 'nasdaqlisted' in u else OTHER_RAW)
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(io.StringIO()):
            result=build_next_opening(c,self.plan,clock=self.clock,builder=builder)
        self.assertEqual(result['status'],'READY');self.assertEqual(result['target_session'],'2026-11-30')
        self.assertTrue(journal_path(self.root,date(2026,11,30)).exists())
    def test_build_success_publication_failure_remains_pending(self):
        self.clock.now=self.clock.now.replace(hour=16,minute=1)
        def builder(*a,**kw):
            return build_with_client(*a,**kw,directory_fetch=lambda u,**k:NASDAQ_RAW if 'nasdaqlisted' in u else OTHER_RAW)
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(io.StringIO()), \
             patch.object(QuoteObservationStore,'record_membership_revision',side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(OSError):build_next_opening(Client(self.clock),self.plan,clock=self.clock,builder=builder)
        receipt=next(self.root.rglob('daily_handoff.json'))
        self.assertEqual(json.loads(receipt.read_text())['status'],'BUILT_PUBLICATION_PENDING')
    def test_repeated_postclose_reuses_complete_build_without_provider_calls(self):
        self.clock.now=self.clock.now.replace(hour=16,minute=1)
        def builder(*a,**kw):
            return build_with_client(*a,**kw,directory_fetch=lambda u,**k:NASDAQ_RAW if 'nasdaqlisted' in u else OTHER_RAW)
        c=Client(self.clock)
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(io.StringIO()):
            first=build_next_opening(c,self.plan,clock=self.clock,builder=builder)
            calls=len(c.calls)
            second=build_next_opening(c,self.plan,clock=self.clock,builder=builder)
        self.assertEqual(second['status'],'READY');self.assertTrue(second['reused_complete_build'])
        self.assertEqual(len(c.calls),calls)
    def test_postclose_provider_failure_retains_raw_but_never_publishes(self):
        self.clock.now=self.clock.now.replace(hour=16,minute=1)
        c=Client(self.clock);c.quotes=Mock(side_effect=TimeoutError())
        def builder(*a,**kw):return build_with_client(*a,**kw,directory_fetch=lambda u,**k:NASDAQ_RAW if 'nasdaqlisted' in u else OTHER_RAW)
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'Incomplete'):build_next_opening(c,self.plan,clock=self.clock,builder=builder)
        self.assertFalse(journal_path(self.root,date(2026,11,6)).exists())
        self.assertTrue(list(self.root.rglob('quote_acquisition.json')))
    def test_postclose_before_close_makes_no_calls(self):
        builder=Mock()
        with self.assertRaises(ValueError):build_next_opening(object(),self.plan,clock=self.clock,builder=builder)
        builder.assert_not_called()
    def test_resume_saved_ranking_does_not_call_provider_or_rerank(self):
        from mb_market_data.acquisition_health import atomic_json
        ensure_opening(self.plan);r=self.acquire();publish_selection(self.plan,r,now=self.clock())
        state=Path(self.plan['state_root']);state.mkdir(parents=True)
        atomic_json(state/'plan.json',self.plan)
        import shutil;shutil.copytree(self.output,state/'selection')
        result=verified_resume(self.plan,state)
        self.assertEqual(result['selected_focus'],r['selected_focus'])

class BackoffTests(unittest.TestCase):
    def test_authorization_failure_latches_without_second_provider_call(self):
        c=Mock();response=Response({});response.status_code=401;c.quotes.return_value=response
        w=BackoffClient(c);w.quotes(['A'],fields='all')
        with self.assertRaisesRegex(RuntimeError,'authorization'):w.quotes(['A'],fields='all')
        self.assertEqual(c.quotes.call_count,1)
    def test_rate_limit_wait_is_bounded_then_recovers(self):
        tick=[0.];c=Mock();response=Response({});response.status_code=429;response.headers={'Retry-After':'90'}
        c.quotes.return_value=response;w=BackoffClient(c,clock=lambda:tick[0])
        w.quotes(['A'],fields='all')
        tick[0]=89
        with self.assertRaises(RuntimeError):w.quotes(['A'],fields='all')
        tick[0]=91;w.quotes(['A'],fields='all');self.assertEqual(c.quotes.call_count,2)
    def test_old_owner_heartbeat_cannot_authorize_new_invocation(self):
        from mb_market_data.acquisition_health import atomic_json
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);atomic_json(p/'owner.json',{'pid':12,'at':datetime.now(ET).isoformat(),'active':True})
            self.assertTrue(owner_available(p,pid=12));self.assertFalse(owner_available(p,pid=13))
    def test_status_shows_actual_progress_and_gaps(self):
        value={'session_date':'2026-11-05','phase':'COLLECTING','at':'09:31','pid':1,
               'health':{'channels':{'uni':{'state':'RUNNING','reason':'recent work','committed_acquisitions':3,
               'missing_slot_count':1,'last_committed_slot_utc':'time','current_members':555}}}}
        text=readable_status(value)
        self.assertIn('DEGRADED',text);self.assertIn('committed_acquisitions',text);self.assertIn('555',text)

if __name__=='__main__':unittest.main()
