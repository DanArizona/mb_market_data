from __future__ import annotations
import hashlib,importlib.util,json,sqlite3,tempfile,unittest
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from mb_market_data.sampling_acquisition import AcquisitionSettings,ExplicitPollSlot,MARKER
from mb_market_data.sampling_incremental import IncrementalSamplingPoller,INCREMENTAL_MARKER
from mb_market_data.sampling_membership import SamplingChannel,SamplingHierarchyRevision
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.watchlist_polling import PollWindow
from mb_market_data.hot_provider_trial import replay_digest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('captured_replay_test',ROOT/'experiments/hot_captured_replay_20261019/captured_replay.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def synthetic_fixture():
    # Generated examples contain no captured brokerage values or provenance.
    phases=[]
    for phase_index,name in enumerate(('phase_01_2s','phase_02_4s')):
        samples=[]
        for sample_index in range(3):
            clock=datetime(2026,1,5,15,phase_index,sample_index*2,tzinfo=timezone.utc)
            rows=[]
            for i in range(40):
                symbol='S'+str(i).zfill(3)
                payload={'symbol':symbol,'ssid':i,'assetMainType':'EQUITY','quote':{'lastPrice':10+i+sample_index/10,'totalVolume':100+i,'quoteTime':int(clock.timestamp()*1000),'tradeTime':int(clock.timestamp()*1000)},'fundamental':{'sharesOutstanding':1000000},'reference':{'description':'Generated example '+symbol}}
                rows.append({'symbol':symbol,'status':'quote','quote':payload,'request_started_at_utc':clock.isoformat(),'response_received_at_utc':(clock+timedelta(milliseconds=100)).isoformat()})
            samples.append({'source_sequence':sample_index*3+2,'source_event_sha256':hashlib.sha256(('synthetic:'+str(phase_index)+':'+str(sample_index)).encode()).hexdigest(),'source_completed_at_utc':(clock+timedelta(milliseconds=200)).isoformat(),'source_recorded_at_utc':(clock+timedelta(milliseconds=210)).isoformat(),'quotes':rows})
        phases.append({'name':name,'assembled_count':3,'samples':samples})
    return {'contract':'successful-quote-payload-samples-v1','source_trial':'generated-unit-fixture','phases':phases}


class CapturedReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.fixture_file=self.root/'synthetic_samples.json'
        raw=json.dumps(synthetic_fixture()).encode();self.fixture_file.write_bytes(raw);self.fixture_sha=hashlib.sha256(raw).hexdigest()
        self.fixture=module.load_fixture(self.fixture_file,expected_sha256=self.fixture_sha)
    def changed_fixture(self,edit):
        value=json.loads(self.fixture_file.read_bytes());edit(value)
        raw=json.dumps(value).encode();p=self.root/'changed.json';p.write_bytes(raw)
        return p,hashlib.sha256(raw).hexdigest()
    def test_original_six_complete_payloads_and_aware_source_clocks(self):
        self.assertEqual(len(self.fixture['samples']),6);self.assertEqual(len(self.fixture['roster']),40)
        self.assertEqual([p['assembled_count'] for p in self.fixture['source']['phases']],[3,3])
        self.assertIn('ssid',self.fixture['samples'][0]['quotes'][0]['quote'])
    def test_changed_original_bytes_refused_before_decoding(self):
        p=self.root/'changed.json';p.write_bytes(self.fixture_file.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'hash differs'):module.load_fixture(p,expected_sha256=self.fixture_sha)
    def test_malformed_roster_status_and_clock_are_refused(self):
        edits=[lambda v:v['phases'][0]['samples'][0]['quotes'].pop(),lambda v:v['phases'][0]['samples'][0]['quotes'][0].update(status='invalid'),lambda v:v['phases'][0]['samples'][0]['quotes'][0].update(response_received_at_utc='2026-10-19T01:00:00')]
        for edit in edits:
            p,sha=self.changed_fixture(edit)
            with self.assertRaises(ValueError):module.load_fixture(p,expected_sha256=sha)
    def test_client_returns_independent_original_payloads_without_retiming(self):
        client=module.CapturedClient(self.fixture);expected=self.fixture['samples'][0]['quotes'][0]['quote']
        first=client.quotes(self.fixture['roster'],fields='all').json();symbol=self.fixture['roster'][0]
        self.assertEqual(first[symbol],expected);first[symbol]['quote']['lastPrice']=-1
        for _ in range(5):client.quotes(self.fixture['roster'],fields='all')
        self.assertEqual(client.quotes(self.fixture['roster'],fields='all').json()[symbol],expected)
        self.assertEqual(client.used_samples,[0,1,2,3,4,5,0])
    def test_client_rejects_partial_roster_and_wrong_fields_before_replay(self):
        client=module.CapturedClient(self.fixture)
        for symbols,fields in [(self.fixture['roster'][:-1],'all'),(self.fixture['roster'],'quote')]:
            with self.assertRaises(ValueError):client.quotes(symbols,fields=fields)
        self.assertEqual(client.count,0)
    def test_all_captured_shapes_use_actual_collector_store_and_replay(self):
        base=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=14,minute=0,second=0,microsecond=0);clock=[base]
        class Logical(datetime):
            @classmethod
            def now(cls,tz=None):return clock[0].astimezone(tz or timezone.utc)
        j=QuoteObservationStore(self.root/'quotes.sqlite3',session_date=base.date(),schema_version=2);j.initialize();roster=self.fixture['roster']
        j.record_membership_revision(SamplingHierarchyRevision(session_date=base.date(),revision=0,effective_at=base,uni_symbols=roster,focus_symbols=roster,hot_symbols=roster,source='captured_offline_test',metadata={MARKER:True,INCREMENTAL_MARKER:True}))
        client=module.CapturedClient(self.fixture);settings=AcquisitionSettings(SamplingChannel.HOT,tuple(range(0,60,2)),PollWindow(base,base+timedelta(seconds=12)),400,'all','captured_offline_test',.75,.05)
        p=IncrementalSamplingPoller(j,settings=settings,client=client,clock=lambda:clock[0]);p.initialize();full=p.full_reconstructions
        with patch('mb_market_data.schwab_quotes.datetime',Logical):
            for i in range(6):
                clock[0]=base+timedelta(seconds=i*2)
                self.assertEqual(p.sample(ExplicitPollSlot(SamplingChannel.HOT,clock[0],settings.seconds))['status'],'acquired')
        self.assertEqual(p.full_reconstructions,full)
        events=[e for e in p.snapshot()['events'] if e['kind']=='assembled']
        for event,sample in zip(events,self.fixture['samples']):
            self.assertEqual([r['quote'] for r in event['result']['results']],[r['quote'] for r in sample['quotes']])
            self.assertNotEqual(event['completed_at_utc'],sample['source_completed_at_utc'])
        state=replay_digest(j.database_path);self.assertEqual(state['observations'],240);self.assertEqual(state,replay_digest(j.database_path))
        with closing(sqlite3.connect(j.database_path)) as c:self.assertEqual(c.execute("SELECT count(*) FROM quote_observation WHERE status='quote'").fetchone()[0],240)

