from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mb_market_data.watchlist_polling import (
    POLL_SECONDS,
    MembershipUnavailableError,
    PollSlot,
    PollSlotGuard,
    PollWindow,
    SkippedPollSlot,
    WatchlistKind,
    WatchlistSnapshot,
    capture_poll_request,
    resolve_provider_poll_slot,
    next_poll_slot,
    poll_slots_between,
)


ET = ZoneInfo("America/New_York")


def et(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 9, hour, minute, second, tzinfo=ET)


class TestPollingCadence(unittest.TestCase):

    def setUp(self) -> None:
        self.window = PollWindow(
            start_at=et(9, 30),
            end_at=et(16, 0),
        )

    def test_initial_cadences(self) -> None:
        self.assertEqual(POLL_SECONDS[WatchlistKind.UNI], (0, 30))
        self.assertEqual(
            POLL_SECONDS[WatchlistKind.FOCUS],
            (5, 20, 35, 50),
        )

    def test_combined_slots_are_ordered_within_minute(self) -> None:
        slots = poll_slots_between(
            after=et(9, 29, 59),
            through=et(9, 30, 50),
            window=self.window,
        )

        self.assertEqual(
            [
                (slot.watchlist_kind, slot.scheduled_at.second)
                for slot in slots
            ],
            [
                (WatchlistKind.UNI, 0),
                (WatchlistKind.FOCUS, 5),
                (WatchlistKind.FOCUS, 20),
                (WatchlistKind.UNI, 30),
                (WatchlistKind.FOCUS, 35),
                (WatchlistKind.FOCUS, 50),
            ],
        )

    def test_slot_at_window_end_is_excluded(self) -> None:
        self.assertIsNone(
            next_poll_slot(
                WatchlistKind.UNI,
                after=et(15, 59, 59),
                window=self.window,
            )
        )

    def test_overrun_skips_missed_slot_instead_of_replaying_it(self) -> None:
        slot = next_poll_slot(
            WatchlistKind.FOCUS,
            after=et(10, 0, 26),
            window=self.window,
        )

        self.assertIsNotNone(slot)
        self.assertEqual(slot.scheduled_at, et(10, 0, 35))

    def test_exact_completed_slot_is_not_repeated(self) -> None:
        slot = next_poll_slot(
            WatchlistKind.UNI,
            after=et(10, 0, 30),
            window=self.window,
        )

        self.assertIsNotNone(slot)
        self.assertEqual(slot.scheduled_at, et(10, 1, 0))

    def test_next_slot_accepts_another_aware_timezone(self) -> None:
        slot = next_poll_slot(
            WatchlistKind.UNI,
            after=et(10, 0, 1).astimezone(timezone.utc),
            window=self.window,
        )

        self.assertIsNotNone(slot)
        self.assertEqual(slot.scheduled_at, et(10, 0, 30))

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            next_poll_slot(
                WatchlistKind.UNI,
                after=datetime(2026, 9, 9, 10, 0),
                window=self.window,
            )


class TestWatchlistSnapshot(unittest.TestCase):

    def test_symbols_are_normalized_and_deduplicated(self) -> None:
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=date(2026, 9, 9),
            revision=42,
            effective_at=et(10, 0),
            symbols=(" aapl ", "NVDA", "aapl"),
        )

        self.assertEqual(snapshot.symbols, ("AAPL", "NVDA"))

    def test_empty_membership_can_represent_an_optional_channel(self) -> None:
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=date(2026, 9, 9),
            revision=1,
            effective_at=et(9, 0),
            symbols=(),
        )

        self.assertEqual(snapshot.symbols, ())

    def test_capture_binds_slot_to_exact_revision(self) -> None:
        slot = PollSlot(
            watchlist_kind=WatchlistKind.FOCUS,
            scheduled_at=et(10, 0, 5),
        )
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=date(2026, 9, 9),
            revision=42,
            effective_at=et(10, 0),
            symbols=("AAPL", "NVDA"),
        )

        request = capture_poll_request(
            slot,
            snapshot,
            dispatched_at=et(10, 0, 6),
        )

        self.assertEqual(request.watchlist_revision, 42)
        self.assertEqual(request.symbols, ("AAPL", "NVDA"))
        self.assertEqual(request.dispatch_lateness_seconds, 1.0)
        self.assertEqual(
            request.batch_id,
            "focus:2026-09-09T10:00:05-04:00:r42",
        )

    def test_capture_rejects_wrong_watchlist_kind(self) -> None:
        slot = PollSlot(
            watchlist_kind=WatchlistKind.UNI,
            scheduled_at=et(10, 0, 0),
        )
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=date(2026, 9, 9),
            revision=1,
            effective_at=et(9, 0),
            symbols=("AAPL",),
        )

        with self.assertRaisesRegex(ValueError, "kinds do not match"):
            capture_poll_request(
                slot,
                snapshot,
                dispatched_at=et(10, 0, 1),
            )

    def test_capture_rejects_wrong_session_date(self) -> None:
        slot = PollSlot(
            watchlist_kind=WatchlistKind.UNI,
            scheduled_at=et(10, 0, 0),
        )
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.UNI,
            session_date=date(2026, 9, 10),
            revision=1,
            effective_at=et(9, 0),
            symbols=("AAPL",),
        )

        with self.assertRaisesRegex(ValueError, "session dates"):
            capture_poll_request(
                slot,
                snapshot,
                dispatched_at=et(10, 0, 1),
            )


