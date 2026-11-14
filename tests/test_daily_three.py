"""Three-channel acceptance: no live provider or credential access.

Real native journal and child-process boundaries; fake SDK and accelerated clock.
Desktop payload policy tested here; visible Windows sound/window checked separately.
"""
from contextlib import closing, nullcontext, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io,json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from mb_market_data import daily_flow as df, daily_hot_seed as hs, daily_runtime as rt
from mb_market_data import daily_health as dh, daily_attended_auth as aa
from mb_market_data.polling_cadence import cadence_seconds,INTERLEAVED,LEGACY,RESERVED_HOT_SECONDS
from mb_market_data.watchlist_polling import PollWindow,WatchlistKind,poll_slots_between
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.opening_hierarchy import write_opening_proposal
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.acquisition_health import atomic_json
from mb_market_data.polling_monitor_supervisor import MonitorWatchdog
from test_volume_proxy_experiment import Clock,Client

class ThreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.clock=Clock(hour=9,minute=25);self.day=self.clock().date();self.open=self.clock().replace(minute=30)
        self.r0=SamplingHierarchyRevision(session_date=self.day,revision=0,effective_at=self.open,
            uni_symbols=tuple(f'S{i:03}' for i in range(555)),focus_symbols=(),hot_symbols=(),source='offline-fixture')
        self.path=self.root/'r0.json';write_opening_proposal(self.path,self.r0)
        self.plan=hs.configure(df.make_plan(self.path,self.root,self.root/'unused.ecfg'))
        self.state=Path(self.plan['state_root']);self.client=Client(self.clock)
        self.patcher=patch('mb_market_data.quote_observation_store._utc_now',side_effect=lambda:self.clock().astimezone(timezone.utc))
        self.patcher.start();self.addCleanup(self.patcher.stop)
    def select(self):
        df.ensure_opening(self.plan)
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()):
            result=df.acquire_selection(self.client,self.plan,output=self.state/'selection',clock=self.clock,sleep=self.clock.sleep)
            proof=hs.validate_seeds(self.client,self.plan,self.state/'selection',clock=self.clock)
        df.publish_selection(self.plan,result,now=self.clock())
        return result,proof
    def test_three_seeds_do_not_change_ranked_forty_or_top100(self):
        result,proof=self.select();rank_before=(self.state/'selection'/'ranking.csv').read_bytes()
        saved=hs.publish_seeds(self.plan,now=self.clock());r2=df.read_revision(self.state/'selection'/'hot-seed-r2.json')
        self.assertEqual(saved['hot_symbols'],['INTC','MSFT','NVDA'])
        self.assertEqual(len(r2.uni_symbols),558);self.assertEqual(len(r2.focus_symbols),43)
        self.assertEqual(result['selected_focus'],list(r2.metadata['ranked_focus_symbols']))
        self.assertEqual(len(result['top_100']),100)
        self.assertEqual(rank_before,(self.state/'selection'/'ranking.csv').read_bytes())
        self.assertFalse(set(hs.DEFAULT_HOT)&set(result['top_100']))
        self.assertEqual(len(self.client.calls),3) # two original batches, one seed identity batch
        self.assertEqual(r2.source,'operator-test-selection-v1')
    def test_existing_members_are_not_duplicated_or_displaced(self):
        self.plan=hs.configure(self.plan,['S000','S001','S002']);result,_=self.select()
        hs.publish_seeds(self.plan,now=self.clock());r2=df.read_revision(self.state/'selection'/'hot-seed-r2.json')
        self.assertEqual(len(r2.uni_symbols),555);self.assertEqual(len(r2.focus_symbols),40)
        self.assertEqual(set(r2.focus_symbols),set(result['selected_focus']))
    def test_repeat_seed_publication_is_idempotent(self):
        self.select();a=hs.publish_seeds(self.plan,now=self.clock());b=hs.publish_seeds(self.plan,now=self.clock())
        self.assertEqual(a,b)
        with closing(sqlite3.connect(self.plan['journal'])) as c:self.assertEqual(c.execute('select count(*) from sampling_membership_revision').fetchone()[0],3)
    def test_resume_does_not_undo_later_demotion(self):
        self.select();hs.publish_seeds(self.plan,now=self.clock());r2=df.read_revision(self.state/'selection'/'hot-seed-r2.json')
        r3=replace(r2,revision=3,effective_at=self.open+timedelta(seconds=20),hot_symbols=('INTC','NVDA'),source='operator-demotion-fixture')
        store=QuoteObservationStore(self.plan['journal'],session_date=self.day,schema_version=2);store.record_membership_revision(r3)
        self.assertTrue(hs.seed_is_published(self.plan))
        self.assertEqual(store.latest_membership_revision_effective_at(self.open+timedelta(minutes=1)).hot_symbols,('INTC','NVDA'))
    def test_no_seed_publication_after_open(self):
        self.select()
        with self.assertRaises(ValueError):hs.publish_seeds(self.plan,now=self.open)
    def test_missing_seed_refuses_hot_but_r1_remains(self):
        self.client.missing={'NVDA'};result,proof=self.select()
        self.assertEqual(proof['status'],'UNAVAILABLE')
        with self.assertRaises(ValueError):hs.publish_seeds(self.plan,now=self.clock())
        self.assertTrue((self.state/'selection'/'publication.json').is_file())
        self.assertEqual(len(result['selected_focus']),40)
    def test_seed_validation_deadline_makes_no_provider_call(self):
        p=dict(self.plan,selection_deadline=self.clock().isoformat())
        value=hs.validate_seeds(self.client,p,self.root,clock=self.clock)
        self.assertEqual(value['status'],'UNAVAILABLE');self.assertEqual(self.client.calls,[])
    def test_seed_capacity_limit(self):
        with self.assertRaises(ValueError):hs.configure(self.plan,[f'X{i}' for i in range(41)])
        with self.assertRaises(ValueError):hs.configure(self.plan,[])
    def test_full_day_hot_cadence_has_9360_slots_no_collisions(self):
        slots=poll_slots_between(after=self.open-timedelta(microseconds=1),through=self.open.replace(hour=16,minute=0),
            window=PollWindow(self.open,self.open.replace(hour=16,minute=0)),watchlist_kinds=(WatchlistKind.HOT,),cadence_profile=INTERLEAVED)
        self.assertEqual(len(slots),9360);self.assertEqual(slots[-1].scheduled_at.second,58)
        self.assertFalse(set(cadence_seconds('hot',INTERLEAVED))&(set(cadence_seconds('uni',INTERLEAVED))|set(cadence_seconds('focus',INTERLEAVED))))
    def test_hot_refuses_legacy(self):
        with self.assertRaises(ValueError):cadence_seconds('hot',LEGACY)
    def child_proof(self,mode='healthy',channels=('uni','focus','hot')):
        self.select();hs.publish_seeds(self.plan,now=self.clock())
        self.plan['closes_at']=(self.open+timedelta(seconds=31)).isoformat()
        (self.root/'unused.ecfg').write_text('NOT A CREDENTIAL')
        atomic_json(self.state/'owner.json',{'pid':os.getpid(),'at':self.open.isoformat(),'active':True})
        p=self.root/'plan.json';atomic_json(p,self.plan)
        script=Path(__file__).with_name('daily_flow_process_fixture.py');children=[]
        for channel in channels:
            proc=subprocess.Popen([sys.executable,'-B',str(script),str(p),channel,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            rt.secret_send(proc,'OFFLINE-CANARY-NOT-A-REAL-PASSWORD');proc.stdin=None;children.append(proc)
        try:
            for proc in children:
                out,err=proc.communicate(timeout=30);self.assertEqual(proc.returncode,0,(out+err).decode())
                self.assertNotIn('OFFLINE-CANARY-NOT-A-REAL-PASSWORD',(out+err).decode())
        finally:
            for proc in children:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
        return self.plan
    def test_actual_three_children_collect_and_audit(self):
        self.child_proof()
        with closing(sqlite3.connect(self.plan['journal'])) as c:
            counts=dict(c.execute('select channel,count(*) from quote_acquisition group by channel'))
            self.assertEqual(counts,{'uni':2,'focus':2,'hot':12})
            rows=c.execute("select count(*) from quote_observation o join quote_acquisition a on o.acquisition_id=a.acquisition_id where a.channel='hot'").fetchone()[0]
            self.assertEqual(rows,36)
        audit=audit_quote_journal(self.plan['journal']);self.assertTrue(audit.passed,audit)
        a=list(QuoteJournalReplayReader(self.plan['journal']).events());b=list(QuoteJournalReplayReader(self.plan['journal']).events())
        self.assertEqual(a,b)
    def test_actual_hot_child_survives_heartbeat_glitch(self):
        self.child_proof(mode='read-glitch',channels=('hot',))
        with closing(sqlite3.connect(self.plan['journal'])) as c:self.assertEqual(c.execute("select count(*) from quote_acquisition where channel='hot'").fetchone()[0],12)
        receipt=rt.read_json(self.state/'workers'/'hot-0'/'exit.json');self.assertTrue(receipt['scheduled_completion'])
    def test_independent_inspector_reads_three_channels_without_writes(self):
        self.child_proof();before=Path(self.plan['journal']).read_bytes()
        snapshot=dh.inspect(self.plan,clock=lambda:self.open+timedelta(seconds=40),inventory={'known':True,'processes':[],'error':None})
        self.assertEqual(set(snapshot['channels']),{'uni','focus','hot'})
        self.assertEqual(snapshot['channels']['hot']['committed_acquisitions'],12)
        self.assertEqual(before,Path(self.plan['journal']).read_bytes())
    def test_delegated_inventory_requires_exact_plan_path_and_role(self):
        argv=['python','run_daily_session.py','_child','--role','hot','--plan-file',str(self.state/'plan.json')]
        inv={'known':True,'error':None,'processes':[{'pid':123,'argv':argv}]}
        mapped=dh.delegated_inventory(self.plan,inv)
        self.assertIn('probe_universe_quote_watch.py',mapped['processes'][0]['argv'])
        inv['processes'][0]['argv'][-1]=str(self.root/'other.json')
        self.assertNotIn('probe_universe_quote_watch.py',dh.delegated_inventory(self.plan,inv)['processes'][0]['argv'])
    def test_three_workers_all_use_private_interleaved_probe_path(self):
        for channel in ('uni','focus','hot'):
            argv=rt.worker_argv(self.plan,channel)
            self.assertEqual(argv[argv.index('--cadence-profile')+1],INTERLEAVED)
            self.assertNotIn('password',' '.join(argv))
    def test_config_keeps_original_opening_hash_and_adds_no_rank_inputs(self):
        self.assertEqual(self.plan['opening_content_sha256'],self.r0.content_sha256)
        self.assertEqual(self.plan['hot_seed_symbols'],['MSFT','INTC','NVDA'])
        self.assertTrue(self.plan['independent_monitor']);self.assertEqual(self.plan['focus_limit'],40)

class AttendedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.db=Path(self.temp.name)/'tokens.db';self.db.write_text('fake')
        self.now=datetime(2026,11,10,14,15,tzinfo=timezone.utc)
        self.plan={'ecfg':'unused','closes_at':'2026-11-10T16:00:00-05:00','postclose_window_seconds':900}
        self.client=Mock();self.messages=[]
        self.api=SimpleNamespace(DEFAULT_POLLING_REFRESH_MARGIN=timedelta(minutes=75,seconds=30),
            load_secure_schwab_config=Mock(return_value=SimpleNamespace(tokens_db=self.db)),
            read_schwab_token_status=Mock(side_effect=[self.status(),self.status()]),
            make_client_from_config=Mock(return_value=self.client),force_schwab_reauthorization=Mock(),
            console_auth_callback=Mock(return_value='PRIVATE-REDIRECT'))
    def status(self,refresh=None,access=None):
        return SimpleNamespace(refresh_token_expires_at=refresh or self.now+timedelta(days=7),
            access_token_expires_at=access or self.now+timedelta(minutes=30),require_refresh_valid_through=Mock())
    def call(self):return aa.attended_prepare(self.plan,'PRIVATE-PASSWORD',auth_lock=lambda _:nullcontext(),clock=lambda:self.now,secure_api=self.api,reporter=self.messages.append)
    def test_valid_authorization_does_not_open_browser(self):
        report=self.call();self.assertEqual(report['status'],'VERIFIED');self.api.console_auth_callback.assert_not_called();self.client.close.assert_called_once()
    def test_expired_authorization_is_renewed_in_attended_call(self):
        self.api.read_schwab_token_status.side_effect=[self.status(refresh=self.now-timedelta(days=2)),self.status()]
        def factory(*a,**kw):kw['call_on_auth']('PRIVATE-URL');return self.client
        self.api.make_client_from_config.side_effect=factory
        self.call();self.api.console_auth_callback.assert_called_once_with('PRIVATE-URL')
        self.assertNotIn('PRIVATE-',str(self.messages))
    def test_short_horizon_uses_existing_recoverable_renewal(self):
        self.api.read_schwab_token_status.side_effect=[self.status(refresh=self.now+timedelta(hours=1)),self.status()]
        self.call();self.api.force_schwab_reauthorization.assert_called_once()
    def test_new_database_can_authorize_normally(self):
        self.db.unlink();self.api.read_schwab_token_status.side_effect=[self.status()]
        self.call();self.assertEqual(self.api.read_schwab_token_status.call_count,1)
    def test_no_second_password_prompt(self):
        with patch('getpass.getpass',side_effect=AssertionError('second prompt')):self.call()
    def test_failed_persisted_refresh_is_never_verified(self):
        self.api.read_schwab_token_status.side_effect=[self.status(),self.status(refresh=self.now)]
        with self.assertRaises(aa.AuthorizationFailure):self.call()
        self.assertNotIn('AUTHORIZATION: VERIFIED',str(self.messages))
    def test_failed_client_suppresses_secret_exception(self):
        self.api.make_client_from_config.side_effect=RuntimeError('PRIVATE-PASSWORD')
        with self.assertRaises(aa.AuthorizationFailure) as error:self.call()
        self.assertNotIn('PRIVATE-',str(error.exception)+str(self.messages))
    def test_still_expired_access_is_reported(self):
        self.api.read_schwab_token_status.side_effect=[self.status(),self.status(access=self.now)]
        with self.assertRaises(aa.AuthorizationFailure) as err:self.call()
        self.assertEqual(err.exception.report['code'],'ACCESS_TOKEN_EXPIRED')
    def test_bad_password_does_not_trigger_browser(self):
        self.api.load_secure_schwab_config.side_effect=type('EcfgDecryptError',(Exception,),{})('PRIVATE-PASSWORD')
        with self.assertRaises(aa.AuthorizationFailure):self.call()
        self.api.console_auth_callback.assert_not_called()
    def test_unreadable_existing_db_is_not_deleted_or_rotated(self):
        self.api.read_schwab_token_status.side_effect=ValueError('PRIVATE-DATA')
        with self.assertRaises(aa.AuthorizationFailure):self.call()
        self.assertEqual(self.db.read_text(),'fake');self.api.force_schwab_reauthorization.assert_not_called()

class AlarmTests(unittest.TestCase):
    def snapshot(self,urgent=None,active=True):
        return {'journal':str(Path('2026-11-10.sqlite3').resolve()),'observed_at_utc':'2026-11-10T15:00:00+00:00',
                'channels':{c:{'state':'RUNNING','missing_slot_count':10} for c in ('uni','focus','hot')},
                'daily_conditions':urgent or {},'daily_active':active}
    def test_hot_failure_is_an_urgent_desktop_payload(self):
        result=dh.payload(self.snapshot({'hot:STOPPED':'Hot worker exited'}))
        self.assertIn('hot:STOPPED',result['urgent']);self.assertIn('HOT',result['text'])
    def test_historical_gaps_do_not_repeat_as_new_urgent_errors(self):
        result=dh.payload(self.snapshot({'hot:coverage':'10 historical gaps'}))
        self.assertFalse(result['urgent']);self.assertTrue(result['notices'])
    def test_dead_controller_is_urgent(self):
        result=dh.payload(self.snapshot({'monitor:controller_unavailable':'Controller stopped'}))
        self.assertTrue(result['urgent'])
    def test_dead_inspector_is_reported_independently(self):
        dog=MonitorWatchdog(child_pid=1,token='x',journal=Path('2026-11-10.sqlite3'),started_monotonic=0)
        state=dog.inspect(None,child_exit=2,now_monotonic=1)
        self.assertIn('monitor:STOPPED',dh.payload(self.snapshot(),state)['urgent'])
    def test_hung_inspector_is_reported_independently(self):
        dog=MonitorWatchdog(child_pid=1,token='x',journal=Path('2026-11-10.sqlite3'),started_monotonic=0)
        state=dog.inspect(None,child_exit=None,now_monotonic=31)
        self.assertIn('monitor:OVERDUE',dh.payload(self.snapshot(),state)['urgent'])
    def test_normal_close_moves_channel_conditions_to_notice(self):
        result=dh.payload(self.snapshot({'hot:STOPPED':'Ended'},active=False))
        self.assertFalse(result['urgent'])
    def test_empty_hot_skips_are_not_an_alert(self):
        snap=self.snapshot();snap['channels']['hot'].update(current_members=0,reason='Waiting for members')
        self.assertFalse(dh.payload(snap)['urgent'])
    def test_alarm_module_has_no_credential_handoff(self):
        source=Path(dh.__file__).read_text()
        self.assertNotIn('getpass',source);self.assertNotIn('secret_receive',source)

if __name__=='__main__':unittest.main(verbosity=2)

class ControllerIntegrationTests(unittest.TestCase):
    def fixture(self):
        import test_daily_runtime as tr
        f=tr.RuntimeTests();f.setUp();self.addCleanup(f.doCleanups)
        f.plan=hs.configure(f.plan)
        return f,tr
    def run_flow(self,invalid_seed=False):
        f,tr=self.fixture();original=tr.acquire_selection;observed=[]
        monitor=Mock(pid=998);monitor.poll.return_value=None;monitor.wait.return_value=0
        def monitor_spawn(planfile,owner_pid):
            self.assertFalse((f.state/'selection'/'publication.json').exists())
            atomic_json(f.state/'health'/'ready.json',{'owner_pid':owner_pid,'alarm_started':True})
            observed.append('monitor_ready');return monitor
        def select(client,plan,**kw):
            result=original(client,plan,**kw)
            if invalid_seed:client.missing.add('NVDA')
            with patch('mb_market_data.schwab_quotes.datetime',f.clock.datetime_type()):
                hs.validate_seeds(client,plan,f.state/'selection',clock=f.clock)
            return result
        with patch.object(tr,'acquire_selection',side_effect=select),patch.object(dh,'spawn_monitor',side_effect=monitor_spawn):
            launched,post,result=f.exercise_controller()
        return f,launched,post,result,observed
    def test_controller_starts_three_workers_and_keeps_postclose(self):
        f,roles,post,result,observed=self.run_flow()
        self.assertEqual(roles,['uni','selection','focus','hot','postclose'])
        self.assertEqual(result['hot_status']['hot_symbols'],['INTC','MSFT','NVDA'])
        self.assertEqual(result['next_session_status']['status'],'READY');self.assertTrue(post)
        self.assertEqual(observed,['monitor_ready']);self.assertTrue((f.state/'health'/'stop.request').exists())
    def test_seed_failure_keeps_ranked_focus_and_postclose(self):
        f,roles,post,result,_=self.run_flow(invalid_seed=True)
        self.assertEqual(roles,['uni','selection','focus','postclose'])
        self.assertIn('UNAVAILABLE',result['hot_status']);self.assertIn('r1 PUBLISHED',result['selection_status']);self.assertTrue(post)
    def test_monitor_not_ready_blocks_workers(self):
        f,tr=self.fixture();monitor=Mock(pid=998);monitor.poll.return_value=2
        with patch.object(rt,'now',f.clock),patch.object(rt,'prepared_client',return_value=Mock()),\
             patch.object(dh,'spawn_monitor',return_value=monitor),patch.object(rt,'spawn') as spawn,\
             patch.object(rt,'health_snapshot',return_value={}):
            with self.assertRaises(RuntimeError):rt.run_controller(f.plan,'offline')
        spawn.assert_not_called();self.assertIn('monitor',rt.read_json(f.state/'status.json')['error'])
    def test_start_one_password_with_attended_check_before_spawn(self):
        f,tr=self.fixture();(f.root/'unused.ecfg').write_text('fake')
        ordering=[];proc=Mock(pid=123);proc.poll.return_value=None
        with patch.object(rt,'now',f.clock),patch('getpass.getpass',return_value='PRIVATE-CANARY') as prompt,\
             patch('mb_market_data.polling_alarm.check_desktop'),\
             patch.object(rt,'attended_prepare',side_effect=lambda *a,**k:ordering.append('auth')),\
             patch.object(rt,'spawn',side_effect=lambda *a,**k:(ordering.append('spawn') or proc)),\
             patch.object(rt,'owner_available',return_value=True),patch.object(rt,'watch',return_value=0),redirect_stdout(io.StringIO()):
            code=rt.main(['start','--live','--opening-proposal',str(f.r0),'--market-data-root',str(f.root),
                '--ecfg',str(f.root/'unused.ecfg'),'--hot-symbols','MSFT','INTC','NVDA'])
        self.assertEqual(code,0);self.assertEqual(ordering,['auth','spawn']);prompt.assert_called_once()
        for path in f.root.rglob('*'):
            if path.is_file():self.assertNotIn(b'PRIVATE-CANARY',path.read_bytes())
    def test_renewal_crossing_deadline_does_not_start_or_fake_snapshot(self):
        f,tr=self.fixture();(f.root/'unused.ecfg').write_text('fake')
        def auth(*a,**k):f.clock.sleep(600)
        with patch.object(rt,'now',f.clock),patch('getpass.getpass',return_value='fake'),\
             patch('mb_market_data.polling_alarm.check_desktop'),patch.object(rt,'attended_prepare',side_effect=auth),\
             patch.object(rt,'spawn') as spawn:
            with self.assertRaises(ValueError):rt.main(['start','--live','--opening-proposal',str(f.r0),
                '--market-data-root',str(f.root),'--ecfg',str(f.root/'unused.ecfg'),'--hot-symbols','MSFT','INTC','NVDA'])
        spawn.assert_not_called();self.assertFalse(f.state.exists())
    def test_three_channel_plan_only_does_not_touch_credentials(self):
        f,tr=self.fixture();before=set(f.root.rglob('*'))
        with patch('getpass.getpass',side_effect=AssertionError()),patch.object(rt,'attended_prepare',side_effect=AssertionError()),redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rt.main(['plan','--opening-proposal',str(f.r0),'--market-data-root',str(f.root),
                '--ecfg',str(f.root/'unused.ecfg'),'--hot-symbols','MSFT','INTC','NVDA']),0)
        self.assertEqual(before,set(f.root.rglob('*')));self.assertIn('"hot_activated": true',out.getvalue())

class MonitorProcessTests(unittest.TestCase):
    def test_actual_independent_watchdog_survives_inspector_exit_and_emits_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'health').mkdir()
            plan={'state_root':str(root),'journal':str(root/'2026-11-10.sqlite3')}
            path=root/'plan.json';atomic_json(path,plan)
            script=Path(__file__).with_name('daily_health_process_fixture.py')
            proc=subprocess.Popen([sys.executable,'-B',str(script),'supervise',str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                out,err=proc.communicate(timeout=12);self.assertEqual(proc.returncode,0,(out+err).decode())
                record=rt.read_json(root/'health'/'alert-observed.json')
                self.assertIn('monitor:STOPPED',record['urgent'])
                self.assertTrue((root/'health'/'ready.json').is_file())
            finally:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
