from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mb_market_data.quote_observation_store import (
    DuplicateRecordError,
    PollRunProvenance,
    QuoteObservationStore,
    RecordResult,
    SamplingChannelRevision,
    SessionMismatchError,
    UnknownChannelRevisionError,
)
from mb_market_data.schwab_quotes import (
    QuoteBatchResult,
    QuoteResult,
    QuoteStatus,
)
from mb_market_data.watchlist_polling import (
    PollRequest,
    PollSlot,
    WatchlistKind,
)


UTC = timezone.utc
SESSION_DATE = date(2026, 9, 9)


def make_request(
    *,
    kind: WatchlistKind = WatchlistKind.UNI,
    second: int = 0,
    minute: int = 31,
    revision: int = 42,
    symbols: tuple[str, ...] = ("AAPL", "NVDA"),
) -> PollRequest:
    scheduled_at = datetime(2026, 9, 9, 14, minute, second, tzinfo=UTC)
    return PollRequest(
        slot=PollSlot(watchlist_kind=kind, scheduled_at=scheduled_at),
        watchlist_revision=revision,
        symbols=symbols,
        dispatched_at=scheduled_at + timedelta(milliseconds=250),
    )


def make_revision(request: PollRequest) -> SamplingChannelRevision:
    return SamplingChannelRevision(
        channel=request.watchlist_kind.value,
        session_date=request.session_date,
        revision=request.watchlist_revision,
        effective_at=request.dispatched_at - timedelta(seconds=1),
        symbols=request.symbols,
        source="test-coordinator",
        reason="test membership",
        metadata={"producer": "unit-test"},
    )


def make_result(
    request: PollRequest,
    *,
    aapl_last_price: float = 230.25,
) -> QuoteBatchResult:
    started_at = request.dispatched_at
    received_at = started_at + timedelta(milliseconds=400)
    results: list[QuoteResult] = []

    for symbol in request.symbols:
        if symbol == "AAPL":
            results.append(
                QuoteResult(
                    symbol=symbol,
                    status=QuoteStatus.QUOTE,
                    quote={
                        "symbol": symbol,
                        "assetMainType": "EQUITY",
                        "realtime": True,
                        "quote": {
                            "bidSize": 200,
                            "askSize": 300,
                            "lastPrice": aapl_last_price,
                            "totalVolume": 12_345_678,
                            "securityStatus": "Normal",
                            "tradeTime": 1_789_000_000_000,
                        },
                        "regular": {"regularMarketLastPrice": 229.5},
                        "fundamental": {
                            "sharesOutstanding": 15_000_000_000,
                            "avg10DaysVolume": 52_000_000,
                            "avg1YearVolume": 61_000_000,
                        },
                        "reference": {
                            "exchange": "Q",
                            "exchangeName": "NASDAQ",
                            "description": "APPLE INC",
                        },
                    },
                    detail=None,
                    batch_number=1,
                    request_started_at_utc=started_at,
                    response_received_at_utc=received_at,
                )
            )
        else:
            results.append(
                QuoteResult(
                    symbol=symbol,
                    status=QuoteStatus.INVALID,
                    quote=None,
                    detail="Reported by Schwab as invalid",
                    batch_number=1,
                    request_started_at_utc=started_at,
                    response_received_at_utc=received_at,
                )
            )

    return QuoteBatchResult(
        results=tuple(results),
        request_count=1,
        batch_size=400,
        unexpected_symbols=("EXTRA",),
    )


