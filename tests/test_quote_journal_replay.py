from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    QuoteAcquisitionEvent,
    QuoteJournalReplayReader,
    ReplayIntegrityError,
    paced_replay,
)
from mb_market_data.quote_observation_store import (
    PollRunProvenance,
    QuoteObservationStore,
    SamplingChannelRevision,
    UnsupportedSchemaVersionError,
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


def make_revision(
    kind: WatchlistKind,
    *,
    effective_at: datetime,
    revision: int = 0,
    symbols: tuple[str, ...] = ("SPY",),
) -> SamplingChannelRevision:
    return SamplingChannelRevision(
        channel=kind.value,
        session_date=SESSION_DATE,
        revision=revision,
        effective_at=effective_at,
        symbols=symbols,
        source="unit-test",
    )


def make_request(
    kind: WatchlistKind,
    *,
    scheduled_at: datetime,
    revision: int = 0,
    symbols: tuple[str, ...] = ("SPY",),
) -> PollRequest:
    return PollRequest(
        slot=PollSlot(kind, scheduled_at),
        watchlist_revision=revision,
        symbols=symbols,
        dispatched_at=scheduled_at,
    )


def make_result(
    request: PollRequest,
    *,
    response_received_at: datetime,
) -> QuoteBatchResult:
    return QuoteBatchResult(
        results=tuple(
            QuoteResult(
                symbol=symbol,
                status=QuoteStatus.QUOTE,
                quote={
                    "symbol": symbol,
                    "quote": {"lastPrice": 100.0 + ordinal},
                },
                detail=None,
                batch_number=1,
                request_started_at_utc=request.dispatched_at,
                response_received_at_utc=response_received_at,
            )
            for ordinal, symbol in enumerate(request.symbols)
        ),
        request_count=1,
        batch_size=400,
        unexpected_symbols=(),
    )


class ReplayJournalFixture(unittest.TestCase):
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
            run_id="replay-test-run",
            started_at=datetime(2026, 9, 9, 13, 29, tzinfo=UTC),
            software_version="test",
            configuration={},
        )
        self.store.record_run(self.run)

    def record_acquisition(
        self,
        request: PollRequest,
        *,
        completed_at: datetime,
    ) -> None:
        self.store.record_acquisition(
            self.run.run_id,
            request,
            make_result(request, response_received_at=completed_at),
            completed_at=completed_at,
        )


