"""Fail-closed next-session verification without production resources."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import hashlib
import json

from mb_market_data.opening_hierarchy import opening_proposal_payload
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from probes.verify_next_session_readiness import inspect_preparation


class NextSessionReadinessTests(TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.closed=date(2026,11,3)
        self.nxt=date(2026,11,4)
        self.workflow=self.root/'output'/'daily_universe_production'/'2026-11-04-from-2026-11-03'
        self.workflow.mkdir(parents=True)
        self.journal_root=self.root/'output'/'quote_observation_journal_v2_api_opening_2026-11-04'
        self.journal_root.mkdir(parents=True)
        self.journal=self.journal_root/'2026-11-04.sqlite3'
        self.journal.write_bytes(b'synthetic sentinel, mocked reader')
        self.revision=SamplingHierarchyRevision(
            session_date=self.nxt, revision=0,
            effective_at=datetime(2026,11,4,9,30,tzinfo=timezone(timedelta(hours=-5))),
            uni_symbols=('ABC','DEF'),focus_symbols=(),hot_symbols=(),
            source='daily-universe-production-v1',
            reason='synthetic opening',
            metadata={'universe_source_session_date':self.closed.isoformat()})
        items={
            'decision_ledger':'universe/decision_ledger.csv',
            'market_snapshot_manifest':'market_snapshot/manifest.json',
            'opening_hierarchy_r0':'opening_hierarchy_r0.json',
            'symbol_directory_manifest':'symbol_directory/manifest.json',
            'uni_symbols':'universe/uni_symbols.csv',
            'uni_watchlist':'universe/uni_watchlist.csv',
            'universe_manifest':'universe/manifest.json'}
        artifacts={}
        for name,relative in items.items():
            file=self.workflow/relative
            file.parent.mkdir(parents=True,exist_ok=True)
            body=(json.dumps(opening_proposal_payload(self.revision))+'\n') if name=='opening_hierarchy_r0' else ('synthetic '+name+'\n')
            file.write_text(body,encoding='utf-8')
            artifacts[name]={'path':relative,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
        self.manifest={'session_date':self.closed.isoformat(),
                       'target_date':self.nxt.isoformat(),'artifacts':artifacts}
        self.manifest_path=self.workflow/'workflow_manifest.json'
        self.save_manifest()
    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest),encoding='utf-8')
    def verify(self,*,published=True):
        fake=SimpleNamespace(session_date=self.nxt,schema_version=2,
            store=SimpleNamespace(membership_revisions_in_effective_order=lambda:
                (self.revision,) if published else ()),events=lambda:iter((1,)))
        with patch('probes.verify_next_session_readiness.QuoteJournalReplayReader',return_value=fake):
            return inspect_preparation(root=self.root,closed_date=self.closed,
                next_date=self.nxt,journal_root=self.journal_root)
    def test_ready_when_source_hashes_and_published_r0_match(self):
        result=self.verify()
        self.assertEqual(result['status'],'READY')
        self.assertEqual(result['uni_members'],2)
        self.assertEqual(result['workflow_artifacts_verified'],7)
    def test_missing_workflow_blocks(self):
        self.manifest_path.unlink()
        with self.assertRaises(FileNotFoundError):self.verify()
    def test_wrong_source_session_blocks(self):
        self.manifest['session_date']='2026-10-30'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError,'dates differ'):self.verify()
    def test_modified_artifact_blocks(self):
        (self.workflow/'universe'/'uni_symbols.csv').write_text('tampered\n')
        with self.assertRaisesRegex(ValueError,'Artifact hash differs'):self.verify()
    def test_missing_publication_blocks(self):
        with self.assertRaisesRegex(ValueError,'not published'):self.verify(published=False)
    def test_published_hash_mismatch_blocks(self):
        bad=SimpleNamespace(revision=0,content_sha256='0'*64,
            effective_at=self.revision.effective_at,
            published_at=self.revision.effective_at)
        fake=SimpleNamespace(session_date=self.nxt,schema_version=2,
            store=SimpleNamespace(membership_revisions_in_effective_order=lambda:(bad,)),
            events=lambda:iter(()))
        with patch('probes.verify_next_session_readiness.QuoteJournalReplayReader',return_value=fake):
            with self.assertRaisesRegex(ValueError,'not published'):inspect_preparation(
                root=self.root,closed_date=self.closed,next_date=self.nxt,
                journal_root=self.journal_root)
    def test_missing_or_wrong_target_blocks(self):
        self.manifest['target_date']='2026-11-05'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError,'dates differ'):self.verify()
