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
    OverlayPointStyle,
    apply_observation_overlay_view_state,
    build_observation_overlay_figure,
    build_observation_overlay_view,
    normalize_overlay_height,
    normalize_overlay_point_style,
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
            (
                "5-minute OHLC",
                "Volume",
                "Focus quote",
                "Navigator host",
            ),
        )
        self.assertGreaterEqual(len(figure.layout.shapes), 3)
        self.assertFalse(figure.layout.xaxis.rangeslider.visible)
        self.assertFalse(figure.layout.xaxis2.rangeslider.visible)
        self.assertTrue(figure.layout.xaxis3.rangeslider.visible)
        self.assertEqual(figure.data[0].xaxis, "x3")
        self.assertEqual(figure.data[1].xaxis, "x2")
        self.assertEqual(figure.data[2].xaxis, "x3")
        self.assertEqual(figure.data[3].xaxis, "x3")
        self.assertEqual(figure.data[3].yaxis, "y3")
        self.assertFalse(figure.data[3].showlegend)
        self.assertEqual(figure.data[3].hoverinfo, "skip")
        self.assertEqual(figure.data[3].opacity, 0)
        self.assertEqual(figure.layout.yaxis.title.text, "Price")
        self.assertEqual(figure.layout.yaxis2.title.text, "Volume")
        self.assertFalse(figure.layout.yaxis3.visible)

    def test_centers_candles_and_volume_on_five_minute_intervals(self) -> None:
        figure = build_observation_overlay_figure(overlay())

        expected = (
            datetime(2026, 9, 24, 9, 27, 30, tzinfo=ET),
            datetime(2026, 9, 24, 9, 32, 30, tzinfo=ET),
        )
        self.assertEqual(tuple(figure.data[0].x), expected)
        self.assertEqual(tuple(figure.data[1].x), expected)

    def test_applies_independent_quote_point_display_styles(self) -> None:
        figure = build_observation_overlay_figure(
            overlay(),
            point_styles={
                "focus": OverlayPointStyle(
                    visible=True,
                    size="small",
                    opacity=0.35,
                )
            },
        )

        focus = next(
            trace for trace in figure.data if trace.name == "Focus quote"
        )
        self.assertEqual(focus.marker.size, 2)
        self.assertEqual(focus.marker.opacity, 0.35)

        hidden = build_observation_overlay_figure(
            overlay(),
            point_styles={"focus": OverlayPointStyle(visible=False)},
        )
        self.assertNotIn(
            "Focus quote", tuple(trace.name for trace in hidden.data)
        )
        self.assertGreaterEqual(len(hidden.layout.shapes), 3)

    def test_bounds_point_style_and_height_presets(self) -> None:
        self.assertEqual(
            normalize_overlay_point_style(
                OverlayPointStyle(size="unknown", opacity=0.0)
            ),
            OverlayPointStyle(size="big", opacity=0.1),
        )
        self.assertEqual(normalize_overlay_height("tall"), "tall")
        self.assertEqual(normalize_overlay_height("unknown"), "standard")

        tall = build_observation_overlay_figure(overlay(), height="tall")
        full = build_observation_overlay_figure(overlay(), height="full")
        self.assertEqual(tall.layout.height, 720)
        self.assertEqual(full.layout.height, 900)

    def test_uses_readable_theme_specific_unified_hover(self) -> None:
        dark = build_observation_overlay_figure(overlay(), theme="dark")
        light = build_observation_overlay_figure(overlay(), theme="light")

        self.assertEqual(dark.layout.hovermode, "x unified")
        self.assertEqual(
            dark.layout.hoverlabel.bgcolor,
            "rgba(11,24,40,0.50)",
        )
        self.assertEqual(dark.layout.hoverlabel.font.color, "#eef7ff")
        self.assertEqual(
            light.layout.hoverlabel.bgcolor,
            "rgba(255,255,255,0.50)",
        )
        focus = next(trace for trace in dark.data if trace.name == "Focus quote")
        self.assertNotIn("%{x}", focus.hovertemplate)
        self.assertIn("Acquisition", focus.hovertemplate)

    def test_membership_shapes_are_confined_to_data_panels(self) -> None:
        figure = build_observation_overlay_figure(overlay())

        self.assertEqual(
            {shape.yref for shape in figure.layout.shapes},
            {"y domain", "y2 domain"},
        )
        self.assertEqual(
            {shape.xref for shape in figure.layout.shapes},
            {"x2", "x3"},
        )

    def test_reapplies_browser_axis_ranges_to_updated_figure(self) -> None:
        figure = build_observation_overlay_figure(overlay())

        result = apply_observation_overlay_view_state(
            figure,
            {
                "xaxis3.range[0]": "2026-09-24 09:25:00",
                "xaxis3.range[1]": "2026-09-24 09:35:00",
                "yaxis.range": [10.0, 11.0],
            },
        )

        self.assertIs(result, figure)
        self.assertEqual(
            tuple(figure.layout.xaxis3.range),
            ("2026-09-24 09:25:00", "2026-09-24 09:35:00"),
        )
        self.assertEqual(tuple(figure.layout.yaxis.range), (10.0, 11.0))
        self.assertFalse(figure.layout.xaxis3.autorange)
        self.assertFalse(figure.layout.yaxis.autorange)

    def test_reapplies_axis_autorange_reset(self) -> None:
        figure = build_observation_overlay_figure(overlay())
        figure.layout.yaxis.range = [10.0, 11.0]

        apply_observation_overlay_view_state(
            figure,
            {"yaxis.autorange": True},
        )

        self.assertTrue(figure.layout.yaxis.autorange)
        self.assertIsNone(figure.layout.yaxis.range)


if __name__ == "__main__":
    unittest.main()
