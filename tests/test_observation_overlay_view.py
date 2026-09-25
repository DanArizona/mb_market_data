from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import MappingProxyType

from mb_market_data.observation_overlay import (
    ET,
    MembershipBand,
    ObservationOverlayData,
    ObservationOverlayOHLCVCache,
    OverlayCandle,
    OverlayMembershipTransition,
    OverlayQuotePoint,
)
from mb_market_data.observation_overlay_view import (
    build_observation_overlay_figure,
    build_observation_overlay_view,
)


UTC = timezone.utc
SESSION_DATE = date(2026, 9, 24)


def overlay() -> ObservationOverlayData:
    candles = (
        OverlayCandle(
            symbol="TEST",
            start_et=datetime(2026, 9, 24, 9, 25, tzinfo=ET),
            open=10.0,
            high=10.5,
            low=9.75,
            close=10.25,
            volume=100,
        ),
        OverlayCandle(
            symbol="TEST",
            start_et=datetime(2026, 9, 24, 9, 30, tzinfo=ET),
            open=10.25,
            high=11.0,
            low=10.0,
            close=10.75,
            volume=200,
        ),
    )
    cache = ObservationOverlayOHLCVCache(
        symbol="TEST",
        session_date=SESSION_DATE,
        provider="Schwab",
        source="unit-test",
        acquired_at_utc=datetime(2026, 9, 24, 21, 0, tzinfo=UTC),
        request_start_et=datetime(2026, 9, 24, 0, 0, tzinfo=ET),
        request_end_et=datetime(2026, 9, 24, 16, 5, tzinfo=ET),
        source_payload_sha256="a" * 64,
        candles=candles,
    )
    return ObservationOverlayData(
        session_date=SESSION_DATE,
        symbol="TEST",
        replay_time_utc=datetime(2026, 9, 24, 13, 35, tzinfo=UTC),
        cache=cache,
        candles=candles,
        membership_transitions=(
            OverlayMembershipTransition(
                available_at_utc=datetime(
                    2026, 9, 24, 13, 30, tzinfo=UTC
                ),
                revision=0,
                band=MembershipBand.FOCUS,
                source="unit-test",
                reason="opening",
            ),
        ),
        quote_points=(
            OverlayQuotePoint(
                available_at_utc=datetime(
                    2026, 9, 24, 13, 30, 5, tzinfo=UTC
                ),
                scheduled_at_utc=datetime(
                    2026, 9, 24, 13, 30, 5, tzinfo=UTC
                ),
                acquisition_id="focus-1",
                channel="focus",
                channel_revision=0,
                status="quote",
                detail=None,
                values=MappingProxyType({"quote_last_price": 10.4}),
            ),
            OverlayQuotePoint(
                available_at_utc=datetime(
                    2026, 9, 24, 13, 30, 30, tzinfo=UTC
                ),
                scheduled_at_utc=datetime(
                    2026, 9, 24, 13, 30, 30, tzinfo=UTC
                ),
                acquisition_id="uni-1",
                channel="uni",
                channel_revision=0,
                status="invalid",
                detail="not available",
                values=MappingProxyType({}),
            ),
        ),
    )


class TestObservationOverlayView(unittest.TestCase):
    def test_summarizes_visible_evidence_and_current_band(self) -> None:
        view = build_observation_overlay_view(overlay())

        self.assertEqual(view.symbol, "TEST")
        self.assertEqual(view.current_band, MembershipBand.FOCUS)
        self.assertEqual(view.current_band_label, "Focus")
        self.assertEqual(view.candle_count, 2)
        self.assertEqual(view.quote_point_count, 2)
        self.assertEqual(dict(view.quote_status_counts), {"invalid": 1, "quote": 1})
        self.assertIn("2 completed candles", view.evidence_text)

    def test_figure_contains_candles_volume_quotes_and_membership_bands(self) -> None:
        figure = build_observation_overlay_figure(overlay())

        self.assertEqual(
            tuple(trace.name for trace in figure.data),
            ("5-minute OHLC", "Volume", "Focus quote"),
        )
        self.assertGreaterEqual(len(figure.layout.shapes), 3)
        self.assertFalse(figure.layout.xaxis.rangeslider.visible)
        self.assertEqual(figure.layout.yaxis.title.text, "Price")
        self.assertEqual(figure.layout.yaxis2.title.text, "Volume")


if __name__ == "__main__":
    unittest.main()
