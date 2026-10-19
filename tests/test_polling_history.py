import importlib.util
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'experiments/polling_history_20261019'))
import history_proof as h


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'run';self.folder.mkdir()

    def config(self,profile='full'):
        value=h.configuration(self.folder,profile)
        h.write_json(self.folder/'config.json',value)
        import hashlib
        h.write_json(self.folder/'identity.json',dict(contract=h.CONTRACT,config_sha256=hashlib.sha256((self.folder/'config.json').read_bytes()).hexdigest()))
        return value

    def test_full_profile_has_regular_session_scale_and_explicit_tail(self):
        config=self.config()
        expected={'uni':780,'focus':1560,'hot':11700}
        for channel,count in expected.items():self.assertEqual(len(list(h.slots(config,channel))),count)
        total=sum(expected[c]*len(config[c]) for c in expected)
        self.assertEqual(total,962520)
        tail=datetime.fromisoformat(config['tail_start'])
        for c,n in {'uni':2,'focus':4,'hot':30}.items():
            self.assertEqual(sum(v>=tail for v in h.slots(config,c)),n)

    def test_config_change_and_moved_run_are_rejected(self):
        config=self.config();self.assertEqual(h.load_config(self.folder),config)
        config['dispatch_deadline_seconds']=99;h.write_json(self.folder/'config.json',config)
        with self.assertRaises(RuntimeError):h.load_config(self.folder)

    def test_unarmed_clock_stays_fixed_then_advances_from_identity_bound_arm(self):
        config=self.config();clock=h.Clock(self.folder,config)
        fixed=clock();self.assertEqual(clock(),fixed)
        h.write_json(self.folder/'ARM.json',dict(run_id=config['run_id'],actual_utc=h.text(h.utc_now()),logical_utc=h.text(fixed)))
        self.assertGreaterEqual(clock(),fixed)
        self.assertIsNotNone(clock.arm)

    def test_foreign_clock_arm_and_wrong_origin_are_refused(self):
        config=self.config()
        for rid,origin in [('foreign',datetime.fromisoformat(config['tail_start'])-timedelta(seconds=5)),
                           (config['run_id'],datetime.fromisoformat(config['tail_start']))]:
            h.write_json(self.folder/'ARM.json',dict(run_id=rid,actual_utc=h.text(h.utc_now()),logical_utc=h.text(origin)))
            with self.assertRaises(RuntimeError):h.Clock(self.folder,config)()

    def test_production_and_existing_directories_are_preserved(self):
        with self.assertRaises(RuntimeError):h.prepare(self.folder,'verify',lambda _:None)
        path=self.folder/'quote_observation_journal_v2_api_opening_2026-10-19'/'new'
        with self.assertRaises(RuntimeError):h.prepare(path,'verify',lambda _:None)
        self.assertFalse(path.exists())

    def test_stop_is_local_file_only(self):
        h.stopped(self.folder)
        (self.folder/'STOP').write_text('stop')
        with self.assertRaises(InterruptedError):h.stopped(self.folder)

    def test_assessment_rejects_partial_input_without_creating_output(self):
        from reassess import reassess
        h.write_json(self.folder/'report.json',dict(contract='other',result='PASS'))
        (self.folder/'quotes.sqlite3').write_bytes(b'not a database')
        output=self.folder.parent/'derived'
        with self.assertRaises(ValueError):reassess(self.folder,output)
        self.assertFalse(output.exists())

    def test_read_only_assessment_verifies_real_journal_and_preserves_original(self):
        from unittest.mock import patch
        from mb_market_data.quote_observation_store import QuoteObservationStore
        from mb_market_data.sampling_membership import SamplingHierarchyRevision,SamplingChannel
        from mb_market_data.sampling_acquisition import AcquisitionSettings,ExplicitPollSlot,IsolatedSamplingPoller,MARKER
        from mb_market_data.watchlist_polling import PollWindow
        from mb_market_data.hot_provider_trial import replay_digest
        from mb_market_data.sampling_assessment import LEGACY_AVAILABILITY_FAILURE
        from reassess import reassess,fingerprint
        from provider_load import SyntheticResponse
        at=(h.utc_now()+timedelta(days=2)).replace(hour=14,minute=0,second=0,microsecond=0)
        store=QuoteObservationStore(self.folder/'quotes.sqlite3',session_date=at.date(),schema_version=2);store.initialize()
        store.record_membership_revision(SamplingHierarchyRevision(session_date=at.date(),revision=0,effective_at=at,
            uni_symbols=['S0000'],focus_symbols=['S0000'],hot_symbols=['S0000'],source='synthetic_assessment_test',metadata={MARKER:True}))
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls,tz=None):return at.astimezone(tz or timezone.utc)
        channels={}
        for channel in h.CHANNELS:
            class Client:
                def quotes(self,symbols,*,fields):
                    return SyntheticResponse({'errors':{'invalidSymbols':['S0000']}} if channel=='uni' else
                        {'S0000':dict(symbol='S0000',assetMainType='EQUITY',quote=dict(lastPrice=10,quoteTime=int(at.timestamp()*1000)))})
            settings=AcquisitionSettings(SamplingChannel(channel),(0,),PollWindow(at,at+timedelta(minutes=1)),400,'all','synthetic_assessment_test',.75,.05)
            poller=IsolatedSamplingPoller(store,settings=settings,client=Client(),clock=lambda:at);poller.initialize()
            with patch('mb_market_data.schwab_quotes.datetime',LogicalDatetime):
                self.assertEqual(poller.sample(ExplicitPollSlot(SamplingChannel(channel),at,(0,)))['status'],'acquired')
            channels[channel]=dict(expected_slots=1,committed_slots=1,missing_slots=0,complete=True,no_full_scan_during_polling=True,
                status_counts={'invalid' if channel=='uni' else 'quote':1})
        original=dict(contract='isolated-combined-provider-v1',result='FAILED',failures=[LEGACY_AVAILABILITY_FAILURE],
            audit_passed=True,replays_identical=True,request_replay_identical=True,publication_replay_identical=True,
            channels=channels,logical_end=h.text(at+timedelta(minutes=1)),journal_replay=replay_digest(store.database_path))
        h.write_json(self.folder/'report.json',original);before=fingerprint(self.folder)
        output=self.folder.parent/'derived'
        result=reassess(self.folder,output,on_status=lambda _:None)
        self.assertEqual(result['result'],'PASS_WITH_WARNINGS')
        self.assertEqual(result['unavailable_outcomes'][0]['symbol'],'S0000')
        self.assertEqual(fingerprint(self.folder),before)
        self.assertEqual(h.read_json(self.folder/'report.json'),original)