class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);(self.root/'runs').mkdir()
        self.path=self.root/'runs/report.json';self.report={'passed':True,'fixture_sha256':'b'*64,'prefill_acquisitions':300,'elapsed_acquired':30,'exact_original_quote_payloads':True,'platform':'win32','audit_passed':True,'replays_identical':True,'no_full_reconstruction_during_polling':True}
        self.path.write_text(json.dumps(self.report));module.write_gate(self.root,self.path,manifest_sha256='pinned',fixture_sha256='b'*64)
    def test_gate_accepts_matching_native_evidence_and_rejects_changed_source(self):
        self.assertEqual(module.check_gate(self.root,manifest_sha256='pinned',fixture_sha256='b'*64,platform='win32')['platform'],'win32')
        with self.assertRaisesRegex(ValueError,'source/fixture differs'):module.check_gate(self.root,manifest_sha256='changed',fixture_sha256='b'*64,platform='win32')
    def test_linux_gate_never_allows_live_launcher(self):
        with self.assertRaisesRegex(ValueError,'Native Windows'):module.check_gate(self.root,manifest_sha256='pinned',fixture_sha256='b'*64,platform='linux')
        self.report['platform']='linux';self.path.write_text(json.dumps(self.report));module.write_gate(self.root,self.path,manifest_sha256='pinned',fixture_sha256='b'*64)
        with self.assertRaisesRegex(ValueError,'Native Windows'):module.check_gate(self.root,manifest_sha256='pinned',fixture_sha256='b'*64,platform='win32')
    def test_changed_or_outside_report_cannot_satisfy_gate(self):
        self.path.write_text(self.path.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'report differs'):module.check_gate(self.root,manifest_sha256='pinned',fixture_sha256='b'*64,platform='win32')
        with self.assertRaisesRegex(ValueError,'inside bundle runs'):module.write_gate(self.root,self.root/'outside.json',manifest_sha256='pinned',fixture_sha256='b'*64)
    def test_partial_or_failed_offline_proof_cannot_write_gate(self):
        for key,value in [('passed',False),('prefill_acquisitions',299),('elapsed_acquired',29),('exact_original_quote_payloads',False)]:
            report={**self.report,key:value};self.path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'Complete captured'):module.write_gate(self.root,self.path,manifest_sha256='pinned',fixture_sha256='b'*64)
