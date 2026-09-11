from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.polling_journal import PollingJournalSession
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.schwab_quotes import (
    QuoteBatchResult,
    QuoteResult,
    QuoteStatus,
)
from mb_market_data.watchlist_polling import (
    PollRequest,
    PollSlot,
    WatchlistKind,
    WatchlistSnapshot,
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


if __name__ == "__main__":
    unittest.main()
