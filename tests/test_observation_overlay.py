from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mb_market_data.observation_overlay import (
    ET,
    MEMBERSHIP_BAND_COLORS,
    MembershipBand,
    ObservationOverlayError,
    ObservationOverlayOHLCVCache,
    ObservationOverlayProjector,
    OverlayCandle,
    load_observation_overlay_cache,
    prepare_observation_overlay,
    sha256_file,
    write_observation_overlay_cache,
)
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    MembershipRevisionEvent,
    QuoteAcquisitionEvent,
)
from mb_market_data.quote_observation_store import (
    SamplingChannelRevision,
    StoredAcquisition,
    StoredQuoteObservation,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision


UTC = timezone.utc
SESSION_DATE = date(2026, 9, 24)
OPEN_UTC = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)


def candle(hour: int, minute: int, volume: int) -> OverlayCandle:
    return OverlayCandle(
        symbol="TEST",
        start_et=datetime(2026, 9, 24, hour, minute, tzinfo=ET),
        open=10.0,
        high=11.0,
        low=9.0,
        close=10.5,
        volume=volume,
    )


def cache() -> ObservationOverlayOHLCVCache:
    return ObservationOverlayOHLCVCache(
        symbol="test",
        session_date=SESSION_DATE,
        provider="schwab",
        source="schwabdev.Client.price_history",
        acquired_at_utc=datetime(2026, 9, 24, 21, 0, tzinfo=UTC),
        request_start_et=datetime(2026, 9, 24, 0, 0, tzinfo=ET),
        request_end_et=datetime(2026, 9, 24, 16, 5, tzinfo=ET),
        source_payload_sha256="a" * 64,
        candles=(candle(9, 25, 100), candle(9, 30, 200)),
    )


def hierarchy(
    revision: int,
    seconds: int,
    *,
    uni: tuple[str, ...],
    focus: tuple[str, ...] = (),
    hot: tuple[str, ...] = (),
) -> MembershipRevisionEvent:
    return MembershipRevisionEvent(
        SamplingHierarchyRevision(
            session_date=SESSION_DATE,
            revision=revision,
            effective_at=OPEN_UTC + timedelta(seconds=seconds),
            uni_symbols=uni,
            focus_symbols=focus,
            hot_symbols=hot,
            source="unit-test",
            reason=f"r{revision}",
        )
    )


def acquisition(seconds: int = 5) -> QuoteAcquisitionEvent:
    completed = OPEN_UTC + timedelta(seconds=seconds)
    scheduled = completed - timedelta(seconds=1)
    header = StoredAcquisition(
        acquisition_id=f"focus-{seconds}",
        run_id="focus-run",
        channel="focus",
        channel_revision=1,
        session_date=SESSION_DATE,
        slot_id=f"slot-{seconds}",
        scheduled_at_utc=scheduled,
        dispatched_at_utc=scheduled,
        completed_at_utc=completed,
        request_count=1,
        batch_size=400,
        requested_symbol_count=1,
        unexpected_symbols=(),
        content_sha256="content",
    )
    observation = StoredQuoteObservation(
        acquisition_id=header.acquisition_id,
        channel="focus",
        channel_revision=1,
        scheduled_at_utc=scheduled,
        symbol="TEST",
        ordinal=0,
        status="quote",
        detail=None,
        schwab_batch_number=1,
        request_started_at_utc=scheduled,
        response_received_at_utc=completed,
        values={"quote_last_price": 10.25, "quote_total_volume": 1234},
    )
    return QuoteAcquisitionEvent(header, (observation,))


def events():
    return (
        hierarchy(0, 0, uni=("TEST", "OTHER")),
        hierarchy(1, 0, uni=("TEST", "OTHER"), focus=("TEST",)),
        acquisition(),
        hierarchy(
            2,
            60,
            uni=("TEST", "OTHER"),
            focus=("TEST",),
            hot=("TEST",),
        ),
        hierarchy(3, 120, uni=("OTHER",)),
    )


class TestOverlayCandleVisibility(unittest.TestCase):
    def test_candle_is_hidden_until_its_interval_closes(self) -> None:
        source = cache()
        just_before = datetime(2026, 9, 24, 9, 34, 59, 999999, tzinfo=ET)
        at_close = datetime(2026, 9, 24, 9, 35, tzinfo=ET)

        self.assertEqual(
            tuple(item.start_et.minute for item in source.visible_candles(just_before)),
            (25,),
        )
        self.assertEqual(
            tuple(item.start_et.minute for item in source.visible_candles(at_close)),
            (25, 30),
        )

    def test_cache_rejects_duplicate_or_out_of_order_candles(self) -> None:
        with self.assertRaisesRegex(ValueError, "strictly ordered"):
            ObservationOverlayOHLCVCache(
                symbol="TEST",
                session_date=SESSION_DATE,
                provider="Schwab",
                source="unit-test",
                acquired_at_utc=datetime(2026, 9, 24, 21, 0, tzinfo=UTC),
                request_start_et=datetime(2026, 9, 24, 0, 0, tzinfo=ET),
                request_end_et=datetime(2026, 9, 24, 16, 5, tzinfo=ET),
                source_payload_sha256="b" * 64,
                candles=(candle(9, 30, 1), candle(9, 25, 2)),
            )


