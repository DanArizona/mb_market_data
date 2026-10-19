"""Bounded provider harness: transport guards, roster provenance and durable budgets."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout,closing
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'experiments/combined_schwab_20261019'))
import provider_load as p
import run_review as entry


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.now=datetime(2026,10,19,14,tzinfo=timezone.utc)
        self.config=p.plan(300)
        self.config.update(start=p.text(self.now),end=p.text(self.now+timedelta(seconds=300)))
        p.initialize_budget(self.folder,self.config)

    def roster_files(self,**overrides):
        uni=[f'S{n:04d}' for n in range(554)]
        r0=dict(session_date='2026-10-19',revision=0,effective_at='2026-10-19T09:30:00-04:00',
            uni_symbols=uni,focus_symbols=[],hot_symbols=[],source='synthetic-test')
        r1={**r0,'revision':1,'focus_symbols':uni[:40]}
        r1.update(overrides)
        paths=self.folder/'r0.json',self.folder/'r1.json'
        for path,value in zip(paths,(r0,r1)):p.write_json(path,value)
        return paths

    def test_live_plan_has_bounded_native_grids_and_no_injected_latency(self):
        roster=p.load_roster(*self.roster_files(),now=self.now)
        cfg=p.plan(300,roster,'schwab')
        self.assertEqual(cfg['quote_call_budgets'],dict(uni=20,focus=20,hot=150))
        self.assertEqual(cfg['total_quote_call_budget'],190)
        self.assertEqual(cfg['grids']['hot'],list(range(0,60,2)))
        self.assertEqual(cfg['dispatch_deadline_seconds'],.75)
        self.assertEqual(cfg['overlap_delay_seconds'],0.)
        self.assertEqual(cfg['request_timeout_seconds'],3)

    def test_source_and_duration_cannot_silently_expand(self):
        for source,duration in [('auto',300),('schwab',60),('schwab',300),('synthetic_only',600)]:
            with self.subTest(source=source,duration=duration),self.assertRaises((ValueError,RuntimeError)):
                p.plan(duration,source=source)

    def test_roster_files_are_read_only_and_hash_bound(self):
        paths=self.roster_files();before=[v.read_bytes() for v in paths]
        roster=p.load_roster(*paths,now=self.now)
        self.assertEqual(roster['outside_symbol'],'MSFT')
        self.assertNotIn(roster['algorithm_symbol'],roster['opening_focus'])
        self.assertEqual(roster['input_sha256']['focus'],hashlib.sha256(before[1]).hexdigest())
        self.assertEqual([v.read_bytes() for v in paths],before)

    def test_wrong_session_mismatched_uni_and_wrong_revision_refused(self):
        for changes in ({'session_date':'2026-10-20'},{'uni_symbols':['MSFT']},{'revision':2},{'hot_symbols':['S0000']},
                        {'effective_at':'2026-10-19T09:30:00.000001-04:00'}):
            with self.subTest(changes=changes),self.assertRaises((RuntimeError,ValueError)):
                p.load_roster(*self.roster_files(**changes),now=self.now)

    def test_input_must_be_proposal_json_and_small(self):
        path=self.folder/'database.sqlite3';path.write_text('{}')
        with self.assertRaises(RuntimeError):p.load_roster(path,path,now=self.now)

    def test_regular_session_gate_before_live_launch(self):
        p.check_live_window(self.now)
        for invalid in (self.now.replace(hour=12),self.now.replace(hour=20),self.now+timedelta(days=5)):
            with self.subTest(invalid=invalid),self.assertRaises(RuntimeError):p.check_live_window(invalid)

    def test_global_and_channel_budgets_are_enforced_durably(self):
        self.config['total_quote_call_budget']=3;self.config['quote_call_budgets']['hot']=2
        for channel in ('hot','hot','uni'):p.reserve_call(self.folder,channel,'slot',self.config,lambda:self.now)
        with self.assertRaises(RuntimeError):p.reserve_call(self.folder,'hot','slot',self.config,lambda:self.now)
        with self.assertRaises(RuntimeError):p.reserve_call(self.folder,'focus','slot',self.config,lambda:self.now)
        with closing(sqlite3.connect(self.folder/'call_budget.sqlite3')) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM call_permit').fetchone()[0],3)

    def test_parallel_reservations_never_exceed_shared_budget(self):
        self.config['total_quote_call_budget']=5
        def reserve(n):
            try:return p.reserve_call(self.folder,'hot',str(n),self.config,lambda:self.now)
            except RuntimeError:return None
        with ThreadPoolExecutor(max_workers=8) as pool:values=list(pool.map(reserve,range(16)))
        self.assertEqual(len([v for v in values if v is not None]),5)

    def test_committed_call_reservations_cannot_be_deleted_or_updated(self):
        p.reserve_call(self.folder,'hot','slot',self.config,lambda:self.now)
        with closing(sqlite3.connect(self.folder/'call_budget.sqlite3')) as c:
            for query in ('DELETE FROM call_permit',"UPDATE call_permit SET channel='uni'"):
                with self.assertRaises(sqlite3.IntegrityError):c.execute(query)

    def test_stop_and_deadline_block_before_reserving_or_querying(self):
        for stopped in (False,True):
            if stopped:(self.folder/'STOP').touch()
            with self.assertRaises(RuntimeError):p.reserve_call(self.folder,'hot','slot',self.config,
                lambda:self.now if stopped else self.now+timedelta(seconds=300))

    def client(self,response=None):
        config={**self.config,'source':'schwab'}
        transport=Mock()
        transport.quotes.return_value=response or Mock(status_code=200,ok=True,json=Mock(return_value={}))
        client=p.CallClient(self.folder,'hot',config,lambda:self.now,Event(),transport)
        client.slot=Mock(slot_id='hot:slot',scheduled_at=self.now)
        return client,transport

    def test_auth_and_rate_limit_stop_every_experimental_channel(self):
        for code in (401,403,429):
            with self.subTest(code=code):
                (self.folder/'STOP').unlink(missing_ok=True)
                response=Mock(status_code=code,ok=False,text='private-provider-body')
                client,transport=self.client(response)
                self.assertEqual(client.quotes(['S0000'],fields='all').json(),{})
                self.assertTrue((self.folder/'STOP').exists())
                with self.assertRaises(RuntimeError):client.quotes(['S0000'],fields='all')
                self.assertEqual(transport.quotes.call_count,1)
                response.json.assert_not_called()

    def test_transport_exception_redaction_and_durable_outcome(self):
        client,transport=self.client();transport.quotes.side_effect=OSError('private-password')
        with self.assertRaises(RuntimeError) as caught:client.quotes(['S0000'],fields='all')
        self.assertNotIn('private-password',str(caught.exception))
        rows=p.lines(self.folder/'hot_calls.jsonl')
        self.assertEqual(rows[0]['exception_type'],'OSError')
        self.assertNotIn('private-password',(self.folder/'hot_calls.jsonl').read_text())

    def test_contended_budget_cannot_bypass_first_request_lateness_gate(self):
        client,transport=self.client()
        with patch.object(p,'reserve_call',return_value=1):
            client.clock=lambda:self.now+timedelta(seconds=1)
            with self.assertRaises(RuntimeError):
                client.quotes(['S0000'],fields='all')
        transport.quotes.assert_not_called()
        self.assertFalse(p.lines(self.folder/'hot_calls.jsonl')[0]['dispatched'])
        self.assertEqual(p.lines(self.folder/'hot_calls.jsonl')[0]['not_dispatched_reason'],'dispatch_gate_elapsed')

    def test_foreign_symbols_missing_slot_and_auth_callback_do_not_dispatch(self):
        client,transport=self.client()
        with self.assertRaises(RuntimeError):client.quotes(['UNLISTED'],fields='all')
        client.rejection=Mock(requested=True)
        with self.assertRaises(RuntimeError):client.quotes(['S0000'],fields='all')
        client.rejection=None;client.slot=None
        with self.assertRaises(RuntimeError):client.quotes(['S0000'],fields='all')
        transport.quotes.assert_not_called()

    def test_no_live_transport_can_be_created_by_synthetic_plan(self):
        client=p.CallClient(self.folder,'hot',self.config,lambda:self.now,Event())
        self.assertIsNotNone(client.synthetic)
        self.assertIsNone(client.transport)

    def test_wrong_cli_arguments_precede_password(self):
        with patch.object(entry.getpass,'getpass') as prompt,redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(['schwab']),2)
            self.assertEqual(entry.main(['verify','extra']),2)
        prompt.assert_not_called()

    def test_failed_credential_preflight_does_not_echo_or_save_password(self):
        from mb_market_data import hot_provider_trial
        output=io.StringIO()
        with patch.object(entry,'ROOT',self.folder),patch.object(entry,'verify_sources'),patch.object(entry,'bootstrap'),\
             patch.object(entry,'check_verification'),patch.object(entry,'check_live_window'),\
             patch.object(entry,'load_roster',return_value={'opening_uni':['S0000']*554}),\
             patch.object(entry,'resolve_ecfg',return_value=self.folder/'secure.ecfg'),\
             patch.object(entry.getpass,'getpass',return_value='private-password'),\
             patch.object(hot_provider_trial,'prepare_schwab_client',side_effect=RuntimeError('private-password')),\
             redirect_stdout(output):
            self.assertEqual(entry.main(['schwab','r0.json','r1.json']),1)
        self.assertNotIn('private-password',output.getvalue())
        for path in self.folder.rglob('*'):
            if path.is_file():self.assertNotIn(b'private-password',path.read_bytes())

    def test_source_inventory_cannot_read_outside_package(self):
        source=self.folder/'experiment';source.mkdir()
        p.write_json(source/'source_manifest.json',{'contract':p.CONTRACT,'files':{'../outside':'0'*64}})
        with patch.object(entry,'ROOT',self.folder),patch.object(entry,'HERE',source):
            with self.assertRaises(ValueError):entry.verify_sources()

    def test_isolated_worker_stages_native_import_path_before_live_config_validation(self):
        spec=importlib.util.spec_from_file_location('provider_worker',ROOT/'experiments/combined_schwab_20261019/worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        ready=[]
        def validate(folder):
            self.assertTrue(ready,'Live configuration imports native types and requires bundled src first')
            return self.folder
        with patch.object(sys,'argv',['worker','run','coordinator']),patch.object(worker,'verify_sources'),\
             patch.object(worker,'bootstrap',side_effect=lambda:ready.append(True)),\
             patch.object(worker,'validate_run_path',side_effect=validate),\
             patch.object(worker,'load_config',return_value=({'source':'schwab'},None)),\
             patch.object(worker,'worker',return_value=0):
            self.assertEqual(worker.main(),0)

    def test_ecfg_resolution_uses_existing_encrypted_path_without_cwd_fallback(self):
        config=self.folder/'secure_schwabdev.ecfg';config.write_text('encrypted-placeholder')
        with patch.dict(entry.os.environ,{'MB_VAULT':str(self.folder)},clear=True):
            self.assertEqual(entry.resolve_ecfg(),config.resolve())
        with patch.dict(entry.os.environ,{'MB_SCHWAB_ECFG':str(self.folder/'missing.ecfg')},clear=True):
            with self.assertRaises(ValueError):entry.resolve_ecfg()

    def test_changed_config_is_refused(self):
        p.bootstrap();folder=self.folder/'fresh';p.prepare(folder,60)
        cfg=p.read_json(folder/'config.json');cfg['source']='schwab';p.write_json(folder/'config.json',cfg)
        with self.assertRaises(RuntimeError):p.load_config(folder)

    def test_actual_utc_live_mode_reaches_native_collector_with_mocked_transport(self):
        p.bootstrap()
        from mb_market_data.sampling_acquisition import ExplicitPollSlot
        from mb_market_data.sampling_membership import SamplingChannel
        from mb_market_data.watchlist_polling import ET
        from mb_market_data.hot_provider_trial import SyntheticResponse
        clock=[self.now+timedelta(days=7)]
        roster=p.load_roster(*self.roster_files(),now=self.now)
        folder=self.folder/'native-live-mode'
        def sleep(seconds):clock[0]+=timedelta(seconds=seconds)
        with patch.object(p,'utc_now',side_effect=lambda:clock[0]),patch.object(p.time,'sleep',side_effect=sleep):
            cfg=p.prepare(folder,300,roster=roster,source='schwab')
            self.assertEqual(cfg['utc_offset_seconds'],0.)
            ledger,service=p.load_control(folder)
            service.cycle()
            clock[0]=datetime.fromisoformat(cfg['start'])
            transport=Mock()
            transport.quotes.side_effect=lambda symbols,**kwargs:SyntheticResponse(symbols,1)
            poller,_,_=p.load_poller(folder,'hot',Event(),transport=transport)
            class ResponseClock(datetime):
                @classmethod
                def now(cls,tz=None):return clock[0].astimezone(tz or timezone.utc)
            slot=ExplicitPollSlot(SamplingChannel.HOT,clock[0].astimezone(ET),p.GRIDS['hot'])
            with patch('mb_market_data.schwab_quotes.datetime',ResponseClock):result=poller.sample(slot)
            self.assertEqual(result['status'],'acquired')
            self.assertEqual(transport.quotes.call_count,1)
            store=p.store_for(folder,cfg)
            self.assertEqual(len(store.acquisitions_in_replay_order()),1)
            self.assertFalse(store.membership_revisions_in_effective_order()[0].metadata['synthetic'])

    def test_stop_pointer_cannot_escape_run_root(self):
        root=self.folder/'root';(root/'runs').mkdir(parents=True)
        p.write_json(root/'runs/active_combined_provider.json',{'contract':p.CONTRACT,'folder':str(self.folder)})
        with self.assertRaises(RuntimeError):p.request_stop(root)
        self.assertFalse((self.folder/'STOP').exists())


if __name__=='__main__':unittest.main()
