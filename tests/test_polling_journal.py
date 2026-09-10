from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.polling_journal import (
    PollingJournalSession,
    daily_quote_journal_path,
)
from mb_market_data.quote_observation_store import RecordResult
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
SESSION_START = datetime(2026, 9, 10, 9, 30, tzinfo=ET)


def make_snapshot(
    kind: WatchlistKind,
    symbols: tuple[str, ...],
) -> WatchlistSnapshot:
    return WatchlistSnapshot(
        watchlist_kind=kind,
        session_date=SESSION_DATE,
        revision=0,
        effective_at=SESSION_START,
        symbols=symbols,
    )


def make_request(snapshot: WatchlistSnapshot) -> PollRequest:
    second = 0 if snapshot.watchlist_kind == WatchlistKind.UNI else 5
    scheduled_at = datetime(2026, 9, 10, 9, 31, second, tzinfo=ET)
    return PollRequest(
        slot=PollSlot(
            watchlist_kind=snapshot.watchlist_kind,
            scheduled_at=scheduled_at,
        ),
        watchlist_revision=snapshot.revision,
        symbols=snapshot.symbols,
        dispatched_at=scheduled_at + timedelta(milliseconds=1),
    )


def make_result(request: PollRequest) -> QuoteBatchResult:
    received_at = request.dispatched_at + timedelta(milliseconds=250)
    return QuoteBatchResult(
        results=tuple(
            QuoteResult(
                symbol=symbol,
                status=QuoteStatus.QUOTE,
                quote={
                    "symbol": symbol,
                    "assetMainType": "EQUITY",
                    "realtime": True,
                    "quote": {
                        "lastPrice": 100.0 + ordinal,
                        "totalVolume": 1_000_000 + ordinal,
                    },
                },
                detail=None,
                batch_number=1,
                request_started_at_utc=request.dispatched_at,
                response_received_at_utc=received_at,
            )
            for ordinal, symbol in enumerate(request.symbols)
        ),
        request_count=1,
        batch_size=400,
        unexpected_symbols=(),
    )


def start_journal(
    database_path: Path,
    *,
    run_id: str,
    started_at: datetime,
    snapshot: WatchlistSnapshot,
) -> PollingJournalSession:
    return PollingJournalSession.start(
        database_path,
        run_id=run_id,
        started_at=started_at,
        software_version="test-version",
        configuration={"fields": "all", "batch_size": 400},
        snapshot=snapshot,
        revision_source="unit-test",
        revision_reason="static probe input",
        revision_metadata={"probe": "universe_quote_watch"},
        host="MasterBot",
        command=("python", "probe_universe_quote_watch.py"),
    )


class TestPollingJournalSession(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = daily_quote_journal_path(
            self.temporary_directory.name,
            SESSION_DATE,
        )

    def test_daily_path_uses_session_date(self) -> None:
        self.assertEqual(
            self.database_path,
            Path(self.temporary_directory.name) / "2026-09-10.sqlite3",
        )

    def test_records_run_revision_and_acquisition(self) -> None:
        snapshot = make_snapshot(WatchlistKind.UNI, ("SPY", "QQQ"))
        journal = start_journal(
            self.database_path,
            run_id="uni-run-1",
            started_at=SESSION_START - timedelta(minutes=5),
            snapshot=snapshot,
        )
        request = make_request(snapshot)

        outcome = journal.record_acquisition(
            request,
            make_result(request),
            completed_at=request.dispatched_at + timedelta(milliseconds=250),
        )

        self.assertEqual(journal.run_record_result, RecordResult.INSERTED)
        self.assertEqual(
            journal.revision_record_result,
            RecordResult.INSERTED,
        )
        self.assertEqual(outcome, RecordResult.INSERTED)
        acquisition = journal.store.get_acquisition(request.batch_id)
        self.assertIsNotNone(acquisition)
        assert acquisition is not None
        self.assertEqual(acquisition.channel, "uni")
        observations = journal.store.get_observations(request.batch_id)
        self.assertEqual(
            tuple(item.symbol for item in observations),
            ("SPY", "QQQ"),
        )

    def test_same_day_restart_reuses_static_revision(self) -> None:
        snapshot = make_snapshot(WatchlistKind.FOCUS, ("SPY", "NVDA"))
        first = start_journal(
            self.database_path,
            run_id="focus-run-1",
            started_at=SESSION_START - timedelta(minutes=5),
            snapshot=snapshot,
        )
        second = start_journal(
            self.database_path,
            run_id="focus-run-2",
            started_at=SESSION_START + timedelta(minutes=10),
            snapshot=snapshot,
        )

        self.assertEqual(
            first.revision_record_result,
            RecordResult.INSERTED,
        )
        self.assertEqual(
            second.revision_record_result,
            RecordResult.ALREADY_PRESENT,
        )
        self.assertEqual(second.run_record_result, RecordResult.INSERTED)

    def test_failed_acquisition_leaves_no_partial_record(self) -> None:
        snapshot = make_snapshot(WatchlistKind.UNI, ("SPY", "QQQ"))
        journal = start_journal(
            self.database_path,
            run_id="uni-run-1",
            started_at=SESSION_START - timedelta(minutes=5),
            snapshot=snapshot,
        )
        request = make_request(snapshot)
        result = make_result(request)
        wrong_order = replace(result, results=tuple(reversed(result.results)))

        with self.assertRaises(ValueError):
            journal.record_acquisition(
                request,
                wrong_order,
                completed_at=(
                    request.dispatched_at + timedelta(milliseconds=250)
                ),
            )

        self.assertIsNone(journal.store.get_acquisition(request.batch_id))
        self.assertEqual(journal.store.get_observations(request.batch_id), ())

    def test_uni_and_focus_write_the_same_daily_database(self) -> None:
        uni_snapshot = make_snapshot(WatchlistKind.UNI, ("SPY", "QQQ"))
        focus_snapshot = make_snapshot(
            WatchlistKind.FOCUS,
            ("AAPL", "NVDA"),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            journals = tuple(
                executor.map(
                    lambda values: start_journal(
                        self.database_path,
                        run_id=values[0],
                        started_at=values[1],
                        snapshot=values[2],
                    ),
                    (
                        (
                            "uni-run-1",
                            SESSION_START - timedelta(minutes=5),
                            uni_snapshot,
                        ),
                        (
                            "focus-run-1",
                            SESSION_START - timedelta(minutes=4),
                            focus_snapshot,
                        ),
                    ),
                )
            )

        uni_journal, focus_journal = journals
        uni_request = make_request(uni_snapshot)
        focus_request = make_request(focus_snapshot)

        def record(
            journal: PollingJournalSession,
            request: PollRequest,
        ) -> RecordResult:
            return journal.record_acquisition(
                request,
                make_result(request),
                completed_at=(
                    request.dispatched_at + timedelta(milliseconds=250)
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(
                    lambda item: record(*item),
                    (
                        (uni_journal, uni_request),
                        (focus_journal, focus_request),
                    ),
                )
            )

        self.assertEqual(
            outcomes,
            (RecordResult.INSERTED, RecordResult.INSERTED),
        )
        acquisitions = uni_journal.store.acquisitions_in_replay_order()
        self.assertEqual(
            tuple(item.channel for item in acquisitions),
            ("uni", "focus"),
        )


if __name__ == "__main__":
    unittest.main()
