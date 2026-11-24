"""Focused synthetic checks of fast historical symbol selection; no Schwab or files."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from mb_market_data.observation_overlay import (
    MembershipBand, ObservationOverlayError, ObservationOverlayProjector,
)
from mb_market_data.overlay_indexed_selection import IndexedOverlaySymbolSelection
from mb_market_data.quote_dashboard_replay import QuoteDashboardReplayController
from mb_market_data.quote_journal_replay import (
    MembershipRevisionEvent, QuoteAcquisitionEvent, ReplayTimeline,
)
from mb_market_data.quote_observation_store import (
    StoredAcquisition, StoredQuoteObservation,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision

DAY = date(2026, 11, 13)
START = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)


def acquisition(
    key: str, completed_seconds: int, *, symbols: int = 1,
    channel: str = "focus", channel_revision: int = 0,
) -> StoredAcquisition:
    return StoredAcquisition(
        acquisition_id=key, run_id="run-1", channel=channel,
        channel_revision=channel_revision, session_date=DAY, slot_id=key,
        scheduled_at_utc=START + timedelta(seconds=completed_seconds - 1),
        dispatched_at_utc=START + timedelta(seconds=completed_seconds - 1),
        completed_at_utc=START + timedelta(seconds=completed_seconds),
        request_count=1, batch_size=400, requested_symbol_count=symbols,
        unexpected_symbols=(), content_sha256="a" * 64,
    )


def observation(acq: StoredAcquisition, symbol: str, ordinal: int) -> StoredQuoteObservation:
    return StoredQuoteObservation(
        acquisition_id=acq.acquisition_id, channel=acq.channel,
        channel_revision=acq.channel_revision,
        scheduled_at_utc=acq.scheduled_at_utc, symbol=symbol,
        ordinal=ordinal, status="quote", detail=None, schwab_batch_number=1,
        request_started_at_utc=acq.dispatched_at_utc,
        response_received_at_utc=acq.completed_at_utc,
        values={"quote_last_price": 10 + ordinal, "quote_total_volume": 1234},
    )


class FakeStore:
    def __init__(self, revisions, acquisitions, observations):
        self.revisions = revisions
        self.acquisitions = acquisitions
        self.observations = observations
        self.header_calls = 0
        self.selection_calls: list[str] = []

    def membership_revisions_in_effective_order(self):
        self.header_calls += 1
        return self.revisions

    def acquisitions_in_replay_order(self):
        self.header_calls += 1
        return self.acquisitions

    def recent_observations(self, symbol, *, limit):
        self.selection_calls.append(symbol)
        assert limit > len(self.observations)
        # Mimic the store's descending scheduled-time SELECT.
        return tuple(reversed([obs for obs in self.observations if obs.symbol == symbol]))


def fixture():
    r0 = SamplingHierarchyRevision(
        session_date=DAY, revision=0, effective_at=START,
        uni_symbols=("AAA", "BBB"), focus_symbols=("AAA", "BBB"),
        hot_symbols=("BBB",), source="unit-test",
    )
    r1 = SamplingHierarchyRevision(
        session_date=DAY, revision=1,
        effective_at=START + timedelta(seconds=20),
        uni_symbols=("AAA", "BBB"), focus_symbols=("AAA", "BBB"),
        hot_symbols=(), source="unit-test",
    )
    a1 = acquisition("a1", 5, symbols=2)
    a2 = acquisition("a2", 25, symbols=2, channel_revision=1)
    aaa = observation(a1, "AAA", 0)
    bbb1 = observation(a1, "BBB", 1)
    aaa2 = observation(a2, "AAA", 0)
    bbb2 = observation(a2, "BBB", 1)
    events = (
        MembershipRevisionEvent(r0),
        QuoteAcquisitionEvent(a1, (aaa, bbb1)),
        MembershipRevisionEvent(r1),
        QuoteAcquisitionEvent(a2, (aaa2, bbb2)),
    )
    store = FakeStore((r0, r1), (a1, a2), (aaa, bbb1, aaa2, bbb2))
    reader = SimpleNamespace(schema_version=2, session_date=DAY, store=store)
    def make(symbol):
        return ObservationOverlayProjector(cache=None, symbol=symbol, session_date=DAY)
    return store, events, IndexedOverlaySymbolSelection(reader, make), make


class TestIndexedOverlaySymbolSelection(unittest.TestCase):
    def test_indexed_projection_matches_full_event_projection(self):
        _, events, indexed, make = fixture()
        full = make("BBB")
        for event in events:
            full.apply(event)
        fast = indexed("BBB", START + timedelta(seconds=30), None)
        self.assertEqual(
            fast.snapshot(START + timedelta(seconds=30)).quote_points,
            full.snapshot(START + timedelta(seconds=30)).quote_points,
        )
        self.assertEqual(
            fast.snapshot(START + timedelta(seconds=30)).membership_transitions,
            full.snapshot(START + timedelta(seconds=30)).membership_transitions,
        )
        self.assertEqual(fast.snapshot(START + timedelta(seconds=30)).current_band, MembershipBand.FOCUS)

    def test_indexed_selection_is_causal_and_reuses_headers(self):
        store, events, indexed, _ = fixture()
        early = indexed("BBB", START + timedelta(seconds=10), events[2])
        self.assertEqual(len(early.snapshot(START + timedelta(seconds=10)).quote_points), 1)
        self.assertEqual(early.snapshot(START + timedelta(seconds=10)).current_band, MembershipBand.HOT)
        other = indexed("AAA", START + timedelta(seconds=30), None)
        # Both a1 and a2 contain AAA at this cutoff; preserve both.
        self.assertEqual(
            [point.acquisition_id for point in other.snapshot(
                START + timedelta(seconds=30)
            ).quote_points],
            ["a1", "a2"],
        )
        self.assertEqual(store.header_calls, 2)
        self.assertEqual(store.selection_calls, ["BBB", "AAA"])

    def test_equal_timestamp_pending_event_is_not_imported_early(self):
        store, _, indexed, _ = fixture()
        first = acquisition("first", 10, channel="focus")
        second = acquisition("second", 10, channel="hot")
        obs_first = observation(first, "BBB", 0)
        obs_second = observation(second, "BBB", 0)
        store.acquisitions = (first, second)
        store.observations = (obs_first, obs_second)
        pending = QuoteAcquisitionEvent(second, (obs_second,))
        selected = indexed("BBB", START + timedelta(seconds=10), pending)
        points = selected.snapshot(START + timedelta(seconds=10)).quote_points
        self.assertEqual([point.acquisition_id for point in points], ["first"])

    def test_controller_switch_uses_indexed_factory_without_replaying_every_event(self):
        store, events, indexed, make = fixture()
        calls = [0]
        def event_factory():
            calls[0] += 1
            return iter(events)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(
                DAY, len(events), START, START + timedelta(seconds=25)
            ),
            event_factory=event_factory,
            overlay_projector_factory=lambda: make("AAA"),
            symbol_projector_factory=make,
            indexed_symbol_projector_factory=indexed,
        )
        before = controller.finish()
        starts = calls[0]
        selected = controller.select_overlay_symbol("BBB")
        self.assertEqual(calls[0], starts)
        self.assertEqual(selected.applied_event_count, before.applied_event_count)
        self.assertEqual(selected.replay_time_utc, before.replay_time_utc)
        self.assertEqual(len(selected.observation_overlay.quote_points), 2)
        self.assertEqual(store.selection_calls, ["BBB"])


if __name__ == "__main__":
    unittest.main()
