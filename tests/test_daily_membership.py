"""Shared-writer acceptance: synthetic identities, real native revisions."""
from contextlib import closing,redirect_stdout
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
import io,json,os,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from uuid import uuid4

from mb_market_data import daily_membership as dm, daily_nasdaq_source as ns
from mb_market_data import daily_runtime as rt, daily_flow as df, daily_health as dh
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.opening_hierarchy import write_opening_proposal
from mb_market_data.nasdaq_halts import HaltRecord,HaltFeed
from mb_market_data.acquisition_health import atomic_json
from test_volume_proxy_experiment import Response

UTC=timezone.utc

class IdentityClient:
    def __init__(self):self.calls=[];self.bad=set();self.fail=False
    def quotes(self,symbols,*,fields):
        self.calls.append(tuple(symbols))
        if self.fail:raise OSError('SYNTHETIC FAILURE')
        return Response({s:{'symbol':s,'assetMainType':'EQUITY','quote':{'lastPrice':2,'totalVolume':100}}
                         for s in symbols if s not in self.bad})

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.day=date(2026,11,10);self.at=datetime(2026,11,10,14,40,tzinfo=UTC)
        self.open=self.at.replace(minute=30);self.close=self.at.replace(hour=21,minute=0)
        self.r0=SamplingHierarchyRevision(self.day,0,self.open,('ABC','DEF'),(),(),'synthetic-opening')
        self.opening=self.root/'r0.json';write_opening_proposal(self.opening,self.r0)
        self.plan=dm.configure(dict(session_date=str(self.day),opens_at=self.open.isoformat(),closes_at=self.close.isoformat(),
            opening_path=str(self.opening),opening_content_sha256=self.r0.content_sha256,
            journal=str(self.root/(str(self.day)+'.sqlite3')),state_root=str(self.root/'state'),
            channels=['uni','focus','hot'],hot_seed_symbols=['MSFT','INTC','NVDA'],data_root=str(self.root),
            cadence_profile='interleaved-v1'),nasdaq=True)
        self.state=Path(self.plan['state_root']);self.state.mkdir();atomic_json(self.state/'plan.json',self.plan)
        self.store=QuoteObservationStore(self.plan['journal'],session_date=self.day,schema_version=2)
        self.clock_patch=patch('mb_market_data.quote_observation_store._utc_now',side_effect=lambda:self.at-timedelta(seconds=1))
        self.clock_patch.start();self.addCleanup(self.clock_patch.stop)
        self.store.initialize()
        with patch('mb_market_data.quote_observation_store._utc_now',return_value=self.open-timedelta(minutes=1)):
            self.store.record_membership_revision(self.r0)
            r1=replace(self.r0,revision=1,focus_symbols=('ABC',),source='synthetic-selection',metadata={'ranked_focus_symbols':['ABC']})
            self.store.record_membership_revision(r1)
            r2=replace(r1,revision=2,uni_symbols=('ABC','DEF','MSFT','INTC','NVDA'),focus_symbols=('ABC','MSFT','INTC','NVDA'),hot_symbols=('MSFT','INTC','NVDA'),source='operator-test-selection-v1')
            self.store.record_membership_revision(r2)
        self.client=IdentityClient();self.engine=dm.MembershipWriter(self.plan,self.client,clock=lambda:self.at)
    def command(self,symbol='INTC',target='focus',action='demote',source='manual',halt=None):
        v={'id':source+'-'+uuid4().hex,'symbol':symbol,'target':target,'action':action,'source':source}
        if halt is not None:v['halted_at']=halt.isoformat()
        return v
    def step(self,seconds=10):self.at+=timedelta(seconds=seconds)
    def apply(self,*a,**kw):return self.engine.apply(self.command(*a,**kw))

