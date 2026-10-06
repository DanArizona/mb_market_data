from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mb_market_data.observation_overlay import MembershipBand, ObservationOverlayProjector
from mb_market_data.quote_dashboard_live import QuoteDashboardLiveController
from mb_market_data.quote_event_state import QuoteEventStateProjector
from mb_market_data.quote_journal_live import (
    LiveJournalCursor, QuoteJournalLiveReader, header_event_id, header_sort_key,
)
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader, ReplayIntegrityError
from mb_market_data.quote_observation_store import PollRunProvenance, QuoteObservationStore
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
from mb_market_data.watchlist_polling import PollRequest, PollSlot, WatchlistKind

UTC = timezone.utc
SESSION = date(2026, 10, 6)
BOUNDARY = datetime(2026, 10, 6, 17, 30, tzinfo=UTC)


class LiveFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "2026-10-06.sqlite3"
        self.store = QuoteObservationStore(self.path, session_date=SESSION, schema_version=2)
        self.store.initialize()
        self.store.record_run(PollRunProvenance(
            run_id="offline-fixture", started_at=BOUNDARY - timedelta(minutes=3),
            software_version="test", configuration={},
        ))
        self.publish(0, -120, ())
        self.publish(1, -120, ("OTHER",))
        self.clock = BOUNDARY

    def publish(self, revision, seconds, focus):
        with patch("mb_market_data.quote_observation_store._utc_now",
                   return_value=BOUNDARY + timedelta(seconds=seconds - 1)):
            self.store.record_membership_revision(SamplingHierarchyRevision(
                session_date=SESSION, revision=revision,
                effective_at=BOUNDARY + timedelta(seconds=seconds),
                uni_symbols=("OTHER", "TEST"), focus_symbols=focus, hot_symbols=(),
                source="offline-fixture", reason=f"r{revision}",
            ))

    def acquire(self, kind=WatchlistKind.UNI, *, scheduled=0, completed=2,
                revision=1, symbols=("OTHER", "TEST"), status=QuoteStatus.QUOTE):
        start = BOUNDARY + timedelta(seconds=scheduled)
        end = BOUNDARY + timedelta(seconds=completed)
        request = PollRequest(PollSlot(kind, start), revision, symbols, start)
        result = QuoteBatchResult(
            results=tuple(QuoteResult(
                symbol=symbol, status=status,
                quote={"symbol": symbol, "quote": {"lastPrice": 100 + completed}} if status == QuoteStatus.QUOTE else None,
                detail=None if status == QuoteStatus.QUOTE else "fixture outcome",
                batch_number=1, request_started_at_utc=start, response_received_at_utc=end,
            ) for symbol in symbols),
            request_count=1, batch_size=400, unexpected_symbols=(),
        )
        self.store.record_acquisition("offline-fixture", request, result, completed_at=end)
        return request

    def controller(self, limit=32):
        return QuoteDashboardLiveController(
            reader=QuoteJournalLiveReader(self.path), symbol="TEST",
            now=lambda: self.clock, batch_limit=limit,
        )

    def drain(self, controller):
        for _ in range(100):
            snapshot = controller.tick()
            self.assertIsNone(snapshot.source_error)
            if not snapshot.catching_up:
                return snapshot
        self.fail("controller did not catch up")

    def assert_replay_equal(self, snapshot):
        state = QuoteEventStateProjector(session_date=SESSION)
        overlay = ObservationOverlayProjector(symbol="TEST", session_date=SESSION)
        for event in QuoteJournalReplayReader(self.path).events():
            if event.available_at_utc <= snapshot.replay_time_utc:
                state.apply(event)
                overlay.apply(event)
        self.assertEqual(snapshot.state, state.snapshot())
        self.assertEqual(snapshot.observation_overlay, overlay.snapshot(snapshot.replay_time_utc))


class TestLiveReader(LiveFixture):
    def test_bounded_pages_match_replay_and_repeat_cursor_is_empty(self):
        self.publish(2, 1, ("OTHER", "TEST"))
        self.acquire(WatchlistKind.FOCUS, scheduled=5, completed=5.3, revision=2)
        self.acquire(completed=2.47)  # earlier completion, later insertion
        reader = QuoteJournalLiveReader(self.path)
        cursor, headers = LiveJournalCursor(), []
        for _ in range(5):
            page = reader.read_page(cursor, limit=1)
            self.assertLessEqual(len(page.headers), 2)
            headers.extend(page.headers)
            cursor = page.cursor
            if page.caught_up:
                break
        else:
            self.fail("reader did not catch up")
        actual = reader.load_events(sorted(headers, key=header_sort_key))
        self.assertEqual(actual, tuple(QuoteJournalReplayReader(self.path).events()))
        self.assertEqual(reader.read_page(cursor).headers, ())
        self.assertEqual(len({header_event_id(h) for h in headers}), len(headers))

    def test_uncommitted_acquisition_is_hidden_and_commit_is_found(self):
        reader = QuoteJournalLiveReader(self.path)
        page = reader.read_page(LiveJournalCursor())
        connection = self.store._connect()
        self.addCleanup(connection.close)

        @contextmanager
        def held_transaction():
            yield connection

        with patch.object(self.store, "_connection", held_transaction):
            self.acquire()
        self.assertEqual(reader.read_page(page.cursor).headers, ())
        connection.commit()
        committed = reader.read_page(page.cursor)
        self.assertEqual(len(committed.headers), 1)
        self.assertEqual(len(reader.load_events(committed.headers)[0].observations), 2)

    def test_detects_truncated_cursor_and_replaced_identity(self):
        self.acquire()
        reader = QuoteJournalLiveReader(self.path)
        cursor = reader.read_page(LiveJournalCursor()).cursor
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM quote_acquisition")
        with self.assertRaisesRegex(ReplayIntegrityError, "anchor"):
            reader.read_page(cursor)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE store_identity SET created_at_utc = 'changed'")
        with self.assertRaisesRegex(ReplayIntegrityError, "identity changed"):
            reader.read_page(LiveJournalCursor())

    def test_detects_incomplete_acquisition_before_emitting_event(self):
        self.acquire()
        reader = QuoteJournalLiveReader(self.path)
        page = reader.read_page(LiveJournalCursor())
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM quote_observation WHERE symbol = 'TEST'")
        with self.assertRaisesRegex(ReplayIntegrityError, "contains 1"):
            reader.load_events(page.headers)

    def test_reader_does_not_create_missing_database_or_open_write_connection(self):
        with self.assertRaises(FileNotFoundError):
            QuoteJournalLiveReader(self.path.with_name("missing.sqlite3"))
        self.assertFalse(self.path.with_name("missing.sqlite3").exists())
        reader = QuoteJournalLiveReader(self.path)
        with patch.object(reader._store, "_connect", side_effect=AssertionError("write connection")):
            reader.load_events(reader.read_page(LiveJournalCursor()).headers)
        with reader._connection() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM poll_run")


