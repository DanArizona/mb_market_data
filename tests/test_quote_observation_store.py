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
    HIERARCHY_SCHEMA_VERSION,
    PollRunProvenance,
    QuoteObservationStore,
    QuoteObservationStoreError,
    RecordResult,
    SamplingChannelRevision,
    SessionMismatchError,
    UnknownChannelRevisionError,
    UnsupportedSchemaVersionError,
)
from mb_market_data.sampling_membership import (
    MembershipRevisionError,
    SamplingHierarchyRevision,
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


def make_hierarchy_revision(
    *,
    revision: int = 0,
    effective_at: datetime = datetime(
        2026,
        9,
        9,
        14,
        30,
        tzinfo=UTC,
    ),
    uni_symbols=("AAPL", "NVDA", "SPY"),
    focus_symbols=("AAPL", "NVDA"),
    hot_symbols=("NVDA",),
    reason: str = "test hierarchy",
    published_at: datetime | None = None,
) -> SamplingHierarchyRevision:
    return SamplingHierarchyRevision(
        session_date=SESSION_DATE,
        revision=revision,
        effective_at=effective_at,
        uni_symbols=uni_symbols,
        focus_symbols=focus_symbols,
        hot_symbols=hot_symbols,
        source="test-coordinator",
        reason=reason,
        metadata={"producer": {"name": "unit-test"}},
        published_at=published_at,
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


class TestHierarchyQuoteObservationStore(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "2026-09-09-v2.sqlite3"
        )
        self.store = QuoteObservationStore(
            self.database_path,
            session_date=SESSION_DATE,
            schema_version=HIERARCHY_SCHEMA_VERSION,
        )
        self.store.initialize()
        self.publication_time = datetime(
            2026,
            9,
            9,
            14,
            0,
            tzinfo=UTC,
        )

    def record(
        self,
        revision: SamplingHierarchyRevision,
        *,
        publication_time: datetime | None = None,
    ) -> RecordResult:
        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=(publication_time or self.publication_time),
        ):
            return self.store.record_membership_revision(revision)

    def test_initializes_opt_in_v2_identity_and_tables(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            identity = connection.execute(
                "SELECT schema_version, membership_contract "
                "FROM store_identity"
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertEqual(version, HIERARCHY_SCHEMA_VERSION)
        self.assertEqual(identity, (2, "nested-uni-focus-hot-v1"))
        self.assertIn("sampling_membership_revision", tables)

    def test_default_v1_creation_remains_unchanged(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        legacy = QuoteObservationStore(
            legacy_path,
            session_date=SESSION_DATE,
        )
        legacy.initialize()

        with closing(sqlite3.connect(legacy_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            hierarchy_table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' "
                "AND name = 'sampling_membership_revision'"
            ).fetchone()

        self.assertEqual(version, 1)
        self.assertIsNone(hierarchy_table)

    def test_refuses_cross_version_open_instead_of_migrating(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        QuoteObservationStore(
            legacy_path,
            session_date=SESSION_DATE,
        ).initialize()
        hierarchy_view = QuoteObservationStore(
            legacy_path,
            session_date=SESSION_DATE,
            schema_version=HIERARCHY_SCHEMA_VERSION,
        )

        with self.assertRaisesRegex(
            UnsupportedSchemaVersionError,
            "database is 1, requested 2",
        ):
            hierarchy_view.initialize()

    def test_records_complete_hierarchy_and_empty_optional_channels(
        self,
    ) -> None:
        revision = make_hierarchy_revision(
            focus_symbols=(),
            hot_symbols=(),
        )

        outcome = self.record(revision)

        self.assertEqual(outcome, RecordResult.INSERTED)
        with closing(sqlite3.connect(self.database_path)) as connection:
            header_count = connection.execute(
                "SELECT COUNT(*) FROM sampling_membership_revision"
            ).fetchone()[0]
            channel_rows = connection.execute(
                "SELECT channel, symbol_count "
                "FROM sampling_channel_revision ORDER BY channel"
            ).fetchall()
            member_count = connection.execute(
                "SELECT COUNT(*) FROM sampling_channel_member"
            ).fetchone()[0]

        self.assertEqual(header_count, 1)
        self.assertEqual(
            channel_rows,
            [("focus", 0), ("hot", 0), ("uni", 3)],
        )
        self.assertEqual(member_count, 3)

        stored = self.store.membership_revisions_in_effective_order()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].content_sha256, revision.content_sha256)
        self.assertEqual(stored[0].published_at, self.publication_time)
        self.assertEqual(stored[0].focus_symbols, ())
        self.assertEqual(stored[0].hot_symbols, ())

    def test_retry_is_idempotent_even_after_effective_time(self) -> None:
        revision = make_hierarchy_revision()
        self.assertEqual(self.record(revision), RecordResult.INSERTED)

        retry_time = revision.effective_at + timedelta(days=1)
        self.assertEqual(
            self.record(revision, publication_time=retry_time),
            RecordResult.ALREADY_PRESENT,
        )
        self.assertEqual(
            len(self.store.membership_revisions_in_effective_order()),
            1,
        )

    def test_same_revision_with_different_content_is_rejected(self) -> None:
        self.record(make_hierarchy_revision())

        with self.assertRaisesRegex(DuplicateRecordError, "different content"):
            self.record(
                make_hierarchy_revision(
                    focus_symbols=("AAPL",),
                    hot_symbols=(),
                )
            )

    def test_requires_sequential_non_no_op_revisions(self) -> None:
        first = make_hierarchy_revision()
        self.record(first)

        with self.assertRaisesRegex(MembershipRevisionError, "gap"):
            self.record(
                make_hierarchy_revision(
                    revision=2,
                    effective_at=first.effective_at + timedelta(minutes=2),
                )
            )
        with self.assertRaisesRegex(MembershipRevisionError, "does not change"):
            self.record(make_hierarchy_revision(revision=1))

        second = make_hierarchy_revision(
            revision=1,
            effective_at=first.effective_at + timedelta(minutes=2),
            focus_symbols=("AAPL",),
            hot_symbols=(),
            reason="demote NVDA",
        )
        self.assertEqual(self.record(second), RecordResult.INSERTED)
        self.assertEqual(
            tuple(
                item.revision
                for item in self.store.membership_revisions_in_effective_order()
            ),
            (0, 1),
        )

    def test_store_assigns_publication_time_and_rejects_backdating(
        self,
    ) -> None:
        supplied = make_hierarchy_revision(
            published_at=self.publication_time,
        )
        with self.assertRaisesRegex(ValueError, "assigned by the store"):
            self.store.record_membership_revision(supplied)

        late_publication = datetime(
            2026,
            9,
            9,
            14,
            31,
            tzinfo=UTC,
        )
        with self.assertRaisesRegex(
            MembershipRevisionError,
            "effective_at cannot precede published_at",
        ):
            self.record(
                make_hierarchy_revision(),
                publication_time=late_publication,
            )

    def test_direct_channel_publication_is_rejected_on_v2(self) -> None:
        with self.assertRaisesRegex(
            UnsupportedSchemaVersionError,
            "requires schema version 1",
        ):
            self.store.record_channel_revision(make_revision(make_request()))

        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        legacy = QuoteObservationStore(
            legacy_path,
            session_date=SESSION_DATE,
        )
        legacy.initialize()
        with self.assertRaisesRegex(
            UnsupportedSchemaVersionError,
            "requires schema version 2",
        ):
            legacy.record_membership_revision(make_hierarchy_revision())

    def test_failure_between_channel_writes_rolls_back_everything(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER inject_focus_failure
                BEFORE INSERT ON sampling_channel_revision
                WHEN NEW.channel = 'focus'
                BEGIN
                    SELECT RAISE(ABORT, 'injected focus failure');
                END
                """
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            self.record(make_hierarchy_revision())

        with closing(sqlite3.connect(self.database_path)) as connection:
            counts = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "sampling_membership_revision",
                    "sampling_channel_revision",
                    "sampling_channel_member",
                )
            )
        self.assertEqual(counts, (0, 0, 0))

    def test_old_in_flight_acquisition_keeps_v2_revision_binding(self) -> None:
        request = make_request(revision=0, symbols=("AAPL", "NVDA"))
        first = make_hierarchy_revision(
            effective_at=request.dispatched_at - timedelta(seconds=1),
            uni_symbols=request.symbols,
            focus_symbols=(),
            hot_symbols=(),
        )
        self.record(first)
        second = make_hierarchy_revision(
            revision=1,
            effective_at=request.dispatched_at + timedelta(milliseconds=500),
            uni_symbols=("AAPL", "NVDA", "SPY"),
            focus_symbols=(),
            hot_symbols=(),
            reason="add SPY after r0 dispatch",
        )
        self.record(second)
        run = PollRunProvenance(
            run_id="v2-uni-run",
            started_at=request.dispatched_at - timedelta(minutes=1),
            software_version="dd84675",
            configuration={"schema_version": 2},
        )
        self.store.record_run(run)

        outcome = self.store.record_acquisition(
            run.run_id,
            request,
            make_result(request),
            completed_at=request.dispatched_at + timedelta(seconds=1),
        )

        self.assertEqual(outcome, RecordResult.INSERTED)
        stored = self.store.get_acquisition(request.batch_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.channel_revision, 0)
        self.assertEqual(
            self.store.membership_revisions_in_effective_order()[-1].revision,
            1,
        )

    def test_loader_detects_incomplete_hierarchy_bundle(self) -> None:
        self.record(make_hierarchy_revision())
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "DELETE FROM sampling_channel_revision "
                "WHERE channel = 'hot'"
            )
            connection.commit()

        with self.assertRaisesRegex(
            QuoteObservationStoreError,
            "does not contain exactly",
        ):
            self.store.membership_revisions_in_effective_order()


if __name__ == "__main__":
    unittest.main()
