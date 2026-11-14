"""Uptime correction: source-matched fault injection; no SDK, real vault or network."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mb_market_data import daily_runtime as rt
from mb_market_data.acquisition_health import atomic_json
from mb_market_data.daily_worker_liveness import OwnerHeartbeatGuard, WorkerControl, MAX_HEARTBEAT_AGE
from mb_market_data.daily_flow import ensure_opening
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.opening_hierarchy import load_opening_proposal
import test_daily_runtime as runtime_fixture


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.origin=datetime(2026,11,6,15,0,tzinfo=timezone.utc)
        self.seconds=0.0;self.record={'pid':123,'at':self.origin.isoformat(),'active':True}
        self.events=[];self.mode='healthy';self.failures_left=0;self.reads=0
        def reader(path):
            self.reads+=1
            if self.mode=='unreadable' or self.failures_left>0:
                self.failures_left=max(0,self.failures_left-1)
                raise PermissionError(13,'PRIVATE-CANARY-DO-NOT-LOG')
            if self.mode=='bad-json':raise json.JSONDecodeError('synthetic','',0)
            return dict(self.record)
        self.reader=reader
        self.guard=OwnerHeartbeatGuard(self.root,pid=123,reader=reader,
            clock=lambda:self.origin+timedelta(seconds=self.seconds),
            monotonic=lambda:self.seconds,sleep=self.advance,emit=self.events.append)
    def advance(self,seconds):self.seconds+=seconds
    def test_single_read_failure_retries_without_stopping(self):
        self.assertTrue(self.guard.available());self.failures_left=1
        self.assertTrue(self.guard.available());self.assertIsNone(self.guard.reason)
        self.assertEqual(self.guard.read_errors,1)
        self.assertEqual([e['event']for e in self.events],['heartbeat_read_retry','heartbeat_read_recovered'])
    def test_short_read_burst_uses_only_last_verified_freshness(self):
        self.assertTrue(self.guard.available());self.advance(1);self.mode='unreadable'
        self.assertTrue(self.guard.available());self.assertEqual(self.guard.read_errors,4)
        self.assertEqual(self.guard.last_at,self.origin)
        self.mode='healthy';self.record['at']=(self.origin+timedelta(seconds=2)).isoformat();self.seconds=2
        self.assertTrue(self.guard.available());self.assertIsNone(self.guard.failure_since)
    def test_continuously_unreadable_expires_at_original_30_seconds(self):
        self.assertTrue(self.guard.available());self.mode='unreadable';self.seconds=29
        self.assertTrue(self.guard.available());self.seconds=30
        self.assertFalse(self.guard.available());self.assertEqual(self.guard.reason,'owner_heartbeat_expired')
        self.assertEqual(self.guard.last_at,self.origin)
    def test_read_error_near_expiry_cannot_extend_last_heartbeat(self):
        self.assertTrue(self.guard.available());self.mode='unreadable';self.seconds=29.98
        self.assertFalse(self.guard.available());self.assertLess(self.seconds,30.1)
    def test_no_prior_verified_heartbeat_does_not_authorize_collection(self):
        self.mode='unreadable'
        self.assertFalse(self.guard.available());self.assertEqual(self.reads,4)
        self.assertEqual(self.guard.reason,'owner_heartbeat_unreadable')
    def test_initial_transient_error_can_recover_within_bounded_read(self):
        self.failures_left=2
        self.assertTrue(self.guard.available());self.assertEqual(self.reads,3)
        self.assertLess(self.seconds,.08)
    def test_repeated_unchanged_timestamp_does_not_renew_freshness(self):
        for t in (0,10,20,29):self.seconds=t;self.assertTrue(self.guard.available())
        self.seconds=30;self.assertFalse(self.guard.available())
    def test_forward_progress_refreshes_owner_receipt(self):
        self.assertTrue(self.guard.available());self.seconds=25
        self.record['at']=(self.origin+timedelta(seconds=25)).isoformat()
        self.assertTrue(self.guard.available());self.seconds=45
        self.assertTrue(self.guard.available());self.seconds=55
        self.assertFalse(self.guard.available())
    def test_backwards_wall_clock_does_not_extend_old_heartbeat(self):
        self.assertTrue(self.guard.available());self.seconds=31
        self.guard.clock=lambda:self.origin
        self.assertFalse(self.guard.available())
    def test_owner_replacement_stops_even_while_previous_heartbeat_fresh(self):
        self.assertTrue(self.guard.available());self.record['pid']=124
        self.assertFalse(self.guard.available());self.assertEqual(self.guard.reason,'owner_replaced')
    def test_explicit_inactive_owner_stops_without_grace(self):
        self.assertTrue(self.guard.available());self.record['active']=False
        self.assertFalse(self.guard.available());self.assertEqual(self.guard.reason,'owner_inactive')
    def test_future_heartbeat_is_not_indefinite_authority(self):
        self.record['at']=(self.origin+timedelta(hours=1)).isoformat()
        self.assertFalse(self.guard.available());self.assertEqual(self.guard.reason,'owner_heartbeat_in_future')
    def test_invalid_heartbeat_fields_are_named_not_uncaught(self):
        for changes in ({'pid':True},{'active':'yes'},{'at':'bad'}, {'at':'2026-11-06T15:00:00'}):
            with self.subTest(changes=changes):
                self.guard.reason=None;self.record={'pid':123,'at':self.origin.isoformat(),'active':True,**changes}
                self.assertFalse(self.guard.available());self.assertEqual(self.guard.reason,'owner_heartbeat_invalid')
    def test_unreadable_json_uses_bounded_prior_receipt_but_never_indefinitely(self):
        self.assertTrue(self.guard.available());self.mode='bad-json'
        self.assertTrue(self.guard.available());self.seconds=30
        self.assertFalse(self.guard.available())
    def test_shutdown_is_latched_not_undone_by_later_read(self):
        self.record['active']=False;self.assertFalse(self.guard.available())
        count=self.reads;self.record['active']=True
        self.assertFalse(self.guard.available());self.assertEqual(self.reads,count)
    def test_failed_diagnostic_sink_does_not_turn_read_recovery_into_stop(self):
        self.guard.emit=Mock(side_effect=PermissionError('diagnostic unavailable'))
        self.failures_left=1;self.assertTrue(self.guard.available())
        self.assertIsNone(self.guard.reason)
    def test_diagnostics_exclude_exception_message(self):
        self.failures_left=1;self.assertTrue(self.guard.available())
        self.assertNotIn('PRIVATE-CANARY',json.dumps(self.events))
    def test_explicit_stop_is_immediate_without_heartbeat_read(self):
        (self.root/'stop.request').write_text('stop')
        control=WorkerControl(self.root,owner_pid=123,channel='uni',generation=0,
            reader=Mock(side_effect=AssertionError('must not read')),clock=lambda:self.origin,sleep=self.advance)
        with redirect_stdout(io.StringIO()):self.assertTrue(control())
        self.assertEqual(control.reason,'collection_stop_requested')
        event=json.loads(control.events_path.read_text().splitlines()[-1])
        self.assertEqual(event['reason'],'collection_stop_requested')
    def test_exit_wins_and_postclose_ignores_collection_stop(self):
        atomic_json(self.root/'owner.json',self.record)
        (self.root/'stop.request').write_text('stop collection')
        control=WorkerControl(self.root,owner_pid=123,channel='postclose',generation=0,
            reader=rt.read_json,clock=lambda:self.origin,honor_collection_stop=False)
        self.assertFalse(control());(self.root/'exit.request').write_text('exit')
        with redirect_stdout(io.StringIO()):self.assertTrue(control())
        self.assertEqual(control.reason,'operator_exit_session')
    def test_real_concurrent_heartbeat_writes_and_reads_do_not_stop_worker(self):
        # Both actual file operations run here. Windows sharing conflicts, if
        # encountered, are handled by the real bounded reader and atomic writer.
        atomic_json(self.root/'owner.json',{'pid':123,'at':datetime.now(timezone.utc).isoformat(),'active':True})
        done=threading.Event();errors=[];counts=[0]
        def writer():
            while not done.wait(.005):
                try:
                    atomic_json(self.root/'owner.json',{'pid':123,'at':datetime.now(timezone.utc).isoformat(),'active':True})
                    counts[0]+=1
                except OSError as error:errors.append(type(error).__name__)
        thread=threading.Thread(target=writer);thread.start()
        guard=OwnerHeartbeatGuard(self.root,pid=123,reader=rt.read_json,
            clock=lambda:datetime.now(timezone.utc))
        try:
            until=time.monotonic()+1.0;reads=0
            while time.monotonic()<until:
                self.assertTrue(guard.available(),guard.reason);reads+=1;time.sleep(.001)
        finally:done.set();thread.join(timeout=3)
        self.assertFalse(thread.is_alive());self.assertGreater(counts[0],0);self.assertGreater(reads,0)


class StatusTests(unittest.TestCase):
    def status(self):
        return {'session_date':'2026-11-06','phase':'COLLECTING','at':'2026-11-06T14:00:00-05:00','pid':100,
            'selection_status':'READY; r1 PUBLISHED','health':{'journal_read':'OK','channels':{
                c:{'state':'RUNNING','reason':'recent progress','missing_slot_count':0}for c in ('uni','focus')}},
            'workers':{c:{'pid':101+i,'exit_code':None,'api_state':'AVAILABLE'}for i,c in enumerate(('uni','focus'))}}
    def test_stopped_workers_do_not_claim_collecting_or_live_api(self):
        v=self.status()
        for row in v['workers'].values():row['exit_code']=0
        text=rt.readable_status(v)
        self.assertIn('| COLLECTION_STOPPED',text);self.assertNotIn('| API AVAILABLE',text)
        self.assertIn('last saved API status (not live): AVAILABLE',text)
    def test_normal_final_slots_are_not_reported_as_unexpected_early_stops(self):
        v=self.status()
        for c,row in v['workers'].items():
            row['exit_code']=0;v['health']['channels'][c]['state']='SCHEDULE_COMPLETE'
        self.assertEqual(rt.collection_phase(v),'COLLECTION_COMPLETE')
        v['health']['channels']['uni']['missing_slot_count']=217
        self.assertEqual(rt.collection_phase(v),'COLLECTION_COMPLETE_WITH_GAPS')
    def test_one_stopped_worker_labels_whole_session_degraded(self):
        v=self.status();v['workers']['uni'].update(exit_code=3,exit_reason='owner_heartbeat_expired')
        self.assertEqual(rt.collection_phase(v),'COLLECTING_DEGRADED')
        self.assertIn('EXIT REASON: owner_heartbeat_expired',rt.readable_status(v))
    def test_recorded_gaps_remain_visible_while_collection_continues(self):
        v=self.status();v['health']['channels']['uni']['missing_slot_count']=217
        self.assertEqual(rt.collection_phase(v),'COLLECTING_DEGRADED')
    def test_good_worker_and_journal_progress_is_collecting(self):
        self.assertEqual(rt.collection_phase(self.status()),'COLLECTING')
    def test_unavailable_journal_is_not_a_collecting_claim(self):
        v=self.status();v['health']['journal_read']='UNAVAILABLE'
        self.assertEqual(rt.collection_phase(v),'COLLECTION_UNVERIFIED')
    def test_authorization_pause_is_degraded_not_healthy(self):
        v=self.status();v['workers']['uni']['api_state']='AUTHORIZATION_REQUIRED'
        self.assertEqual(rt.collection_phase(v),'COLLECTING_DEGRADED')
    def test_completed_session_age_is_historical_not_stale_running(self):
        v=self.status();v['phase']='FINISHED';v['final_status']=True
        for row in v['workers'].values():row['exit_code']=0
        message=rt.status_age_message(v,2884)
        self.assertIn('FINAL REPORT',message);self.assertNotIn('WARNING',message)
    def test_old_completed_report_with_exited_workers_is_final(self):
        v=self.status();v['phase']='FINISHED'
        for row in v['workers'].values():row['exit_code']=0
        self.assertIn('FINAL REPORT',rt.status_age_message(v,2884))
    def test_unfinished_cleanup_still_has_stale_warning(self):
        v=self.status();v['phase']='FINISHED';v['final_status']=True
        self.assertIn('WARNING',rt.status_age_message(v,31))
    def test_live_status_staleness_still_warns(self):
        self.assertIn('WARNING',rt.status_age_message(self.status(),31))
    def test_status_write_failure_does_not_mask_valid_api_response(self):
        c=Mock();response=Mock(status_code=200);c.quotes.return_value=response
        wrapper=rt.BackoffClient(c,state_path=Path('/unused/status.json'))
        with patch.object(rt,'atomic_json',side_effect=PermissionError('synthetic')),redirect_stdout(io.StringIO()):
            self.assertIs(wrapper.quotes(['ABC'],fields='all'),response)
        self.assertEqual(c.quotes.call_count,1)
    def test_failed_api_status_write_does_not_remove_auth_latch(self):
        c=Mock();c.quotes.return_value=Mock(status_code=401)
        w=rt.BackoffClient(c,state_path=Path('/unused/status.json'))
        with patch.object(rt,'atomic_json',side_effect=PermissionError('synthetic')),redirect_stdout(io.StringIO()):
            w.quotes(['ABC'],fields='all')
            with self.assertRaises(RuntimeError):w.quotes(['ABC'],fields='all')
        self.assertEqual(c.quotes.call_count,1)


class OwnedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.f=runtime_fixture.RuntimeTests('test_real_child_probes_private_handoff_and_interleaved_native_writes')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;ensure_opening(f.plan);f.state.mkdir(parents=True)
        f.plan['closes_at']=f.clock().replace(hour=9,minute=30,second=31).isoformat()
        (f.root/'unused.ecfg').write_text('SYNTHETIC; NOT A VAULT')
        atomic_json(f.state/'owner.json',{'pid':os.getpid(),'at':f.plan['opens_at'],'active':True})
        r0=load_opening_proposal(f.r0)
        store=QuoteObservationStore(f.plan['journal'],session_date=f.day,schema_version=2)
        store.record_membership_revision(SamplingHierarchyRevision(session_date=f.day,revision=1,
            effective_at=r0.effective_at,uni_symbols=r0.uni_symbols,focus_symbols=('ABC',),hot_symbols=(),source='synthetic-uptime'))
        self.plan_path=f.root/'process-plan.json';atomic_json(self.plan_path,f.plan)
    def launch(self,mode,channel='uni'):
        f=self.f
        entry=Path(__file__).with_name('daily_flow_process_fixture.py')
        proc=subprocess.Popen([sys.executable,'-B',str(entry),str(self.plan_path),channel,mode],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            rt.secret_send(proc,'OFFLINE-CANARY-NOT-A-REAL-PASSWORD');proc.stdin=None
            out,err=proc.communicate(timeout=25)
        except BaseException:
            proc.kill();proc.communicate();raise
        text=(out+err).decode()
        self.assertNotIn('OFFLINE-CANARY-NOT-A-REAL-PASSWORD',text)
        receipt=rt.read_json(f.state/'workers'/f'{channel}-0'/'exit.json')
        with closing(sqlite3.connect(f.plan['journal'])) as c:
            acquisitions=c.execute('SELECT count(*) FROM quote_acquisition WHERE channel=?',(channel,)).fetchone()[0]
        for file in f.root.rglob('*'):
            if file.is_file():self.assertNotIn(b'OFFLINE-CANARY-NOT-A-REAL-PASSWORD',file.read_bytes())
        return proc.returncode,text,receipt,acquisitions
    def test_actual_child_uni_survives_read_failure_burst_then_saves_final_slot(self):
        code,text,receipt,count=self.launch('read-glitch')
        self.assertEqual(code,0,text);self.assertEqual(count,2,text)
        self.assertEqual(receipt['reason'],'schedule_complete');self.assertTrue(receipt['scheduled_completion'])
        self.assertEqual(receipt['heartbeat_read_errors'],4)
        self.assertIn('heartbeat_read_retry',text);self.assertIn('heartbeat_read_recovered',text)
        self.assertNotIn('Stopped by user',text)
    def test_actual_child_focus_survives_read_failure_burst(self):
        code,text,receipt,count=self.launch('read-glitch','focus')
        self.assertEqual(code,0,text);self.assertEqual(count,2)
        self.assertEqual(receipt['heartbeat_read_errors'],4);self.assertEqual(receipt['reason'],'schedule_complete')
    def test_actual_child_expired_owner_stops_with_nonzero_code_and_specific_receipt(self):
        code,text,receipt,count=self.launch('expired')
        self.assertEqual(code,3,text);self.assertEqual(count,1)
        self.assertEqual(receipt['reason'],'owner_heartbeat_expired')
        self.assertIn('Stopped by daily control: owner_heartbeat_expired',text)
        self.assertNotIn('Stopped by user',text)
    def test_actual_child_replaced_owner_stops_before_next_acquisition(self):
        code,text,receipt,count=self.launch('replaced')
        self.assertEqual(code,3,text);self.assertEqual(count,1)
        self.assertEqual(receipt['reason'],'owner_replaced')
    def test_actual_child_explicit_stop_while_waiting_is_not_keyboard_interrupt(self):
        code,text,receipt,count=self.launch('operator-stop')
        self.assertEqual(code,0,text);self.assertEqual(count,1)
        self.assertEqual(receipt['reason'],'collection_stop_requested')
        self.assertNotIn('Interrupted by console',text);self.assertNotIn('Stopped by user',text)


class ControllerUptimeTests(unittest.TestCase):
    def setUp(self):
        self.f=runtime_fixture.RuntimeTests('test_controller_publishes_r1_before_focus_worker_launch')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_temporary_controller_status_file_conflict_does_not_stop_workers_or_handoff(self):
        original=rt.atomic_json;failures=[]
        def write(path,value):
            if Path(path).name=='status.json' and not failures:
                failures.append('injected');raise PermissionError(13,'synthetic cosmetic failure')
            return original(path,value)
        with patch.object(rt,'atomic_json',side_effect=write),redirect_stdout(io.StringIO()):
            launched,post,result=self.f.exercise_controller()
        self.assertEqual(launched,['uni','selection','focus','postclose'])
        self.assertTrue(post);self.assertTrue(result['final_status'])
        self.assertEqual(result['next_session_status']['status'],'READY')
        self.assertEqual(result['status_write_errors'],1)
    def test_temporary_owner_write_failure_is_visible_but_does_not_cancel_handoff(self):
        original=rt.atomic_json;failures=[]
        def write(path,value):
            if Path(path).name=='owner.json' and not failures:
                failures.append('injected');raise PermissionError(13,'synthetic owner file conflict')
            return original(path,value)
        with patch.object(rt,'atomic_json',side_effect=write),redirect_stdout(io.StringIO()):
            launched,post,result=self.f.exercise_controller()
        self.assertTrue(post);self.assertEqual(result['status_write_errors'],1)
        self.assertEqual(rt.read_json(self.f.state/'owner.json')['active'],False)


if __name__=='__main__':unittest.main()
