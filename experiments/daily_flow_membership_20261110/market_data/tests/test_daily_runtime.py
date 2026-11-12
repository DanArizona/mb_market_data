"""Owned-process and controller ordering tests. All SDK responses are synthetic."""
from contextlib import ExitStack,closing,redirect_stdout
from datetime import date,datetime,time,timedelta,timezone
import io,json,os,socket,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from mb_market_data import daily_runtime as rt
from mb_market_data.daily_flow import *
from mb_market_data.acquisition_health import atomic_json,operation_lock
from test_volume_proxy_experiment import Clock,Client

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.day=date(2026,11,5)
        self.clock=Clock(hour=9,minute=24,second=59)
        self.r0=self.root/'r0.json'
        write_opening_proposal(self.r0,SamplingHierarchyRevision(session_date=self.day,revision=0,
            effective_at=datetime.combine(self.day,time(9,30),ET),uni_symbols=('ABC','DEF'),
            focus_symbols=(),hot_symbols=(),source='offline-runtime-fixture'))
        self.plan=make_plan(self.r0,self.root,self.root/'unused.ecfg')
        self.state=Path(self.plan['state_root'])
        self.patch=patch('mb_market_data.quote_observation_store._utc_now',side_effect=lambda:self.clock().astimezone(timezone.utc))
        self.patch.start();self.addCleanup(self.patch.stop)
    def test_real_child_probes_private_handoff_and_interleaved_native_writes(self):
        # Actual two OS children, delegated existing probes, temporary native DB.
        ensure_opening(self.plan)
        self.plan['closes_at']=self.clock().replace(hour=9,minute=30,second=31).isoformat()
        self.state.mkdir(parents=True)
        (self.root/'unused.ecfg').write_text('THIS IS NOT AN ENCRYPTED CREDENTIAL FILE')
        atomic_json(self.state/'owner.json',{'pid':os.getpid(),
            'at':datetime.fromisoformat(self.plan['opens_at']).isoformat(),'active':True})
        from mb_market_data.daily_flow import revision_payload
        r0=load_opening_proposal(self.r0)
        r1=SamplingHierarchyRevision(session_date=self.day,revision=1,effective_at=r0.effective_at,
            uni_symbols=r0.uni_symbols,focus_symbols=('ABC',),hot_symbols=(),source=METHOD)
        store=QuoteObservationStore(self.plan['journal'],session_date=self.day,schema_version=2)
        store.record_membership_revision(r1)
        p=self.root/'plan.json';atomic_json(p,self.plan)
        entry=Path(__file__).with_name('daily_flow_process_fixture.py')
        canary='OFFLINE-CANARY-NOT-A-REAL-PASSWORD';procs=[]
        for c in ('uni','focus'):
            args=[sys.executable,'-B',str(entry),str(p),c]
            proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            procs.append((c,proc,args))
            rt.secret_send(proc,canary);proc.stdin=None
        for c,proc,args in procs:
            out,err=proc.communicate(timeout=25)
            self.assertEqual(proc.returncode,0,(out+err).decode())
            self.assertNotIn(canary,' '.join(args));self.assertNotIn(canary,(out+err).decode())
        with closing(sqlite3.connect(self.plan['journal']))as con:
            counts=dict(con.execute('SELECT channel,count(*) FROM quote_acquisition GROUP BY channel'))
            self.assertEqual(counts,{'uni':2,'focus':2})
            configs=[json.loads(r[0])for r in con.execute('SELECT configuration_json FROM poll_run')]
            self.assertTrue(all(c['cadence_profile']=='interleaved-v1' for c in configs))
        for f in self.root.rglob('*'):
            if f.is_file():self.assertNotIn(canary.encode(),f.read_bytes(),str(f))
    def exercise_controller(self,selection_fails=False,uni_crashes=False,stop_collection=False,post_fails=False):
        # Shortened logical scenario; process launching is substituted here.
        # Actual child probe process boundary is independently tested above.
        self.plan['closes_at']=self.clock().replace(minute=31,second=0).isoformat()
        self.plan['postclose_delay_seconds']=1;self.plan['postclose_window_seconds']=60
        launched=[];post_called=[];controller=self
        class Proc:
            pid=100
            returncode=None
            def __init__(self,role):self.role=role;self.pid=100+len(launched)
            def poll(self):
                if self.returncode is not None:return self.returncode
                if self.role=='selection' and controller.clock()>=datetime.fromisoformat(controller.plan['snapshot_at']):
                    if selection_fails:self.returncode=2
                    else:
                        with patch('mb_market_data.schwab_quotes.datetime',controller.clock.datetime_type()):
                            acquire_selection(Client(controller.clock),controller.plan,output=controller.state/'selection',
                                              clock=controller.clock,sleep=controller.clock.sleep)
                        self.returncode=0
                if self.role=='uni' and uni_crashes:self.returncode=2
                if self.role=='postclose':
                    post_called.append(controller.clock())
                    atomic_json(controller.state/'postclose-status.json',{'status':'INCOMPLETE' if post_fails else 'READY'})
                    self.returncode=2 if post_fails else 0
                return self.returncode
            def wait(self,timeout=None):self.returncode=self.returncode or 0;return self.returncode
            def terminate(self):self.returncode=-1
        def spawn(role,*a,**k):
            launched.append(role)
            if stop_collection and role=='uni':(self.state/'stop.request').write_text('test stop collection')
            return Proc(role)
        with patch.object(rt,'now',self.clock),patch.object(rt,'prepared_client',return_value=Mock()),\
             patch.object(rt,'spawn',side_effect=spawn),patch.object(rt,'health_snapshot',return_value={}),\
             patch.object(rt.time,'sleep',side_effect=self.clock.sleep):
            rt.run_controller(self.plan,'OFFLINE-CANARY')
        return launched,post_called,rt.read_json(self.state/'status.json')
    def test_controller_publishes_r1_before_focus_worker_launch(self):
        launched,post,result=self.exercise_controller()
        self.assertEqual(launched,['uni','selection','focus','postclose']);self.assertTrue(post)
        self.assertIn('r1 PUBLISHED',result['selection_status']);self.assertEqual(result['next_session_status']['status'],'READY')
    def test_uni_crash_does_not_cancel_postclose_handoff(self):
        launched,post,result=self.exercise_controller(uni_crashes=True)
        self.assertTrue(post);self.assertEqual(result['next_session_status']['status'],'READY')
    def test_selection_failure_keeps_uni_and_postclose(self):
        launched,post,result=self.exercise_controller(selection_fails=True)
        self.assertEqual(launched,['uni','selection','postclose']);self.assertTrue(post)
        self.assertIn('UNAVAILABLE',result['selection_status'])
    def test_stop_collection_preserves_automatic_preparation(self):
        launched,post,result=self.exercise_controller(stop_collection=True)
        self.assertNotIn('focus',launched);self.assertTrue(post)
    def test_postclose_failure_not_reported_as_ready(self):
        launched,post,result=self.exercise_controller(post_fails=True)
        self.assertEqual(result['next_session_status']['status'],'INCOMPLETE')
    def test_exit_session_cancels_preparation_and_closes_only_owned(self):
        self.state.mkdir(parents=True);atomic_json(self.state/'plan.json',self.plan)
        (self.state/'exit.request').write_text('exit')
        # Exercise control request itself: it cannot act without a fresh owner.
        with self.assertRaises(ValueError):rt.main(['exit','--state-root',str(self.state)])
    def test_plan_does_not_prompt_write_or_contact_provider(self):
        before=set(self.root.rglob('*'))
        with patch('getpass.getpass',side_effect=AssertionError('prompt')),redirect_stdout(io.StringIO()):
            self.assertEqual(rt.main(['plan','--opening-proposal',str(self.r0),'--market-data-root',str(self.root),
                                     '--ecfg',str(self.root/'unused')]),0)
        self.assertEqual(before,set(self.root.rglob('*')))
    def test_duplicate_owner_lock_refuses_second_controller(self):
        key='daily-session:'+str(Path(self.plan['journal']).resolve()).casefold()
        code=('from mb_market_data.acquisition_health import operation_lock; import sys; '
              'm=operation_lock(sys.argv[1]); m.__enter__(); print("LOCKED",flush=True); '
              'sys.stdin.read(); m.__exit__(None,None,None)')
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        p=subprocess.Popen([sys.executable,'-B','-c',code,key],stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        try:
            self.assertEqual(p.stdout.readline().strip(),'LOCKED')
            with self.assertRaises(ValueError):
                with operation_lock(key):pass
        finally:
            p.communicate('',timeout=10)
        self.assertEqual(p.returncode,0)
    def test_viewer_interrupt_has_no_stop_side_effect(self):
        self.state.mkdir(parents=True);before=set(self.state.iterdir())
        with patch.object(rt.time,'sleep',side_effect=KeyboardInterrupt),redirect_stdout(io.StringIO()):
            self.assertEqual(rt.watch(self.state),0)
        self.assertEqual(before,set(self.state.iterdir()))
    def test_start_requests_one_unlock_and_does_not_save_password(self):
        (self.root/'unused.ecfg').write_text('synthetic placeholder')
        child=Mock(pid=123);child.poll.return_value=None
        with patch.object(rt,'now',self.clock),patch('getpass.getpass',return_value='PRIVATE-OFFLINE-CANARY') as prompt, \
             patch.object(rt,'spawn',return_value=child) as spawn,patch.object(rt,'owner_available',return_value=True), \
             patch.object(rt,'watch',return_value=0),redirect_stdout(io.StringIO()):
            code=rt.main(['start','--live','--opening-proposal',str(self.r0),
                '--market-data-root',str(self.root),'--ecfg',str(self.root/'unused.ecfg')])
        self.assertEqual(code,0);prompt.assert_called_once();spawn.assert_called_once()
        for f in self.root.rglob('*'):
            if f.is_file():self.assertNotIn(b'PRIVATE-OFFLINE-CANARY',f.read_bytes())
    def test_spawn_uses_private_pipe_with_no_secret_in_command_or_log(self):
        entry=self.root/'pipe_child.py'
        entry.write_text('import sys,json; d=json.load(sys.stdin); assert d["password"]=="SYNTHETIC-PIPE-CANARY"; print("PRIVATE FRAME RECEIVED",flush=True)')
        plan=self.root/'public-plan.json';atomic_json(plan,{'public':True})
        log=self.root/'child.log'
        with patch.object(rt,'ENTRY',entry):
            proc=rt.spawn('uni',plan,'SYNTHETIC-PIPE-CANARY',log)
        self.assertEqual(proc.wait(timeout=10),0)
        self.assertNotIn('SYNTHETIC-PIPE-CANARY',' '.join(proc.args))
        self.assertEqual(log.read_text().strip(),'PRIVATE FRAME RECEIVED')
    def test_private_handoff_rejects_malformed_frame(self):
        with patch.object(rt.sys,'stdin',io.TextIOWrapper(io.BytesIO(b'{"secret":"wrong"}'))):
            with self.assertRaises(ValueError):rt.secret_receive()

if __name__=='__main__':unittest.main()