class MembershipTests(Fixture):
    def test_uni_only_outside_symbol_gets_uni_not_focus_hot(self):
        self.apply('NEW','uni','promote');r=dm.latest_revision(self.plan)
        self.assertIn('NEW',r.uni_symbols);self.assertNotIn('NEW',r.focus_symbols);self.assertNotIn('NEW',r.hot_symbols)
        self.assertEqual(self.client.calls,[('NEW',)])
    def test_focus_adds_uni_without_hot(self):
        self.apply('NEW','focus','promote');r=dm.latest_revision(self.plan)
        self.assertIn('NEW',r.uni_symbols);self.assertIn('NEW',r.focus_symbols);self.assertNotIn('NEW',r.hot_symbols)
    def test_hot_adds_both_parents(self):
        self.apply('NEW','hot','promote');r=dm.latest_revision(self.plan)
        self.assertTrue(all('NEW' in r.symbols_for(k) for k in ('uni','focus','hot')))
    def test_demote_seed_to_focus_keeps_parents(self):
        self.apply();r=dm.latest_revision(self.plan)
        self.assertNotIn('INTC',r.hot_symbols);self.assertIn('INTC',r.focus_symbols);self.assertIn('INTC',r.uni_symbols)
        self.assertFalse(self.client.calls)
    def test_demote_to_uni_removes_both_children(self):
        self.apply('INTC','uni');r=dm.latest_revision(self.plan)
        self.assertIn('INTC',r.uni_symbols);self.assertNotIn('INTC',r.focus_symbols);self.assertNotIn('INTC',r.hot_symbols)
    def test_promote_to_lower_level_does_not_demote(self):
        self.apply('INTC','uni','promote');self.assertIn('INTC',dm.latest_revision(self.plan).hot_symbols)
    def test_both_sources_same_symbol_demote_wins_then_new_halt_promotes(self):
        halt=self.at-timedelta(seconds=10)
        self.apply('NEW','hot','promote','nasdaq',halt)
        self.apply('NEW','hot','promote');self.step()
        self.apply('NEW','focus','demote');self.step()
        ignored=self.apply('NEW','hot','promote','nasdaq',halt)
        self.assertEqual(ignored['status'],'IGNORED');self.assertNotIn('NEW',dm.latest_revision(self.plan).hot_symbols)
        self.apply('NEW','hot','promote','nasdaq',self.at-timedelta(seconds=1))
        self.assertIn('NEW',dm.latest_revision(self.plan).hot_symbols)
        self.assertEqual(self.client.calls,[('NEW',)])
    def test_demote_before_first_delayed_halt_report_suppresses_old(self):
        halt=self.at-timedelta(seconds=5);self.apply();self.step()
        self.assertEqual(self.apply('INTC','hot','promote','nasdaq',halt)['status'],'IGNORED')
        self.assertNotIn('INTC',dm.latest_revision(self.plan).hot_symbols)
    def test_demotion_survives_writer_restart(self):
        halt=self.at-timedelta(seconds=5);self.apply();self.step()
        self.engine=dm.MembershipWriter(self.plan,self.client,clock=lambda:self.at)
        self.assertEqual(self.apply('INTC','hot','promote','nasdaq',halt)['status'],'IGNORED')
        self.step();self.apply('INTC','hot','promote','nasdaq',self.at-timedelta(seconds=1))
        self.assertIn('INTC',dm.latest_revision(self.plan).hot_symbols)
    def test_after_release_demotion_same_episode_does_not_restore(self):
        halt=self.at-timedelta(seconds=5);self.apply('INTC','hot','promote','nasdaq',halt)
        self.step(60);self.apply();self.step()
        self.assertEqual(self.apply('INTC','hot','promote','nasdaq',halt)['status'],'IGNORED')
    def test_identity_reused_across_sources_and_restart(self):
        self.apply('NEW','uni','promote');self.step()
        self.engine=dm.MembershipWriter(self.plan,self.client,clock=lambda:self.at)
        self.apply('NEW','hot','promote','nasdaq',self.at-timedelta(seconds=1))
        self.assertEqual(len(self.client.calls),1)
    def test_capacity_refuses_before_extra_identity_call(self):
        self.engine.plan=dict(self.plan,membership_max_hot=3)
        with self.assertRaises(dm.Rejected):self.apply('NEW','hot','promote')
        self.assertFalse(self.client.calls)
    def test_extra_capacity_counts_seeds(self):
        self.engine.plan=dict(self.plan,membership_max_extra=3)
        with self.assertRaises(dm.Rejected):self.apply('NEW','uni','promote')
        self.assertFalse(self.client.calls)
    def test_invalid_symbol_does_not_change_revision(self):
        self.client.bad={'NEW'}
        with self.assertRaises((dm.Rejected,dm.RetryLater)):self.apply('NEW','hot','promote')
        self.assertEqual(dm.latest_revision(self.plan).revision,2)
    def test_network_failure_does_not_change_existing_membership(self):
        self.client.fail=True
        with self.assertRaises(dm.RetryLater):self.apply('NEW','hot','promote')
        self.assertEqual(dm.latest_revision(self.plan).revision,2)
    def test_same_command_is_idempotent(self):
        cmd=self.command();r=self.engine.apply(cmd);self.assertEqual(self.engine.apply(cmd),r)
        self.assertEqual(dm.latest_revision(self.plan).revision,3)
    def test_commit_then_receipt_loss_recovers_without_new_revision(self):
        cmd=self.command('NEW','hot','promote');self.engine.apply(cmd)
        (self.engine.receipts/(cmd['id']+'.json')).unlink();self.engine=dm.MembershipWriter(self.plan,self.client,clock=lambda:self.at)
        r=self.engine.apply(cmd);self.assertEqual(r['revision'],3);self.assertEqual(len(self.client.calls),1)
    def test_ambiguous_commit_same_writer_recovers_without_republishing(self):
        cmd=self.command();original=self.engine.store.record_membership_revision
        def ambiguous(rev):
            original(rev);raise sqlite3.OperationalError('synthetic postcommit acknowledgement loss')
        with patch.object(self.engine.store,'record_membership_revision',side_effect=ambiguous):
            with self.assertRaises(sqlite3.OperationalError):self.engine.apply(cmd)
        result=self.engine.apply(cmd)
        self.assertEqual(result['revision'],3);self.assertEqual(dm.latest_revision(self.plan).revision,3)
    def test_reused_id_different_command_rejected(self):
        cmd=self.command();self.engine.apply(cmd);cmd['target']='uni'
        with self.assertRaises(dm.Rejected):self.engine.apply(cmd)
    def test_publication_failure_retains_original(self):
        with patch.object(self.engine.store,'record_membership_revision',side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaises(sqlite3.OperationalError):self.apply()
        self.assertEqual(dm.latest_revision(self.plan).revision,2)
    def test_publication_is_future_effective(self):
        r=self.apply();self.assertGreater(dm.timestamp(r['effective_at']),self.at)
        active=self.store.latest_membership_revision_effective_at(self.at)
        self.assertIn('INTC',active.hot_symbols)
    def test_ranked_focus_stays_distinct_and_original_file_unchanged(self):
        raw=self.opening.read_bytes();self.apply('NEW','hot','promote');r=dm.latest_revision(self.plan)
        self.assertEqual(list(r.metadata['ranked_focus_symbols']),['ABC']);self.assertEqual(raw,self.opening.read_bytes())
    def test_stop_after_identity_before_publication_cancels(self):
        self.engine.can_publish=lambda:False
        with self.assertRaises(dm.Rejected):self.apply('NEW','hot','promote')
        self.assertEqual(dm.latest_revision(self.plan).revision,2)
    def test_close_during_lookup_prevents_publication(self):
        def validator(*a):self.at=self.close+timedelta(seconds=1)
        self.engine.validate=validator
        with self.assertRaises(dm.Rejected):self.apply('NEW','hot','promote')
        self.assertEqual(dm.latest_revision(self.plan).revision,2)
    def test_unrelated_publisher_does_not_get_overwritten(self):
        rev=dm.latest_revision(self.plan)
        self.store.record_membership_revision(replace(rev,published_at=None,revision=3,effective_at=self.at+timedelta(seconds=5),source='other'))
        with self.assertRaises(dm.RetryLater):self.apply()
        self.assertEqual(dm.latest_revision(self.plan).source,'other')
    def test_receipt_distinguishes_requested_effective(self):
        r=self.apply();self.assertEqual(dm.report_result(self.state,r['id'],clock=lambda:self.at)['status'],'PUBLISHED_PENDING_EFFECTIVE')
        self.step();self.assertEqual(dm.report_result(self.state,r['id'],clock=lambda:self.at)['status'],'EFFECTIVE_AS_PUBLISHED')

class FeedTests(Fixture):
    def record(self,halt=None,reason='LUDP',resumption=''):
        at=halt or self.at-timedelta(seconds=1);et=at.astimezone(df.ET)
        return HaltRecord(et.strftime('%m/%d/%Y'),et.strftime('%H:%M:%S'),'NEW','Synthetic issue','NASDAQ','Q',reason,'',str(self.day) if resumption else '',resumption,resumption,self.at.isoformat(),'CURRENT')
    def feed(self,records,raw=b'<rss><channel/></rss>'):
        return HaltFeed('https://example.invalid/synthetic',self.at,'CURRENT',None,raw,tuple(records))
    def capture(self,records):return ns.accept_capture(self.plan,self.feed(records),owner_pid=100,eligible_since=self.open,root=self.state/'nasdaq')
    def test_same_episode_mutable_release_update_no_second_promotion(self):
        a=self.capture([self.record()]);self.assertEqual(a['queued'],1)
        b=self.capture([self.record(resumption='10:00:00')]);self.assertEqual(b['queued'],0)
        self.assertEqual(len(list((self.state/'membership/inbox').glob('*.json'))),1)
    def test_distinct_halt_produces_separate_command(self):
        self.capture([self.record()]);self.step();self.capture([self.record()])
        self.assertEqual(len(list((self.state/'membership/inbox').glob('*.json'))),2)
    def test_historical_startup_rows_not_promoted(self):
        r=ns.accept_capture(self.plan,self.feed([self.record()]),owner_pid=100,eligible_since=self.at,root=self.state/'nasdaq')
        self.assertEqual(r['queued'],0)
    def test_nonqualifying_reason_not_promoted(self):
        self.assertEqual(self.capture([self.record(reason='T1')])['queued'],0)
    def test_m_and_ludp_are_supported(self):
        self.assertEqual(self.capture([self.record(reason='M')])['queued'],1)
    def test_html_error_is_not_empty_valid_feed(self):
        with self.assertRaises(ValueError):ns.validate_feed(self.feed([],b'<html><body>Error</body></html>'))
    def test_malformed_xml_is_not_empty_valid_feed(self):
        with self.assertRaises(Exception):ns.validate_feed(self.feed([],b'not xml'))
    def test_raw_capture_precedes_command_and_is_retained(self):
        self.capture([self.record()]);cmd=dm.read(next((self.state/'membership/inbox').glob('*.json')))
        self.assertTrue(Path(cmd['capture_file']).is_file());self.assertTrue(list((self.state/'nasdaq/captures').glob('*.xml')))
    def test_source_outage_does_not_remove_hot(self):
        old=dm.latest_revision(self.plan).content_sha256
        with self.assertRaises(ValueError):ns.validate_feed(self.feed([],b'<html/>'))
        self.assertEqual(dm.latest_revision(self.plan).content_sha256,old)
    def test_future_halt_not_admitted(self):
        self.assertEqual(self.capture([self.record(halt=self.at+timedelta(seconds=1))])['queued'],0)
    def test_source_has_no_secret_handoff(self):
        with tempfile.TemporaryDirectory() as t,patch.object(rt.subprocess,'Popen') as pop,patch.object(rt,'secret_send') as secret:
            pop.return_value=Mock()
            rt.spawn('nasdaq',Path(t)/'plan.json','SECRET',Path(t)/'feed.log')
            self.assertEqual(pop.call_args.kwargs['stdin'],rt.subprocess.DEVNULL);secret.assert_not_called()

class ControlsTests(Fixture):
    def test_offline_submit_only_writes_request_not_journal(self):
        atomic_json(self.state/'owner.json',{'pid':10,'active':True,'at':self.at.isoformat()})
        atomic_json(self.state/'membership/status.json',{'owner_pid':10,'state':'RUNNING','at':self.at.isoformat()})
        before=Path(self.plan['journal']).read_bytes()
        with patch.object(rt,'owner_available',return_value=True):v=dm.submit(self.state,action='promote',symbol='new',target='hot',clock=lambda:self.at)
        self.assertEqual(v['symbol'],'NEW');self.assertEqual(before,Path(self.plan['journal']).read_bytes())
    def test_obsolete_worker_status_refuses_submission(self):
        atomic_json(self.state/'owner.json',{'pid':10,'active':True,'at':self.at.isoformat()})
        atomic_json(self.state/'membership/status.json',{'owner_pid':9,'state':'RUNNING','at':self.at.isoformat()})
        with patch.object(rt,'owner_available',return_value=True),self.assertRaises(ValueError):dm.submit(self.state,action='promote',symbol='NEW',target='hot',clock=lambda:self.at)
    def test_explicit_stop_refuses_new_commands(self):
        (self.state/'stop.request').write_text('stop')
        with patch.object(rt,'owner_available',return_value=True),self.assertRaises(ValueError):dm.submit(self.state,action='promote',symbol='NEW',target='hot',clock=lambda:self.at)
    def test_service_rejects_old_window_pending_request(self):
        cmd=self.command();cmd.update(owner_pid=1,opening_content_sha256=self.plan['opening_content_sha256'])
        atomic_json(self.state/'membership/inbox'/(cmd['id']+'.json'),cmd)
        ticks=[0]
        dm.service(self.plan,self.client,owner_pid=2,stop=lambda:ticks[0]>0,clock=lambda:self.at,sleep=lambda _:ticks.__setitem__(0,1))
        r=dm.report_result(self.state,cmd['id']);self.assertEqual(r['status'],'REJECTED');self.assertEqual(dm.latest_revision(self.plan).revision,2)
    def test_service_applies_both_sources_in_same_writer(self):
        for source,symbol in [('manual','ABC'),('nasdaq','DEF')]:
            cmd=self.command(symbol,'hot','promote',source,self.at-timedelta(seconds=1) if source=='nasdaq' else None)
            cmd.update(owner_pid=2,opening_content_sha256=self.plan['opening_content_sha256'])
            atomic_json(self.state/'membership/inbox'/(cmd['id']+'.json'),cmd)
        ticks=[0]
        dm.service(self.plan,self.client,owner_pid=2,stop=lambda:ticks[0]>=2,clock=lambda:self.at,sleep=lambda _:ticks.__setitem__(0,ticks[0]+1))
        self.assertTrue({'ABC','DEF'}<=set(dm.latest_revision(self.plan).hot_symbols))
    def test_cli_reports_effective_result_without_operator_copying_request_id(self):
        with patch.object(dm,'submit',return_value={'id':'manual-'+'a'*32}), \
             patch.object(dm,'report_result',return_value={'id':'manual-'+'a'*32,'status':'EFFECTIVE_AS_PUBLISHED'}), \
             redirect_stdout(io.StringIO()) as output:
            code=dm.main(['promote','--state-root',str(self.state),'--symbol','NEW','--to','hot'])
        self.assertEqual(code,0);self.assertIn('EFFECTIVE_AS_PUBLISHED',output.getvalue())
    def test_plan_opt_in_without_credentials_or_provider(self):
        cfg=dm.configure(self.plan,nasdaq=True)
        self.assertEqual(cfg['opening_content_sha256'],self.r0.content_sha256);self.assertTrue(cfg['automatic_nasdaq'])
        with self.assertRaises(ValueError):dm.configure(dict(self.plan,channels=['uni','focus']))

if __name__=='__main__':unittest.main(verbosity=2)

class RuntimeConnectionTests(unittest.TestCase):
    def fixture(self):
        import test_daily_runtime as tr
        from mb_market_data import daily_hot_seed as hs
        f=tr.RuntimeTests();f.setUp();self.addCleanup(f.doCleanups)
        f.plan=dm.configure(hs.configure(f.plan),nasdaq=True)
        return f,tr,hs
    def test_controller_launches_shared_writer_and_feed_once_after_selection(self):
        f,tr,hs=self.fixture();original=tr.acquire_selection
        monitor=Mock(pid=998);monitor.poll.return_value=None;monitor.wait.return_value=0
        def monitored(planfile,owner_pid):
            atomic_json(f.state/'health/ready.json',{'owner_pid':owner_pid,'alarm_started':True});return monitor
        def selected(client,plan,**kw):
            r=original(client,plan,**kw)
            with patch('mb_market_data.schwab_quotes.datetime',f.clock.datetime_type()):hs.validate_seeds(client,plan,f.state/'selection',clock=f.clock)
            return r
        with patch.object(tr,'acquire_selection',side_effect=selected),patch.object(dh,'spawn_monitor',side_effect=monitored):
            roles,post,result=f.exercise_controller()
        self.assertEqual(roles,['uni','selection','focus','hot','membership','nasdaq','postclose'])
        self.assertTrue(post);self.assertEqual(result['next_session_status']['status'],'READY')
    def test_manual_only_does_not_start_nasdaq_source(self):
        f,tr,hs=self.fixture();f.plan['automatic_nasdaq']=False;original=tr.acquire_selection
        monitor=Mock(pid=998);monitor.poll.return_value=None;monitor.wait.return_value=0
        def monitored(planfile,owner_pid):
            atomic_json(f.state/'health/ready.json',{'owner_pid':owner_pid,'alarm_started':True});return monitor
        def selected(client,plan,**kw):
            r=original(client,plan,**kw)
            with patch('mb_market_data.schwab_quotes.datetime',f.clock.datetime_type()):hs.validate_seeds(client,plan,f.state/'selection',clock=f.clock)
            return r
        with patch.object(tr,'acquire_selection',side_effect=selected),patch.object(dh,'spawn_monitor',side_effect=monitored):roles,_,_=f.exercise_controller()
        self.assertIn('membership',roles);self.assertNotIn('nasdaq',roles)
    def test_enabled_plan_only_makes_no_directories_or_password_calls(self):
        f,tr,hs=self.fixture();before=set(f.root.rglob('*'))
        with patch('getpass.getpass',side_effect=AssertionError('prompt')),redirect_stdout(io.StringIO()) as out:
            result=rt.main(['plan','--opening-proposal',str(f.r0),'--market-data-root',str(f.root),'--ecfg',str(f.root/'unused.ecfg'),
                           '--hot-symbols','MSFT','INTC','NVDA','--membership-controls','--nasdaq-halts'])
        self.assertEqual(result,0);self.assertIn('daily-shared-membership-v1',out.getvalue());self.assertEqual(before,set(f.root.rglob('*')))
