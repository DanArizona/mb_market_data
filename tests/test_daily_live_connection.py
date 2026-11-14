"""End-to-end membership and read-only existing Overlay chart connection."""
from contextlib import closing,redirect_stdout
from datetime import timedelta
from pathlib import Path
import io,json,os,sqlite3,subprocess,sys,time,unittest
from unittest.mock import patch
from test_daily_membership import Fixture
from mb_market_data import daily_membership as dm,daily_live_overlay as ov,daily_runtime as rt
from mb_market_data.acquisition_health import atomic_json
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader
from mb_market_data.observation_overlay_view import build_observation_overlay_figure

class LiveConnectionTests(Fixture):
    def proof(self):
        self.plan['closes_at']=(self.open+timedelta(seconds=61)).isoformat()
        self.engine.plan=self.plan
        (self.root/'unused.ecfg').write_text('SYNTHETIC-NOT-CREDENTIALS')
        self.plan['ecfg']=str(self.root/'unused.ecfg')
        atomic_json(self.state/'plan.json',self.plan)
        atomic_json(self.state/'owner.json',{'pid':os.getpid(),'at':self.open.isoformat(),'active':True})
        entry=Path(__file__).with_name('daily_membership_process_fixture.py');children=[]
        for channel in ('uni','focus','hot'):
            proc=subprocess.Popen([sys.executable,'-B',str(entry),str(self.state/'plan.json'),channel],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            rt.secret_send(proc,'OFFLINE-NOT-A-REAL-PASSWORD');proc.stdin=None;children.append((channel,proc))
        pids={c:p.pid for c,p in children}
        try:
            for barrier in (12,24,36):
                until=time.monotonic()+20
                while not all((self.state/f'barrier-{barrier}-{c}.ready').exists() for c,p in children):
                    for c,p in children:
                        if p.poll() is not None:
                            out,err=p.communicate();self.fail((out+err).decode())
                    if time.monotonic()>until:self.fail('Collector barrier timeout')
                    time.sleep(.01)
                self.assertTrue(all(p.poll() is None for _,p in children))
                self.at=self.open+timedelta(seconds=barrier)
                if barrier==12:self.apply('INTC','focus','demote')
                elif barrier==24:self.apply('INTC','hot','promote')
                else:self.apply('ZZZ','hot','promote','nasdaq',self.at-timedelta(seconds=1))
                (self.state/f'barrier-{barrier}.release').write_text('continue synthetic clock')
            for channel,proc in children:
                out,err=proc.communicate(timeout=30)
                self.assertEqual(proc.returncode,0,(out+err).decode())
                self.assertNotIn('OFFLINE-NOT-A-REAL-PASSWORD',(out+err).decode())
            self.assertEqual(pids,{c:p.pid for c,p in children})
        finally:
            for _,proc in children:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
        return pids
    def test_same_three_workers_follow_manual_demotion_repromotion_and_nasdaq(self):
        pids=self.proof()
        with closing(sqlite3.connect(self.plan['journal'])) as c:
            counts=dict(c.execute('SELECT channel,count(*) FROM quote_acquisition GROUP BY channel'))
            self.assertEqual(counts,{'uni':3,'focus':4,'hot':24})
            hot=c.execute("SELECT a.scheduled_at_utc FROM quote_observation o JOIN quote_acquisition a USING(acquisition_id) WHERE o.symbol='INTC' AND a.channel='hot'").fetchall()
            secs=[(dm.timestamp(r[0])-self.open).total_seconds() for r in hot]
            self.assertTrue(all(not 15<=t<27 for t in secs));self.assertTrue(any(t>=28 for t in secs));self.assertEqual(len(secs),19)
            channels={r[0] for r in c.execute("SELECT DISTINCT a.channel FROM quote_observation o JOIN quote_acquisition a USING(acquisition_id) WHERE o.symbol='ZZZ'")}
            self.assertEqual(channels,{'uni','focus','hot'})
            self.assertEqual(c.execute('SELECT count(*) FROM poll_run').fetchone()[0],3)
        audit=audit_quote_journal(self.plan['journal']);self.assertTrue(audit.passed,audit)
        self.assertEqual(list(QuoteJournalReplayReader(self.plan['journal']).events()),list(QuoteJournalReplayReader(self.plan['journal']).events()))
        data,info=ov.overlay_snapshot(self.state,'INTC',at=self.open+timedelta(seconds=61))
        self.assertIsNone(data.cache);self.assertEqual(data.candles,());self.assertEqual(data.current_band.value,'hot')
        self.assertEqual([v.band.value for v in data.membership_transitions],['hot','focus','hot'])
        self.assertEqual(len(data.quote_points),26)
        self.assertEqual(info['membership_revision'],5)
        evidence=os.environ.get('MB_MEMBERSHIP_PROOF')
        if evidence:
            atomic_json(Path(evidence),{'result':'PASS','actual_child_pids':pids,'worker_restarts':0,
                'acquisitions':counts,'INTC_hot_observations':19,'nasdaq_symbol':'ZZZ',
                'nasdaq_symbol_collected_in':['uni','focus','hot'],'native_audit':audit.passed,
                'replays_equal':True,'overlay_membership_bands':['hot','focus','hot'],
                'overlay_quote_points':len(data.quote_points),'provider':'synthetic','clock':'parent-gated synthetic',
                'live_nasdaq_verified':False,'live_uptime_qualified':False})
    def test_pending_membership_not_shown_effective_early(self):
        self.apply();data,info=ov.overlay_snapshot(self.state,'INTC',at=self.at)
        self.assertEqual(data.current_band.value,'hot');self.assertTrue(info['pending_revisions'])
        self.step();data,info=ov.overlay_snapshot(self.state,'INTC',at=self.at)
        self.assertEqual(data.current_band.value,'focus')
    def test_read_only_snapshot_does_not_change_journal(self):
        before=Path(self.plan['journal']).read_bytes()
        data,info=ov.overlay_snapshot(self.state,'INTC',at=self.at)
        self.assertEqual(Path(self.plan['journal']).read_bytes(),before);self.assertTrue(info['read_only'])
    def test_missing_database_is_not_created(self):
        Path(self.plan['journal']).unlink()
        with self.assertRaises(sqlite3.OperationalError):ov.overlay_snapshot(self.state,'INTC',at=self.at)
        self.assertFalse(Path(self.plan['journal']).exists())
    def test_wrong_session_identity_is_rejected(self):
        plan=dict(self.plan,session_date='2026-11-11');atomic_json(self.state/'plan.json',plan)
        with self.assertRaises(ValueError):ov.overlay_snapshot(self.state,'INTC',at=self.at)
    def test_finished_health_not_labeled_live(self):
        atomic_json(self.state/'status.json',{'at':self.at.isoformat(),'final_status':True,'phase':'FINISHED'})
        _,info=ov.overlay_snapshot(self.state,'INTC',at=self.at+timedelta(days=1))
        self.assertTrue(info['final']);self.assertFalse(info['health_stale'])
    def test_old_active_status_is_explicitly_stale(self):
        atomic_json(self.state/'status.json',{'at':(self.at-timedelta(minutes=1)).isoformat(),'phase':'COLLECTING'})
        _,info=ov.overlay_snapshot(self.state,'INTC',at=self.at)
        self.assertTrue(info['health_stale'])
    def test_no_sdk_or_credential_import_in_live_reader(self):
        source=Path(ov.__file__).read_text()
        self.assertNotIn('mb_tools',source);self.assertNotIn('make_client',source)
        self.assertNotIn('fetch_trade_halts',source)
    def test_halt_information_is_visible_only_after_its_observation(self):
        atomic_json(self.state/'nasdaq/events.json',{'INTC@episode':{'record':{'symbol':'INTC','halt_date':'11/10/2026','halt_time':'09:35:00','reason_code':'LUDP',
             'resumption_date':'11/10/2026','resumption_trade_time':'09:38:00'},'first_observed_at':self.at.isoformat(),'last_observed_at':self.at.isoformat()}})
        data,info=ov.overlay_snapshot(self.state,'INTC',at=self.at-timedelta(seconds=1))
        self.assertEqual(data.halt_intervals,())
        data,info=ov.overlay_snapshot(self.state,'INTC',at=self.at)
        self.assertEqual(len(data.halt_intervals),1);self.assertEqual(len(info['observed_halts']),1)
    def test_new_external_member_appears_in_symbol_choices(self):
        self.apply('NEW','uni','promote');self.step()
        _,info=ov.overlay_snapshot(self.state,'NEW',at=self.at)
        self.assertEqual(info['membership'],'uni');self.assertIn('NEW',info['members']['uni'])
    def test_root_for_display_does_not_create_membership_service(self):
        ov.overlay_snapshot(self.state,'INTC',at=self.at)
        self.assertFalse((self.state/'membership/status.json').exists())

if __name__=='__main__':unittest.main(verbosity=2)
