from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mb_market_data.polling_journal import PollingJournalSession
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.schwab_quotes import (
    QuoteBatchResult,
    QuoteResult,
    QuoteStatus,
)
from mb_market_data.watchlist_polling import (
    PollRequest,
    PollSlot,
    SkippedPollSlot,
    WatchlistKind,
    WatchlistSnapshot,
    resolve_provider_poll_slot,
)


ET = ZoneInfo("America/New_York")
SESSION_DATE = date(2026, 9, 10)
WINDOW_START = datetime(2026, 9, 10, 9, 30, tzinfo=ET)
WINDOW_END = datetime(2026, 9, 10, 9, 31, tzinfo=ET)


class TestQuoteJournalAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.database_path = root / "2026-09-10.sqlite3"
        self.evidence_directory = root / "evidence-focus"
        self.evidence_directory.mkdir()
        (self.evidence_directory / "sample.jsonl").write_bytes(b"12345")
        self.snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=SESSION_DATE,
            revision=0,
            effective_at=WINDOW_START,
            symbols=("SPY", "QQQ"),
        )
        self.journal = PollingJournalSession.start(
            self.database_path,
            run_id="focus-run-1",
            started_at=WINDOW_START - timedelta(minutes=5),
            software_version="test-commit",
            configuration={
                "watchlist_kind": "focus",
                "poll_seconds": [5, 20, 35, 50],
                "poll_window_start_et": WINDOW_START.isoformat(),
                "poll_window_end_et": WINDOW_END.isoformat(),
                "evidence_run_directory": str(self.evidence_directory),
            },
            snapshot=self.snapshot,
            revision_source="unit-test",
            host="MasterBot",
        )

    def record_second(self, second: int) -> str:
        scheduled_at = WINDOW_START.replace(second=second)
        request = PollRequest(
            slot=PollSlot(
                watchlist_kind=WatchlistKind.FOCUS,
                scheduled_at=scheduled_at,
            ),
            watchlist_revision=0,
            symbols=self.snapshot.symbols,
            dispatched_at=scheduled_at + timedelta(milliseconds=1),
        )
        received_at = request.dispatched_at + timedelta(milliseconds=200)
        result = QuoteBatchResult(
            results=tuple(
                QuoteResult(
                    symbol=symbol,
                    status=(
                        QuoteStatus.QUOTE
                        if symbol == "SPY"
                        else QuoteStatus.INVALID
                    ),
                    quote=(
                        {"symbol": symbol, "quote": {"lastPrice": 100.0}}
                        if symbol == "SPY"
                        else None
                    ),
                    detail=None,
                    batch_number=1,
                    request_started_at_utc=request.dispatched_at,
                    response_received_at_utc=received_at,
                )
                for symbol in request.symbols
            ),
            request_count=1,
            batch_size=400,
            unexpected_symbols=(),
        )
        self.journal.record_acquisition(
            request,
            result,
            completed_at=received_at,
        )
        return request.batch_id

    def test_complete_healthy_window_passes(self) -> None:
        for second in (5, 20, 35, 50):
            self.record_second(second)

        report = audit_quote_journal(
            self.database_path,
            audited_at=WINDOW_END + timedelta(minutes=1),
        )

        self.assertTrue(report.passed)
        self.assertEqual(
            report.membership_contract,
            "legacy-independent-channels",
        )
        self.assertEqual(report.integrity_results, ("ok",))
        self.assertEqual(report.table_counts["quote_acquisition"], 4)
        self.assertEqual(report.table_counts["quote_observation"], 8)
        self.assertEqual(len(report.runs), 1)
        self.assertEqual(report.runs[0].software_version, "test-commit")
        channel = report.channels[0]
        self.assertEqual(channel.channel, "focus")
        self.assertEqual(channel.acquisition_count, 4)
        self.assertEqual(channel.expected_slot_count, 4)
        self.assertEqual(channel.missing_slot_ids, ())
        self.assertEqual(channel.status_counts, {"invalid": 4, "quote": 4})
        self.assertEqual(report.evidence_bytes["focus-run-1"], 5)
        self.assertGreater(report.storage_bytes["database"], 0)

    def test_missing_configured_slot_fails(self) -> None:
        for second in (5, 20, 35):
            self.record_second(second)

        report = audit_quote_journal(
            self.database_path,
            audited_at=WINDOW_END + timedelta(minutes=1),
        )

        self.assertFalse(report.passed)
        self.assertEqual(
            report.channels[0].missing_slot_ids,
            ("focus:2026-09-10T09:30:50-04:00",),
        )
        self.assertIn(
            "missing_poll_slots",
            {item.code for item in report.findings},
        )

    def test_future_slots_are_not_expected_during_live_audit(self) -> None:
        for second in (5, 20):
            self.record_second(second)

        report = audit_quote_journal(
            self.database_path,
            audited_at=WINDOW_START.replace(second=25),
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.channels[0].expected_slot_count, 2)

    def test_missing_observation_row_fails(self) -> None:
        acquisition_id = self.record_second(5)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "DELETE FROM quote_observation "
                "WHERE acquisition_id = ? AND symbol = 'QQQ'",
                (acquisition_id,),
            )
            connection.commit()

        report = audit_quote_journal(
            self.database_path,
            audited_at=WINDOW_START.replace(second=10),
        )

        self.assertFalse(report.passed)
        self.assertEqual(
            report.channels[0].row_count_mismatch_ids,
            (acquisition_id,),
        )
        self.assertIn(
            "acquisition_observation_count",
            {item.code for item in report.findings},
        )

    def test_naive_audit_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            audit_quote_journal(
                self.database_path,
                audited_at=datetime(2026, 9, 10, 9, 30),
            )


class TestHierarchyQuoteJournalAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "2026-09-10-v2.sqlite3"
        )
        self.journal = PollingJournalSession.register_hierarchy_polling(
            self.database_path,
            session_date=SESSION_DATE,
            run_id="focus-v2-run",
            started_at=WINDOW_START - timedelta(minutes=5),
            software_version="test-commit",
            configuration={
                "watchlist_kind": "focus",
                "poll_seconds": [5],
                "poll_window_start_et": WINDOW_START.isoformat(),
                "poll_window_end_et": WINDOW_END.isoformat(),
            },
        )
        self.revision = SamplingHierarchyRevision(
            session_date=SESSION_DATE,
            revision=0,
            effective_at=WINDOW_START,
            uni_symbols=("QQQ", "SPY"),
            focus_symbols=("SPY",),
            hot_symbols=(),
            source="unit-test-publisher",
        )
        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=WINDOW_START - timedelta(minutes=10),
        ):
            self.journal.store.record_membership_revision(self.revision)

    def record_focus_acquisition(self) -> str:
        slot = PollSlot(
            WatchlistKind.FOCUS,
            WINDOW_START.replace(second=5),
        )
        request = resolve_provider_poll_slot(
            slot,
            self.journal,
            dispatched_at=slot.scheduled_at + timedelta(milliseconds=1),
        )
        assert request is not None
        received_at = request.dispatched_at + timedelta(milliseconds=200)
        result = QuoteBatchResult(
            results=(
                QuoteResult(
                    symbol="SPY",
                    status=QuoteStatus.QUOTE,
                    quote={"symbol": "SPY", "quote": {"lastPrice": 100}},
                    detail=None,
                    batch_number=1,
                    request_started_at_utc=request.dispatched_at,
                    response_received_at_utc=received_at,
                ),
            ),
            request_count=1,
            batch_size=400,
            unexpected_symbols=(),
        )
        self.journal.record_acquisition(
            request,
            result,
            completed_at=received_at,
        )
        return request.batch_id

    def test_complete_v2_hierarchy_and_binding_pass(self) -> None:
        self.record_focus_acquisition()

        report = audit_quote_journal(
            self.database_path,
            audited_at=WINDOW_START.replace(second=10),
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.schema_version, 2)
        self.assertEqual(
            report.membership_contract,
            "nested-uni-focus-hot-v1",
        )
        self.assertEqual(
            report.table_counts["sampling_membership_revision"],
            1,
        )
        self.assertEqual(
            tuple(channel.channel for channel in report.channels),
            ("focus", "hot", "uni"),
        )

    def test_incomplete_bundle_fails_audit(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "DELETE FROM sampling_channel_revision "
                "WHERE channel = 'hot'"
            )
            connection.commit()

        report = audit_quote_journal(self.database_path)

        self.assertFalse(report.passed)
        self.assertIn(
            "hierarchy_bundle_channels",
            {finding.code for finding in report.findings},
        )
        self.assertIn(
            "hierarchy_content",
            {finding.code for finding in report.findings},
        )

    def test_changed_membership_and_acquisition_binding_fail_audit(
        self,
    ) -> None:
        acquisition_id = self.record_focus_acquisition()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE sampling_channel_member SET symbol = 'OUT' "
                "WHERE channel = 'focus' AND symbol = 'SPY'"
            )
            connection.commit()

        report = audit_quote_journal(self.database_path)
        codes = {finding.code for finding in report.findings}

        self.assertFalse(report.passed)
        self.assertIn("hierarchy_content", codes)
        self.assertIn("acquisition_revision_binding", codes)
        self.assertTrue(
            any(
                acquisition_id in finding.detail
                for finding in report.findings
                if finding.code == "acquisition_revision_binding"
            )
        )

    def test_acquisition_before_bound_revision_effective_time_fails(
        self,
    ) -> None:
        self.record_focus_acquisition()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE sampling_channel_revision "
                "SET effective_at_utc = '2026-09-10T13:30:10.000000Z' "
                "WHERE channel = 'focus' AND revision = 0"
            )
            connection.commit()

        report = audit_quote_journal(self.database_path)

        self.assertFalse(report.passed)
        self.assertIn(
            "acquisition_revision_effective_time",
            {finding.code for finding in report.findings},
        )

    def test_empty_membership_skip_satisfies_slot_completeness(self) -> None:
        empty_path = (
            Path(self.temporary_directory.name) / "empty-focus-v2.sqlite3"
        )
        journal = PollingJournalSession.register_hierarchy_polling(
            empty_path,
            session_date=SESSION_DATE,
            run_id="empty-focus-v2-run",
            started_at=WINDOW_START - timedelta(minutes=5),
            software_version="test-commit",
            configuration={
                "watchlist_kind": "focus",
                "poll_seconds": [5],
                "poll_window_start_et": WINDOW_START.isoformat(),
                "poll_window_end_et": WINDOW_END.isoformat(),
            },
        )
        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=WINDOW_START - timedelta(minutes=10),
        ):
            journal.store.record_membership_revision(
                SamplingHierarchyRevision(
                    session_date=SESSION_DATE,
                    revision=0,
                    effective_at=WINDOW_START,
                    uni_symbols=("SPY",),
                    focus_symbols=(),
                    hot_symbols=(),
                    source="unit-test-publisher",
                )
            )
        slot = PollSlot(
            WatchlistKind.FOCUS,
            WINDOW_START.replace(second=5),
        )
        skipped = resolve_provider_poll_slot(
            slot,
            journal,
            dispatched_at=slot.scheduled_at + timedelta(milliseconds=1),
        )
        self.assertIsInstance(skipped, SkippedPollSlot)
        assert isinstance(skipped, SkippedPollSlot)
        journal.record_skipped_slot(skipped)

        report = audit_quote_journal(
            empty_path,
            audited_at=WINDOW_START.replace(second=10),
        )

        self.assertTrue(report.passed)
        focus = next(
            item for item in report.channels if item.channel == "focus"
        )
        self.assertEqual(focus.acquisition_count, 0)
        self.assertEqual(focus.skipped_slot_count, 1)
        self.assertEqual(focus.missing_slot_ids, ())
        self.assertEqual(report.table_counts["poll_slot_skip"], 1)

    def test_stale_acquisition_binding_fails_audit(self) -> None:
        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=WINDOW_START - timedelta(minutes=9),
        ):
            self.journal.store.record_membership_revision(
                SamplingHierarchyRevision(
                    session_date=SESSION_DATE,
                    revision=1,
                    effective_at=WINDOW_START + timedelta(seconds=1),
                    uni_symbols=("DIA", "QQQ", "SPY"),
                    focus_symbols=("SPY",),
                    hot_symbols=(),
                    source="unit-test-publisher",
                    reason="change uni only",
                )
            )
        acquisition_id = self.record_focus_acquisition()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE quote_acquisition SET channel_revision = 0 "
                "WHERE acquisition_id = ?",
                (acquisition_id,),
            )
            connection.commit()

        report = audit_quote_journal(
            self.database_path,
            audited_at=WINDOW_START.replace(second=10),
        )

        self.assertFalse(report.passed)
        self.assertIn(
            "acquisition_latest_revision",
            {finding.code for finding in report.findings},
        )

    def test_channel_header_corruption_fails_hierarchy_audit(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE sampling_channel_revision SET source = 'corrupted' "
                "WHERE channel = 'focus'"
            )
            connection.commit()

        report = audit_quote_journal(self.database_path)

        self.assertFalse(report.passed)
        self.assertIn(
            "hierarchy_content",
            {finding.code for finding in report.findings},
        )

    def test_skip_content_hash_corruption_fails_audit(self) -> None:
        empty_path = (
            Path(self.temporary_directory.name) / "bad-skip-v2.sqlite3"
        )
        journal = PollingJournalSession.register_hierarchy_polling(
            empty_path,
            session_date=SESSION_DATE,
            run_id="bad-skip-v2-run",
            started_at=WINDOW_START - timedelta(minutes=5),
            software_version="test-commit",
            configuration={"watchlist_kind": "focus"},
        )
        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=WINDOW_START - timedelta(minutes=10),
        ):
            journal.store.record_membership_revision(
                SamplingHierarchyRevision(
                    session_date=SESSION_DATE,
                    revision=0,
                    effective_at=WINDOW_START,
                    uni_symbols=("SPY",),
                    focus_symbols=(),
                    hot_symbols=(),
                    source="unit-test-publisher",
                )
            )
        slot = PollSlot(
            WatchlistKind.FOCUS,
            WINDOW_START.replace(second=5),
        )
        skipped = resolve_provider_poll_slot(
            slot,
            journal,
            dispatched_at=slot.scheduled_at,
        )
        assert isinstance(skipped, SkippedPollSlot)
        journal.record_skipped_slot(skipped)
        with closing(sqlite3.connect(empty_path)) as connection:
            connection.execute(
                "UPDATE poll_slot_skip SET reason = 'corrupted'"
            )
            connection.commit()

        report = audit_quote_journal(empty_path)

        self.assertFalse(report.passed)
        self.assertIn(
            "skip_content_hash",
            {finding.code for finding in report.findings},
        )


if __name__ == "__main__":
    unittest.main()