class TestQuoteObservationStore(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "2026-09-09.sqlite3"
        )
        self.store = QuoteObservationStore(
            self.database_path,
            session_date=SESSION_DATE,
        )
        self.store.initialize()
        self.run = PollRunProvenance(
            run_id="run-uni-1",
            started_at=datetime(2026, 9, 9, 14, 30, tzinfo=UTC),
            software_version="67218a9",
            configuration={"fields": "all", "batch_size": 400},
            host="MasterBot",
            command=("python", "probe_universe_quote_watch.py"),
        )
        self.store.record_run(self.run)

    def prepare(self, request: PollRequest) -> QuoteBatchResult:
        self.store.record_channel_revision(make_revision(request))
        return make_result(request)

    def test_initializes_daily_versioned_wal_database(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            stored_date = connection.execute(
                "SELECT session_date FROM store_identity"
            ).fetchone()[0]

        self.assertEqual(version, 1)
        self.assertEqual(journal_mode.casefold(), "wal")
        self.assertEqual(stored_date, SESSION_DATE.isoformat())

    def test_existing_database_rejects_another_session(self) -> None:
        wrong_store = QuoteObservationStore(
            self.database_path,
            session_date=date(2026, 9, 10),
        )
        with self.assertRaises(SessionMismatchError):
            wrong_store.initialize()

    def test_run_provenance_is_immutable_and_idempotent(self) -> None:
        self.assertEqual(
            self.store.record_run(self.run),
            RecordResult.ALREADY_PRESENT,
        )
        changed = PollRunProvenance(
            run_id=self.run.run_id,
            started_at=self.run.started_at,
            software_version="different",
            configuration=self.run.configuration,
        )
        with self.assertRaises(DuplicateRecordError):
            self.store.record_run(changed)

    def test_store_explicitly_closes_each_connection(self) -> None:
        real_connect = sqlite3.connect
        opened_connections: list[sqlite3.Connection] = []

        def tracked_connect(*args, **kwargs) -> sqlite3.Connection:
            connection = real_connect(*args, **kwargs)
            opened_connections.append(connection)
            return connection

        with patch(
            "mb_market_data.quote_observation_store.sqlite3.connect",
            side_effect=tracked_connect,
        ):
            self.assertIsNone(self.store.get_acquisition("not-present"))

        self.assertEqual(len(opened_connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened_connections[0].execute("SELECT 1")

    def test_channel_names_are_data_and_hot_needs_no_schema_change(self) -> None:
        hot = SamplingChannelRevision(
            channel="HOT",
            session_date=SESSION_DATE,
            revision=1,
            effective_at=datetime(2026, 9, 9, 15, 0, tzinfo=UTC),
            symbols=("IPO",),
            source="ipo-monitor",
            reason="new listing",
        )
        outcome = self.store.record_channel_revision(hot)

        self.assertEqual(outcome, RecordResult.INSERTED)
        with closing(sqlite3.connect(self.database_path)) as connection:
            stored = connection.execute(
                "SELECT channel, reason FROM sampling_channel_revision "
                "WHERE channel = 'hot'"
            ).fetchone()
        self.assertEqual(stored, ("hot", "new listing"))

    def test_channel_revision_is_immutable_and_idempotent(self) -> None:
        request = make_request()
        revision = make_revision(request)
        self.assertEqual(
            self.store.record_channel_revision(revision),
            RecordResult.INSERTED,
        )
        self.assertEqual(
            self.store.record_channel_revision(revision),
            RecordResult.ALREADY_PRESENT,
        )
        changed = SamplingChannelRevision(
            channel=revision.channel,
            session_date=revision.session_date,
            revision=revision.revision,
            effective_at=revision.effective_at,
            symbols=("AAPL", "MSFT"),
            source=revision.source,
        )
        with self.assertRaises(DuplicateRecordError):
            self.store.record_channel_revision(changed)

    def test_promotion_is_a_new_revision_without_rewriting_uni(self) -> None:
        uni = SamplingChannelRevision(
            channel="uni",
            session_date=SESSION_DATE,
            revision=7,
            effective_at=datetime(2026, 9, 9, 14, 30, tzinfo=UTC),
            symbols=("AAPL", "IPO"),
            source="daily-selector",
        )
        focus_before = SamplingChannelRevision(
            channel="focus",
            session_date=SESSION_DATE,
            revision=20,
            effective_at=datetime(2026, 9, 9, 14, 31, tzinfo=UTC),
            symbols=("AAPL",),
            source="coordinator",
        )
        focus_after = SamplingChannelRevision(
            channel="focus",
            session_date=SESSION_DATE,
            revision=21,
            effective_at=datetime(2026, 9, 9, 14, 32, tzinfo=UTC),
            symbols=("AAPL", "IPO"),
            source="ipo-monitor",
            reason="promote IPO to focus",
        )
        for revision in (focus_after, uni, focus_before):
            self.store.record_channel_revision(revision)

        replay = self.store.channel_revisions_in_effective_order()
        self.assertEqual(replay, (uni, focus_before, focus_after))
        self.assertIn("IPO", replay[0].symbols)
        self.assertNotIn("IPO", replay[1].symbols)
        self.assertIn("IPO", replay[2].symbols)

    def test_records_normalized_header_and_every_symbol(self) -> None:
        request = make_request()
        result = self.prepare(request)
        completed_at = request.dispatched_at + timedelta(seconds=1)

        outcome = self.store.record_acquisition(
            self.run.run_id,
            request,
            result,
            completed_at=completed_at,
        )

        self.assertEqual(outcome, RecordResult.INSERTED)
        acquisition = self.store.get_acquisition(request.batch_id)
        self.assertIsNotNone(acquisition)
        assert acquisition is not None
        self.assertEqual(acquisition.run_id, self.run.run_id)
        self.assertEqual(acquisition.channel, "uni")
        self.assertEqual(acquisition.channel_revision, 42)
        self.assertEqual(acquisition.session_date, SESSION_DATE)
        self.assertEqual(acquisition.unexpected_symbols, ("EXTRA",))

        observations = self.store.get_observations(request.batch_id)
        self.assertEqual(
            tuple(item.symbol for item in observations),
            request.symbols,
        )
        self.assertEqual(observations[0].channel, "uni")
        self.assertEqual(observations[0].values["quote_last_price"], 230.25)
        self.assertEqual(
            observations[0].values["quote_total_volume"],
            12_345_678,
        )
        self.assertEqual(observations[0].values["realtime"], 1)
        self.assertEqual(
            observations[0].values["avg_10_days_volume"],
            52_000_000,
        )
        self.assertEqual(
            observations[0].values["description"],
            "APPLE INC",
        )
        self.assertNotIn("quote_json", observations[0].values)
        self.assertEqual(observations[1].status, "invalid")
        self.assertTrue(
            all(value is None for value in observations[1].values.values())
        )

    def test_identical_acquisition_replay_is_idempotent(self) -> None:
        request = make_request()
        result = self.prepare(request)
        completed_at = request.dispatched_at + timedelta(seconds=1)

        first = self.store.record_acquisition(
            self.run.run_id,
            request,
            result,
            completed_at=completed_at,
        )
        second = self.store.record_acquisition(
            self.run.run_id,
            request,
            result,
            completed_at=completed_at,
        )

        self.assertEqual(first, RecordResult.INSERTED)
        self.assertEqual(second, RecordResult.ALREADY_PRESENT)
        self.assertEqual(len(self.store.get_observations(request.batch_id)), 2)

    def test_conflicting_acquisition_replay_is_rejected(self) -> None:
        request = make_request()
        result = self.prepare(request)
        completed_at = request.dispatched_at + timedelta(seconds=1)
        self.store.record_acquisition(
            self.run.run_id,
            request,
            result,
            completed_at=completed_at,
        )

        with self.assertRaises(DuplicateRecordError):
            self.store.record_acquisition(
                self.run.run_id,
                request,
                make_result(request, aapl_last_price=999.0),
                completed_at=completed_at,
            )

    def test_one_acquisition_is_allowed_per_channel_slot(self) -> None:
        first = make_request(revision=42, symbols=("AAPL",))
        replacement = make_request(revision=43, symbols=("AAPL",))
        for request in (first, replacement):
            self.store.record_channel_revision(make_revision(request))

        self.store.record_acquisition(
            self.run.run_id,
            first,
            make_result(first),
            completed_at=first.dispatched_at + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(DuplicateRecordError, "slot"):
            self.store.record_acquisition(
                self.run.run_id,
                replacement,
                make_result(replacement),
                completed_at=(
                    replacement.dispatched_at + timedelta(seconds=1)
                ),
            )

    def test_acquisition_requires_recorded_channel_revision(self) -> None:
        request = make_request()
        with self.assertRaises(UnknownChannelRevisionError):
            self.store.record_acquisition(
                self.run.run_id,
                request,
                make_result(request),
                completed_at=request.dispatched_at + timedelta(seconds=1),
            )

    def test_acquisition_rejects_revision_not_yet_effective(self) -> None:
        request = make_request()
        future_revision = SamplingChannelRevision(
            channel="uni",
            session_date=SESSION_DATE,
            revision=request.watchlist_revision,
            effective_at=request.dispatched_at + timedelta(seconds=1),
            symbols=request.symbols,
            source="test-coordinator",
        )
        self.store.record_channel_revision(future_revision)

        with self.assertRaisesRegex(DuplicateRecordError, "effective"):
            self.store.record_acquisition(
                self.run.run_id,
                request,
                make_result(request),
                completed_at=request.dispatched_at + timedelta(seconds=2),
            )

    def test_recent_observations_span_uni_and_focus(self) -> None:
        uni_request = make_request(symbols=("AAPL",))
        focus_request = make_request(
            kind=WatchlistKind.FOCUS,
            second=5,
            revision=43,
            symbols=("AAPL",),
        )

        for request, price in ((uni_request, 230.0), (focus_request, 231.0)):
            self.store.record_channel_revision(make_revision(request))
            self.store.record_acquisition(
                self.run.run_id,
                request,
                make_result(request, aapl_last_price=price),
                completed_at=request.dispatched_at + timedelta(seconds=1),
            )

        observations = self.store.recent_observations("aapl", limit=2)
        self.assertEqual(
            tuple(item.channel for item in observations),
            ("focus", "uni"),
        )
        self.assertEqual(
            tuple(item.values["quote_last_price"] for item in observations),
            (231.0, 230.0),
        )
        acquisitions = self.store.acquisitions_in_replay_order()
        self.assertEqual(
            tuple(item.channel for item in acquisitions),
            ("uni", "focus"),
        )

    def test_two_store_instances_can_write_same_daily_database(self) -> None:
        second_store = QuoteObservationStore(
            self.database_path,
            session_date=SESSION_DATE,
        )
        second_store.initialize()
        uni_request = make_request(symbols=("AAPL",))
        focus_request = make_request(
            kind=WatchlistKind.FOCUS,
            second=5,
            symbols=("NVDA",),
        )
        self.store.record_channel_revision(make_revision(uni_request))
        second_store.record_channel_revision(make_revision(focus_request))

        def record(
            store: QuoteObservationStore,
            request: PollRequest,
        ) -> RecordResult:
            return store.record_acquisition(
                self.run.run_id,
                request,
                make_result(request),
                completed_at=request.dispatched_at + timedelta(seconds=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(record, self.store, uni_request),
                executor.submit(record, second_store, focus_request),
            )
            outcomes = {future.result() for future in futures}

        self.assertEqual(outcomes, {RecordResult.INSERTED})
        self.assertEqual(len(self.store.get_observations(uni_request.batch_id)), 1)
        self.assertEqual(
            len(self.store.get_observations(focus_request.batch_id)),
            1,
        )


if __name__ == "__main__":
    unittest.main()
