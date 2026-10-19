from __future__ import annotations
import importlib.util,json,sqlite3,tempfile,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from zipfile import ZipFile
import test_hot_provider_trial as accepted
from mb_market_data.hot_provider_trial import SyntheticClient
from mb_market_data.sampling_incremental import IncrementalSamplingPoller
from mb_market_data.sampling_acquisition import canonical,digest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('export_payloads_test',ROOT/'experiments/hot_incremental_20261019/export_payloads.py')
exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)

class IncrementalTrialTests(unittest.TestCase):
    setUp=accepted.TrialTests.setUp
    run_proof=accepted.TrialTests.run_proof
    def test_guarded_mode_audits_replays_and_exposes_gate_costs(self):
        report=self.run_proof(verification_mode='guarded_incremental')
        self.assertTrue(report['passed'],report)
        self.assertEqual(report['verification_mode'],'guarded_incremental');self.assertEqual(len(report['source_sha256']),3)
        for phase in report['phases']:
            self.assertEqual(phase['verification_diagnostics']['full_reconstructions'],3)
            self.assertTrue(phase['timings']['before_api_gate_seconds']['count'])
    def test_clean_received_quotes_is_independent_of_missing_coverage(self):
        original=IncrementalSamplingPoller.sample;count=[0];clock=self.clock
        def sample(poller,slot,**kwargs):
            count[0]+=1
            if count[0]==2:clock.value+=accepted.timedelta(seconds=.751)
            return original(poller,slot,**kwargs)
        with patch.object(IncrementalSamplingPoller,'sample',sample):
            report=self.run_proof(verification_mode='guarded_incremental')
        self.assertFalse(report['complete_coverage']);self.assertTrue(report['clean_transport']);self.assertFalse(report['passed'])
        self.assertEqual(report['received_observations'],6)
        missed=report['phases'][0]['missed_gate_outcomes'];self.assertEqual(missed[0]['gate'],'before_dispatch_deadline')

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for name in ('phase_01_2s','phase_02_4s'):
            path=self.root/name/'quotes.hot.poller.sqlite3';path.parent.mkdir()
            identity={'contract':'isolated_hot_acquisition_v1','settings':{'watchlist_kind':'hot'},'synthetic_export_fixture':True};previous=digest(identity)
            with closing(sqlite3.connect(path)) as c,c:
                c.execute('CREATE TABLE acquisition_identity(id INTEGER PRIMARY KEY,payload TEXT)');c.execute('INSERT INTO acquisition_identity VALUES(1,?)',(canonical(identity),))
                c.execute('CREATE TABLE acquisition_event(sequence INTEGER,payload TEXT,previous_sha256 TEXT,event_sha256 TEXT)')
                for sequence in range(1,6):
                    value={'kind':'assembled','recorded_at_utc':'2026-10-18T21:00:00+00:00','completed_at_utc':'2026-10-18T21:00:00+00:00','result':{'results':[{'symbol':'SPY','status':'quote','quote':{'quote':{'lastPrice':sequence}},'request_started_at_utc':'2026-10-18T21:00:00+00:00','response_received_at_utc':'2026-10-18T21:00:00+00:00'}]}}
                    sha=digest({'sequence':sequence,'payload':value,'previous_sha256':previous});c.execute('INSERT INTO acquisition_event VALUES(?,?,?,?)',(sequence,canonical(value),previous,sha));previous=sha
    def test_read_only_first_middle_last_export_preserves_sources(self):
        paths=list(self.root.glob('*/quotes.hot.poller.sqlite3'));before=[(p.read_bytes(),p.stat().st_mtime_ns) for p in paths]
        output=exporter.export(self.root,self.root/'exports')
        with ZipFile(output) as z:value=json.loads(z.read('quote_payload_samples.json'))
        self.assertEqual([x['source_sequence'] for x in value['phases'][0]['samples']],[1,3,5])
        self.assertEqual(before,[(p.read_bytes(),p.stat().st_mtime_ns) for p in paths])
        self.assertEqual(len(list(self.root.glob('exports/*'))),1)
    def test_changed_source_chain_refuses_export(self):
        with closing(sqlite3.connect(self.root/'phase_01_2s/quotes.hot.poller.sqlite3')) as c,c:c.execute("UPDATE acquisition_event SET payload='{}' WHERE sequence=1")
        with self.assertRaisesRegex(ValueError,'chain differs'):exporter.export(self.root,self.root/'exports')
        self.assertFalse((self.root/'exports').exists())
    def test_missing_source_refuses_without_creating_database(self):
        with self.assertRaisesRegex(ValueError,'missing'):exporter.export(self.root/'missing',self.root/'exports')
        self.assertFalse((self.root/'missing').exists())
