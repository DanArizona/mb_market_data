from __future__ import annotations
import sqlite3,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from contextlib import closing
from unittest.mock import patch
from mb_market_data.sampling_incremental import INCREMENTAL_MARKER,IncrementalSamplingPoller
from mb_market_data.sampling_acquisition import MARKER,AcquisitionSettings,ExplicitPollSlot
from mb_market_data.sampling_membership import SamplingHierarchyRevision,SamplingChannel
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.watchlist_polling import PollWindow
from mb_market_data.hot_provider_trial import SyntheticClient,replay_digest


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.base=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=14,minute=0,second=0,microsecond=0)
        self.clock=[self.base];clock=self.clock
        class Logical(datetime):
            @classmethod
            def now(cls,tz=None):return clock[0].astimezone(tz or timezone.utc)
        p=patch('mb_market_data.schwab_quotes.datetime',Logical);p.start();self.addCleanup(p.stop)
        self.j=QuoteObservationStore(self.root/'quotes.sqlite3',session_date=self.base.date(),schema_version=2);self.j.initialize()
        self.r0=SamplingHierarchyRevision(session_date=self.base.date(),revision=0,effective_at=self.base,uni_symbols=('AAPL','MSFT','SPY'),focus_symbols=('MSFT',),hot_symbols=('MSFT',),source='incremental_test',metadata={MARKER:True,INCREMENTAL_MARKER:True})
        self.j.record_membership_revision(self.r0)
        self.settings=AcquisitionSettings(SamplingChannel.HOT,tuple(range(0,60,2)),PollWindow(self.base,self.base+timedelta(seconds=120)),400,'all','synthetic_incremental',.75,.05)
        self.client=SyntheticClient();self.p=self.open()
    def open(self):
        p=IncrementalSamplingPoller(self.j,settings=self.settings,client=self.client,clock=lambda:self.clock[0]);p.initialize();return p
    def sample(self,n=0,**kwargs):
        self.clock[0]=self.base+timedelta(seconds=n)
        return self.p.sample(ExplicitPollSlot(SamplingChannel.HOT,self.clock[0],self.settings.seconds),**kwargs)
    def test_normal_acquisition_full_replay_and_restart(self):
        self.assertEqual(self.sample()['status'],'acquired');self.assertEqual(self.sample(2)['status'],'acquired')
        before=replay_digest(self.j.database_path);self.p=self.open()
        self.assertEqual(replay_digest(self.j.database_path),before)
        self.assertEqual(self.sample(2)['status'],'already_finished');self.assertEqual(self.client.count,2)
    def test_no_full_reconstruction_in_normal_cycle(self):
        full=self.p.full_reconstructions
        for n in range(0,10,2):self.sample(n)
        self.assertEqual(self.p.full_reconstructions,full)
        self.assertEqual(self.p.incremental_events,14) # final ACK is consumed by next read
        self.p.snapshot();self.assertEqual(self.p.full_reconstructions,full+1)
    def test_immutable_update_delete_and_replace_are_blocked(self):
        self.sample()
        for sql in ('UPDATE quote_observation SET quote_last_price=999','DELETE FROM quote_acquisition',
                    'INSERT OR REPLACE INTO quote_observation SELECT * FROM quote_observation'):
            with self.subTest(sql=sql),closing(sqlite3.connect(self.j.database_path)) as c,self.assertRaises(sqlite3.IntegrityError):c.execute(sql)
        self.assertEqual(self.sample(2)['status'],'acquired')
    def test_guard_removed_even_restored_is_detected_before_api(self):
        self.sample()
        with closing(sqlite3.connect(self.j.database_path)) as c,c:
            sql=c.execute("SELECT sql FROM sqlite_master WHERE name='ig_quote_observation_update'").fetchone()[0]
            c.execute('DROP TRIGGER ig_quote_observation_update');c.execute(sql)
        with self.assertRaisesRegex(ValueError,'guard changed'):self.sample(2)
        self.assertEqual(self.client.count,1)
    def test_missing_guard_is_not_silently_repaired_on_restart(self):
        with closing(sqlite3.connect(self.j.database_path)) as c,c:c.execute('DROP TRIGGER ig_quote_observation_update')
        with self.assertRaisesRegex(ValueError,'guard definitions'):self.open()
    def test_sidecar_mutation_blocked_and_schema_change_detected(self):
        self.sample()
        with closing(sqlite3.connect(self.p.path)) as c,c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE acquisition_event SET payload='{}'")
            c.execute('CREATE TABLE unexpected(x)')
        with self.assertRaisesRegex(ValueError,'guard changed'):self.sample(2)
        self.assertEqual(self.client.count,1)
    def test_ordinal_guard_prevents_appending_extra_old_observation(self):
        self.sample()
        with closing(sqlite3.connect(self.j.database_path)) as c:
            columns=[r[1] for r in c.execute('PRAGMA table_info(quote_observation)')]
            row=list(c.execute('SELECT * FROM quote_observation').fetchone());row[columns.index('symbol')]='AAPL';row[columns.index('ordinal')]=1
            with self.assertRaisesRegex(sqlite3.IntegrityError,'ordinal/parent'):c.execute('INSERT INTO quote_observation VALUES('+','.join('?' for _ in row)+')',row)
    def test_unknown_channel_append_is_detected_before_api(self):
        self.sample()
        with closing(sqlite3.connect(self.j.database_path)) as c,c:
            columns=[r[1] for r in c.execute('PRAGMA table_info(quote_acquisition)')];row=list(c.execute('SELECT * FROM quote_acquisition').fetchone())
            row[columns.index('acquisition_id')]='foreign';row[columns.index('slot_id')]='foreign'
            c.execute('INSERT INTO quote_acquisition VALUES('+','.join('?' for _ in row)+')',row)
        with self.assertRaisesRegex(ValueError,'Unexpected journal'):self.sample(2)
        self.assertEqual(self.client.count,1)
    def test_future_membership_keeps_slot_time_binding_and_retry_is_idempotent(self):
        self.sample()
        r=replace(self.r0,revision=1,effective_at=self.base+timedelta(seconds=4),hot_symbols=(),published_at=None)
        self.j.record_membership_revision(r);self.j.record_membership_revision(r)
        self.assertEqual(self.sample(2)['symbols'],['MSFT'])
        self.assertEqual(self.sample(4)['status'],'empty_membership')
    def test_saved_response_and_committed_response_recover_without_repeat(self):
        for stage in ('after_assembled','after_journal_commit'):
            n=0 if stage=='after_assembled' else 2
            def fault(value):
                if value==stage:raise RuntimeError('injected')
            with self.assertRaises(RuntimeError):self.sample(n,fault_hook=fault)
            count=self.client.count;self.p=self.open();self.p.recover();self.assertEqual(self.client.count,count)
        self.p.snapshot();self.assertEqual(replay_digest(self.j.database_path)['acquisitions'],2)
    def test_unknown_dispatch_is_not_reissued(self):
        def fault(value):
            if value=='after_dispatch':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):self.sample(fault_hook=fault)
        self.p=self.open();self.assertEqual(self.p.recover()['status'],'unresolved_dispatch');self.assertEqual(self.client.count,0)
    def test_other_channel_can_append_without_invalidating_hot(self):
        other=IncrementalSamplingPoller(self.j,settings=replace(self.settings,channel=SamplingChannel.UNI),client=SyntheticClient(),clock=lambda:self.clock[0]);other.initialize()
        other.sample(ExplicitPollSlot(SamplingChannel.UNI,self.base,self.settings.seconds))
        self.assertEqual(self.sample()['status'],'acquired');self.p.snapshot()

    def test_guarded_store_can_be_reopened_by_standard_initializer(self):
        self.sample();self.j.initialize();self.assertEqual(self.sample(2)['status'],'acquired')
    def test_two_instances_adopt_new_verified_events_without_duplicate_request(self):
        other=self.open();other.sample(ExplicitPollSlot(SamplingChannel.HOT,self.base,self.settings.seconds))
        self.assertEqual(self.sample()['status'],'already_finished');self.assertEqual(self.client.count,1)
    def test_write_failure_recovers_saved_result_without_repeat(self):
        with patch.object(self.j,'record_acquisition',side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):self.sample()
        self.p.recover();self.assertEqual(self.client.count,1);self.assertEqual(replay_digest(self.j.database_path)['acquisitions'],1)

    def test_checkpoint_drops_old_payloads_but_full_reconstruction_keeps_them(self):
        self.sample();self.sample(2)
        self.assertTrue(all('result' not in e for e in self.p._checkpoint['events']))
        snapshot=self.p.snapshot()
        self.assertEqual(sum('result' in e for e in snapshot['events']),2)
        self.assertTrue(all('result' not in e for e in self.p._checkpoint['events']))
    def test_response_not_saved_is_a_gap_and_never_reissued(self):
        def fault(stage):
            if stage=='after_response':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):self.sample(fault_hook=fault)
        self.p=self.open();self.assertEqual(self.p.recover()['status'],'unresolved_dispatch')
        self.assertEqual(self.client.count,1);self.assertEqual(replay_digest(self.j.database_path)['acquisitions'],0)
    def test_750ms_gate_diagnostics_and_no_late_api(self):
        slot=ExplicitPollSlot(SamplingChannel.HOT,self.base,self.settings.seconds)
        self.clock[0]=self.base+timedelta(seconds=.751)
        value=self.p.sample(slot)
        self.assertEqual(value['status'],'missed');self.assertEqual(value['gate'],'before_dispatch_deadline')
        self.assertAlmostEqual(value['gate_lateness_seconds'],.751);self.assertEqual(self.client.count,0)
        self.clock[0]=self.base+timedelta(seconds=2)
        def delay(stage):
            if stage=='after_dispatch':self.clock[0]+=timedelta(seconds=.751)
        value=self.p.sample(ExplicitPollSlot(SamplingChannel.HOT,self.base+timedelta(seconds=2),self.settings.seconds),fault_hook=delay)
        self.assertEqual(value['gate'],'before_api_deadline');self.assertEqual(self.client.count,0)

    def test_replaced_sidecar_file_is_detected_before_another_api(self):
        self.sample()
        replacement=self.p.path.with_suffix('.replacement')
        replacement.write_bytes(self.p.path.read_bytes());replacement.replace(self.p.path)
        with self.assertRaisesRegex(ValueError,'file_identity'):self.sample(2)
        self.assertEqual(self.client.count,1)