class TestObservationOverlayProjection(unittest.TestCase):
    def prepare(self, cutoff: datetime):
        return prepare_observation_overlay(
            events(),
            cache=cache(),
            symbol="test",
            session_date=SESSION_DATE,
            replay_time_utc=cutoff,
        )

    def test_before_first_revision_symbol_is_outside_with_no_quotes(self) -> None:
        result = self.prepare(OPEN_UTC - timedelta(microseconds=1))

        self.assertEqual(result.current_band, MembershipBand.OUTSIDE_UNI)
        self.assertEqual(result.membership_transitions, ())
        self.assertEqual(result.quote_points, ())
        self.assertEqual(result.candles, ())

    def test_same_timestamp_revisions_project_uni_then_focus(self) -> None:
        result = self.prepare(OPEN_UTC)

        self.assertEqual(
            tuple(item.band for item in result.membership_transitions),
            (MembershipBand.UNI, MembershipBand.FOCUS),
        )
        self.assertEqual(result.current_band, MembershipBand.FOCUS)
        self.assertEqual(
            result.current_band_color,
            MEMBERSHIP_BAND_COLORS[MembershipBand.FOCUS],
        )
        self.assertEqual(result.quote_points, ())

    def test_quote_is_visible_only_at_acquisition_completion(self) -> None:
        before = self.prepare(OPEN_UTC + timedelta(seconds=4, microseconds=999999))
        at_completion = self.prepare(OPEN_UTC + timedelta(seconds=5))

        self.assertEqual(before.quote_points, ())
        self.assertEqual(len(at_completion.quote_points), 1)
        point = at_completion.quote_points[0]
        self.assertEqual(point.available_at_utc, OPEN_UTC + timedelta(seconds=5))
        self.assertEqual(point.values["quote_last_price"], 10.25)
        with self.assertRaises(TypeError):
            point.values["quote_last_price"] = 99  # type: ignore[index]

    def test_uni_focus_hot_removal_sequence_is_preserved(self) -> None:
        at_hot = self.prepare(OPEN_UTC + timedelta(seconds=60))
        after_removal = self.prepare(OPEN_UTC + timedelta(seconds=120))

        self.assertEqual(at_hot.current_band, MembershipBand.HOT)
        self.assertEqual(
            tuple(item.band for item in after_removal.membership_transitions),
            (
                MembershipBand.UNI,
                MembershipBand.FOCUS,
                MembershipBand.HOT,
                MembershipBand.OUTSIDE_UNI,
            ),
        )
        self.assertEqual(after_removal.current_band, MembershipBand.OUTSIDE_UNI)
        self.assertEqual(after_removal.latest_quote.status, "quote")

    def test_rejects_legacy_channel_revisions(self) -> None:
        legacy = ChannelRevisionEvent(
            SamplingChannelRevision(
                channel="uni",
                session_date=SESSION_DATE,
                revision=0,
                effective_at=OPEN_UTC,
                symbols=("TEST",),
                source="legacy",
            )
        )

        with self.assertRaisesRegex(
            ObservationOverlayError, "requires a schema-v2 hierarchy journal"
        ):
            prepare_observation_overlay(
                (legacy,),
                cache=cache(),
                symbol="TEST",
                session_date=SESSION_DATE,
                replay_time_utc=OPEN_UTC,
            )

    def test_rejects_out_of_order_events(self) -> None:
        out_of_order = (events()[2], events()[0])
        with self.assertRaisesRegex(ObservationOverlayError, "causal"):
            prepare_observation_overlay(
                out_of_order,
                cache=cache(),
                symbol="TEST",
                session_date=SESSION_DATE,
                replay_time_utc=OPEN_UTC + timedelta(minutes=1),
            )

    def test_incremental_projector_keeps_only_selected_symbol_evidence(self) -> None:
        projector = ObservationOverlayProjector(
            cache=cache(),
            symbol="TEST",
            session_date=SESSION_DATE,
        )

        projector.apply(events()[0])
        at_open = projector.snapshot(OPEN_UTC)
        self.assertEqual(at_open.current_band, MembershipBand.UNI)
        self.assertEqual(at_open.quote_points, ())

        projector.apply(events()[1])
        projector.apply(events()[2])
        after_quote = projector.snapshot(OPEN_UTC + timedelta(seconds=5))
        self.assertEqual(after_quote.current_band, MembershipBand.FOCUS)
        self.assertEqual(len(after_quote.quote_points), 1)
        self.assertEqual(after_quote.latest_quote.channel, "focus")

        # Time alone reveals a completed candle without retaining or applying
        # another journal event.
        at_candle_close = projector.snapshot(
            datetime(2026, 9, 24, 9, 35, tzinfo=ET)
        )
        self.assertEqual(len(at_candle_close.candles), 2)


class TestObservationOverlayCacheArtifact(unittest.TestCase):
    def test_round_trip_preserves_provenance_and_is_immutable(self) -> None:
        expected = cache()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "TEST-2026-09-24.json"
            written = write_observation_overlay_cache(path, expected)
            loaded = load_observation_overlay_cache(written)

            self.assertEqual(loaded, expected)
            self.assertEqual(len(sha256_file(written)), 64)
            self.assertEqual(loaded.provider, "Schwab")
            self.assertEqual(
                loaded.source, "schwabdev.Client.price_history"
            )
            self.assertEqual(loaded.source_payload_sha256, "a" * 64)
            with self.assertRaises(FileExistsError):
                write_observation_overlay_cache(path, expected)

    def test_overlay_rejects_cache_for_another_symbol(self) -> None:
        with self.assertRaisesRegex(ValueError, "cache symbol"):
            prepare_observation_overlay(
                (),
                cache=cache(),
                symbol="OTHER",
                session_date=SESSION_DATE,
                replay_time_utc=OPEN_UTC,
            )


if __name__ == "__main__":
    unittest.main()