class TestQuoteJournalReplayReader(ReplayJournalFixture):
    def test_replays_revisions_and_complete_acquisitions_causally(self) -> None:
        effective_at = datetime(2026, 9, 9, 13, 29, tzinfo=UTC)
        for kind in (WatchlistKind.UNI, WatchlistKind.FOCUS):
            self.store.record_channel_revision(
                make_revision(kind, effective_at=effective_at)
            )

        # Uni was scheduled first but completed after Focus.  Consumers could
        # know about the Focus result first, so replay must publish it first.
        uni = make_request(
            WatchlistKind.UNI,
            scheduled_at=datetime(2026, 9, 9, 13, 30, 0, tzinfo=UTC),
        )
        focus = make_request(
            WatchlistKind.FOCUS,
            scheduled_at=datetime(2026, 9, 9, 13, 30, 5, tzinfo=UTC),
        )
        self.record_acquisition(
            uni,
            completed_at=datetime(2026, 9, 9, 13, 30, 10, tzinfo=UTC),
        )
        self.record_acquisition(
            focus,
            completed_at=datetime(2026, 9, 9, 13, 30, 6, tzinfo=UTC),
        )

        reader = QuoteJournalReplayReader(self.database_path)
        events = tuple(reader.events())

        self.assertEqual(reader.session_date, SESSION_DATE)
        self.assertEqual(
            tuple(event.channel for event in events),
            ("focus", "uni", "focus", "uni"),
        )
        self.assertIsInstance(events[0], ChannelRevisionEvent)
        self.assertIsInstance(events[1], ChannelRevisionEvent)
        self.assertIsInstance(events[2], QuoteAcquisitionEvent)
        self.assertIsInstance(events[3], QuoteAcquisitionEvent)
        focus_event = events[2]
        assert isinstance(focus_event, QuoteAcquisitionEvent)
        self.assertEqual(focus_event.acquisition.acquisition_id, focus.batch_id)
        self.assertEqual(len(focus_event.observations), 1)
        self.assertEqual(focus_event.observations[0].symbol, "SPY")
        self.assertEqual(
            focus_event.observations[0].values["quote_last_price"],
            100.0,
        )

    def test_revision_wins_same_availability_time_tie(self) -> None:
        instant = datetime(2026, 9, 9, 13, 30, 5, tzinfo=UTC)
        revision = make_revision(
            WatchlistKind.FOCUS,
            effective_at=instant,
        )
        self.store.record_channel_revision(revision)
        request = make_request(
            WatchlistKind.FOCUS,
            scheduled_at=instant,
        )
        self.record_acquisition(request, completed_at=instant)

        events = tuple(QuoteJournalReplayReader(self.database_path).events())

        self.assertIsInstance(events[0], ChannelRevisionEvent)
        self.assertIsInstance(events[1], QuoteAcquisitionEvent)

    def test_same_time_revisions_are_ordered_numerically(self) -> None:
        instant = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
        for revision in (10, 2):
            self.store.record_channel_revision(
                make_revision(
                    WatchlistKind.FOCUS,
                    effective_at=instant,
                    revision=revision,
                )
            )

        events = tuple(QuoteJournalReplayReader(self.database_path).events())

        self.assertEqual(
            tuple(event.revision.revision for event in events),
            (2, 10),
        )

    def test_missing_observation_is_reported_as_corruption(self) -> None:
        instant = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
        self.store.record_channel_revision(
            make_revision(
                WatchlistKind.UNI,
                effective_at=instant,
                symbols=("SPY", "QQQ"),
            )
        )
        request = make_request(
            WatchlistKind.UNI,
            scheduled_at=instant,
            symbols=("SPY", "QQQ"),
        )
        self.record_acquisition(
            request,
            completed_at=instant + timedelta(seconds=1),
        )
        # A sqlite3 connection context commits or rolls back but does not
        # close.  Explicit closing is required before Windows can remove the
        # temporary database file.
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "DELETE FROM quote_observation WHERE symbol = 'QQQ'"
            )
            connection.commit()

        reader = QuoteJournalReplayReader(self.database_path)
        with self.assertRaisesRegex(
            ReplayIntegrityError,
            "declares 2 observations but contains 1",
        ):
            tuple(reader.events())

    def test_rejects_unsupported_database_schema(self) -> None:
        other_path = Path(self.temporary_directory.name) / "other.sqlite3"
        with closing(sqlite3.connect(other_path)) as connection:
            connection.execute("PRAGMA user_version = 999")
            connection.commit()

        with self.assertRaises(UnsupportedSchemaVersionError):
            QuoteJournalReplayReader(other_path)

    def test_rejects_versioned_database_without_identity_table(self) -> None:
        other_path = Path(self.temporary_directory.name) / "other.sqlite3"
        with closing(sqlite3.connect(other_path)) as connection:
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        with self.assertRaisesRegex(
            ReplayIntegrityError,
            "no readable store_identity table",
        ):
            QuoteJournalReplayReader(other_path)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def revision_event(at: datetime, revision: int) -> ChannelRevisionEvent:
    return ChannelRevisionEvent(
        make_revision(
            WatchlistKind.FOCUS,
            effective_at=at,
            revision=revision,
        )
    )


class TestPacedReplay(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
        self.events = tuple(
            revision_event(self.start + timedelta(minutes=index), index)
            for index in range(3)
        )

    def test_immediate_mode_does_not_wait(self) -> None:
        clock = FakeClock()

        replayed = tuple(
            paced_replay(
                self.events,
                sleeper=clock.sleep,
                monotonic=clock.monotonic,
            )
        )

        self.assertEqual(replayed, self.events)
        self.assertEqual(clock.sleeps, [])

    def test_scaled_mode_uses_absolute_deadlines_without_drift(self) -> None:
        clock = FakeClock()
        replay = paced_replay(
            self.events,
            speed=60,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        self.assertEqual(next(replay), self.events[0])
        clock.now += 0.25  # Simulate consumer work after the first event.
        self.assertEqual(next(replay), self.events[1])
        clock.now += 0.40  # More consumer work after the second event.
        self.assertEqual(next(replay), self.events[2])
        with self.assertRaises(StopIteration):
            next(replay)

        self.assertEqual(len(clock.sleeps), 2)
        self.assertAlmostEqual(clock.sleeps[0], 0.75)
        self.assertAlmostEqual(clock.sleeps[1], 0.60)
        self.assertEqual(clock.now, 102.0)

    def test_step_mode_waits_before_each_event(self) -> None:
        waited: list[str] = []

        replayed = tuple(
            paced_replay(
                self.events,
                step_waiter=lambda event: waited.append(event.event_id),
            )
        )

        self.assertEqual(replayed, self.events)
        self.assertEqual(waited, [event.event_id for event in self.events])

    def test_rejects_invalid_or_conflicting_pacing(self) -> None:
        with self.assertRaises(ValueError):
            tuple(paced_replay(self.events, speed=0))
        with self.assertRaises(TypeError):
            tuple(paced_replay(self.events, speed=True))
        with self.assertRaises(ValueError):
            tuple(
                paced_replay(
                    self.events,
                    speed=1,
                    step_waiter=lambda event: None,
                )
            )

    def test_rejects_out_of_order_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "availability order"):
            tuple(paced_replay(reversed(self.events)))