class TestLiveController(LiveFixture):
    def test_late_commit_rebuilds_without_duplicate_points_and_matches_replay(self):
        controller = self.controller(limit=1)
        self.drain(controller)
        self.publish(2, 1, ("OTHER", "TEST"))
        self.acquire(WatchlistKind.FOCUS, scheduled=5, completed=5.3, revision=2)
        self.clock = BOUNDARY + timedelta(seconds=10)
        before = self.drain(controller)
        self.assertEqual(len(before.observation_overlay.quote_points), 1)
        self.acquire(completed=2.47)
        after = self.drain(controller)
        self.assertEqual([p.channel for p in after.observation_overlay.quote_points], ["uni", "focus"])
        self.assertEqual(after.observation_overlay.quote_points[0].channel_revision, 1)
        self.assert_replay_equal(after)
        self.assertEqual(self.drain(controller), after)

    def test_future_membership_waits_for_effective_time_without_new_commit(self):
        self.publish(2, 1, ("OTHER", "TEST"))
        controller = self.controller()
        self.assertEqual(self.drain(controller).observation_overlay.current_band, MembershipBand.UNI)
        self.clock = BOUNDARY + timedelta(seconds=1)
        at_boundary = self.drain(controller)
        self.assertEqual(at_boundary.observation_overlay.current_band, MembershipBand.FOCUS)
        self.assert_replay_equal(at_boundary)

    def test_pause_keeps_cutoff_but_discovery_continues_and_return_live_catches_up(self):
        controller = self.controller()
        frozen = controller.pause()
        self.acquire(completed=2)
        self.clock = BOUNDARY + timedelta(seconds=10)
        paused = self.drain(controller)
        self.assertEqual(paused.replay_time_utc, frozen.replay_time_utc)
        self.assertEqual(paused.state.acquisition_count, 0)
        self.assertEqual(paused.total_event_count, 3)
        controller.play()
        live = self.drain(controller)
        self.assertEqual(live.state.acquisition_count, 1)
        self.assert_replay_equal(live)

    def test_backward_seek_and_fresh_process_reconstruct_exact_state(self):
        self.publish(2, 1, ("OTHER", "TEST"))
        self.acquire(completed=2.47)
        self.clock = BOUNDARY + timedelta(seconds=10)
        controller = self.controller(limit=1)
        latest = self.drain(controller)
        controller.seek(BOUNDARY)
        earlier = self.drain(controller)
        self.assertEqual(earlier.observation_overlay.current_band, MembershipBand.UNI)
        self.assertEqual(earlier.state.acquisition_count, 0)
        self.assert_replay_equal(earlier)
        controller.play()
        self.assertEqual(self.drain(controller), latest)
        self.assertEqual(self.drain(self.controller(limit=1)), latest)

    def test_quote_waits_for_completion_and_outcomes_are_preserved(self):
        self.acquire(completed=2, status=QuoteStatus.INVALID)
        controller = self.controller()
        self.assertEqual(self.drain(controller).state.acquisition_count, 0)
        self.clock = BOUNDARY + timedelta(seconds=2)
        visible = self.drain(controller)
        self.assertEqual(visible.observation_overlay.latest_quote.status, "invalid")
        self.assert_replay_equal(visible)

    def test_transient_reader_failure_preserves_projection_and_retries(self):
        controller = self.controller()
        before = self.drain(controller)
        self.acquire()
        self.clock = BOUNDARY + timedelta(seconds=10)
        with patch.object(controller.reader, "read_page", side_effect=sqlite3.OperationalError("busy")):
            failed = controller.tick()
        self.assertEqual(failed.state, before.state)
        self.assertIn("busy", failed.source_error)
        after = self.drain(controller)
        self.assertEqual(after.state.acquisition_count, 1)
        self.assert_replay_equal(after)

    def test_incomplete_event_keeps_last_coherent_snapshot(self):
        controller = self.controller()
        before = self.drain(controller)
        self.acquire()
        self.clock = BOUNDARY + timedelta(seconds=10)
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM quote_observation WHERE symbol = 'TEST'")
        failed = controller.tick()
        self.assertEqual(failed.state, before.state)
        self.assertEqual(failed.observation_overlay, before.observation_overlay)
        self.assertIn("ReplayIntegrityError", failed.source_error)

    def test_rejects_future_or_naive_seek(self):
        controller = self.controller()
        with self.assertRaisesRegex(ValueError, "future"):
            controller.seek(BOUNDARY + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            controller.seek(BOUNDARY.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
