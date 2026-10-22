"""Plain receipt reads share all native hierarchy validation and fresh bytes."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mb_market_data.quote_observation_store import QuoteObservationStore, QuoteObservationStoreError, _fingerprint
from mb_market_data.sampling_membership import (
    SamplingHierarchyRevision, MembershipRevisionError, _validated_hierarchy_content)


class PlainReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = QuoteObservationStore(Path(self.temp.name)/'journal.sqlite3',
            session_date=date(2026, 11, 2), schema_version=2)
        self.store.initialize()
        self.value = SamplingHierarchyRevision(session_date=self.store.session_date,
            revision=0, effective_at=datetime(2026, 11, 2, 15, 0, 0, 123456, tzinfo=timezone.utc),
            uni_symbols=('MSFT', 'AAPL'), focus_symbols=('MSFT',), hot_symbols=(),
            source='test', reason='résumé',
            metadata={'rows': [None, True, 1, 1.0, 1.25, '東京'], 'nested': {'empty': []}})
        with patch('mb_market_data.quote_observation_store._utc_now',
                   return_value=self.value.effective_at-timedelta(seconds=1)):
            self.store.record_membership_revision(self.value)

    def test_supplied_non_json_storage_value_cannot_fall_back_to_empty_metadata(self):
        for raw in (None, 1, True):
            with self.subTest(raw=raw), self.assertRaises(TypeError):
                _validated_hierarchy_content(session_date=self.value.session_date,
                    revision=0, effective_at=self.value.effective_at,
                    uni_symbols=self.value.uni_symbols, focus_symbols=self.value.focus_symbols,
                    hot_symbols=(), source='test', reason=None, metadata={},
                    published_at=None, contract=self.value.contract, _metadata_json_text=raw)

    def readers(self):
        return (self.store.membership_revisions_in_effective_order,
                self.store.validated_membership_revision_payloads)

    @staticmethod
    def refresh_channel_hashes(c):
        row = c.execute('SELECT * FROM sampling_membership_revision').fetchone()
        for channel in c.execute('SELECT * FROM sampling_channel_revision').fetchall():
            symbols = tuple(r[0] for r in c.execute(
                'SELECT symbol FROM sampling_channel_member WHERE channel=? ORDER BY ordinal', (channel['channel'],)))
            header = (channel['channel'], row['session_date'], row['revision'], row['effective_at_utc'],
                      len(symbols), row['source'], row['reason'], row['metadata_json'])
            sha = _fingerprint({'header': header, 'symbols': symbols})
            c.execute('UPDATE sampling_channel_revision SET content_sha256=? WHERE channel=?', (sha, channel['channel']))

    def test_plain_receipt_hash_and_wire_values_match_immutable_native_read(self):
        native = self.store.membership_revisions_in_effective_order()[0]
        receipt = self.store.validated_membership_revision_payloads()[0]
        p = receipt['proposal']
        rebuilt = SamplingHierarchyRevision(session_date=date.fromisoformat(p['session_date']),
            revision=p['revision'], effective_at=datetime.fromisoformat(p['effective_at_utc']),
            uni_symbols=p['uni_symbols'], focus_symbols=p['focus_symbols'], hot_symbols=p['hot_symbols'],
            source=p['source'], reason=p['reason'], metadata=p['metadata'],
            published_at=datetime.fromisoformat(receipt['published_at_utc']))
        self.assertEqual((native.content_sha256, receipt['content_sha256']), (rebuilt.content_sha256,)*2)
        self.assertEqual(native.metadata['rows'], tuple(p['metadata']['rows']))
        self.assertEqual(p['effective_at_utc'], native.effective_at.isoformat())
        self.assertEqual(receipt['published_at_utc'], native.published_at.isoformat())

    def test_plain_payload_mutation_cannot_poison_next_read_or_native_value(self):
        before = self.store.validated_membership_revision_payloads()
        expected = json.dumps(before, sort_keys=True)
        before[0]['proposal']['metadata']['rows'].append('FORGED')
        before[0]['proposal']['uni_symbols'].clear()
        self.assertEqual(json.dumps(self.store.validated_membership_revision_payloads(), sort_keys=True), expected)
        self.assertEqual(self.store.membership_revisions_in_effective_order()[0].uni_symbols, ('AAPL', 'MSFT'))

    def test_channel_corruption_after_warm_read_fails_both_readers(self):
        self.store.validated_membership_revision_payloads()
        with closing(sqlite3.connect(self.store.database_path)) as c, c:
            c.execute("UPDATE sampling_channel_member SET symbol='FORGED' WHERE channel='uni' AND ordinal=0")
        for read in self.readers():
            with self.subTest(read=read.__name__), self.assertRaisesRegex(QuoteObservationStoreError, 'content hash'):
                read()

    def test_rehashed_channel_cannot_bypass_hierarchy_nesting(self):
        with closing(sqlite3.connect(self.store.database_path)) as c, c:
            c.row_factory = sqlite3.Row
            c.execute("UPDATE sampling_channel_member SET symbol='ZZZZ' WHERE channel='focus'")
            self.refresh_channel_hashes(c)
        for read in self.readers():
            with self.subTest(read=read.__name__), self.assertRaisesRegex(MembershipRevisionError, 'Focus must be a subset'):
                read()

    def test_publication_time_is_validated_even_though_excluded_from_content_hash(self):
        later = self.value.effective_at + timedelta(seconds=1)
        with closing(sqlite3.connect(self.store.database_path)) as c, c:
            c.execute('UPDATE sampling_membership_revision SET published_at_utc=?', (later.isoformat(),))
        for read in self.readers():
            with self.subTest(read=read.__name__), self.assertRaisesRegex(MembershipRevisionError, 'effective_at cannot precede'):
                read()

    def test_unsupported_contract_cannot_borrow_an_existing_hash(self):
        with closing(sqlite3.connect(self.store.database_path)) as c, c:
            # Normal writes already reject this at the schema boundary. Bypass
            # that constraint only to model corrupt retained bytes on a read.
            c.execute('PRAGMA ignore_check_constraints=ON')
            c.execute("UPDATE sampling_membership_revision SET membership_contract='forged'")
        for read in self.readers():
            with self.subTest(read=read.__name__), self.assertRaisesRegex(MembershipRevisionError, 'unsupported sampling hierarchy'):
                read()

    def test_rehashed_nonfinite_or_nonmapping_metadata_remains_invalid(self):
        for raw in ('{"bad":NaN}', '{"bad":1e999}', '[1,2]', 'null'):
            with self.subTest(raw=raw):
                with closing(sqlite3.connect(self.store.database_path)) as c, c:
                    c.row_factory = sqlite3.Row
                    c.execute('UPDATE sampling_membership_revision SET metadata_json=?', (raw,))
                    c.execute('UPDATE sampling_channel_revision SET metadata_json=?', (raw,))
                    self.refresh_channel_hashes(c)
                for read in self.readers():
                    with self.subTest(read=read.__name__), self.assertRaises((TypeError, ValueError)):
                        read()


if __name__ == '__main__':
    unittest.main()