class FakeMembershipProvider:
    def __init__(self, snapshot: WatchlistSnapshot | None) -> None:
        self.snapshot = snapshot
        self.lookups: list[tuple[WatchlistKind, datetime]] = []

    def latest_effective_snapshot(
        self,
        watchlist_kind: WatchlistKind,
        *,
        at: datetime,
    ) -> WatchlistSnapshot | None:
        self.lookups.append((watchlist_kind, at))
        return self.snapshot


class TestMembershipProviderCapture(unittest.TestCase):
    def test_resolves_membership_at_slot_time_not_delayed_dispatch(self) -> None:
        slot = PollSlot(
            watchlist_kind=WatchlistKind.FOCUS,
            scheduled_at=et(10, 0, 5),
        )
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=date(2026, 9, 9),
            revision=7,
            effective_at=et(9, 59),
            symbols=("AAPL",),
        )
        provider = FakeMembershipProvider(snapshot)

        request = resolve_provider_poll_slot(
            slot,
            provider,
            dispatched_at=et(10, 0, 8),
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.watchlist_revision, 7)
        self.assertEqual(request.symbols, ("AAPL",))
        self.assertEqual(
            provider.lookups,
            [(WatchlistKind.FOCUS, slot.scheduled_at)],
        )

    def test_missing_effective_revision_fails_closed(self) -> None:
        slot = PollSlot(
            watchlist_kind=WatchlistKind.UNI,
            scheduled_at=et(10, 0),
        )

        with self.assertRaisesRegex(
            MembershipUnavailableError,
            slot.slot_id,
        ):
            resolve_provider_poll_slot(
                slot,
                FakeMembershipProvider(None),
                dispatched_at=et(10, 0, 1),
            )

    def test_empty_channel_is_skipped_without_a_poll_request(self) -> None:
        slot = PollSlot(
            watchlist_kind=WatchlistKind.FOCUS,
            scheduled_at=et(10, 0, 5),
        )
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=date(2026, 9, 9),
            revision=7,
            effective_at=et(9, 59),
            symbols=(),
        )

        request = resolve_provider_poll_slot(
            slot,
            FakeMembershipProvider(snapshot),
            dispatched_at=et(10, 0, 6),
        )

        self.assertIsInstance(request, SkippedPollSlot)
        assert isinstance(request, SkippedPollSlot)
        self.assertEqual(request.watchlist_revision, 7)
        self.assertEqual(request.reason, "empty_membership")

    def test_provider_snapshot_must_be_effective_at_slot_time(self) -> None:
        slot = PollSlot(
            watchlist_kind=WatchlistKind.FOCUS,
            scheduled_at=et(10, 0, 5),
        )
        snapshot = WatchlistSnapshot(
            watchlist_kind=WatchlistKind.FOCUS,
            session_date=date(2026, 9, 9),
            revision=8,
            effective_at=et(10, 0, 6),
            symbols=("AAPL",),
        )

        with self.assertRaisesRegex(ValueError, "effective at slot time"):
            resolve_provider_poll_slot(
                slot,
                FakeMembershipProvider(snapshot),
                dispatched_at=et(10, 0, 8),
            )

class TestPollSlotGuard(unittest.TestCase):

    def test_each_slot_can_be_claimed_only_once(self) -> None:
        guard = PollSlotGuard()
        slot = PollSlot(
            watchlist_kind=WatchlistKind.UNI,
            scheduled_at=et(10, 0, 0),
        )

        self.assertTrue(guard.claim(slot))
        self.assertFalse(guard.claim(slot))

    def test_uni_and_focus_slots_are_independent(self) -> None:
        guard = PollSlotGuard()
        uni = PollSlot(
            watchlist_kind=WatchlistKind.UNI,
            scheduled_at=et(10, 0, 0),
        )
        focus = PollSlot(
            watchlist_kind=WatchlistKind.FOCUS,
            scheduled_at=et(10, 0, 5),
        )

        self.assertTrue(guard.claim(uni))
        self.assertTrue(guard.claim(focus))


if __name__ == "__main__":
    unittest.main()
