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
    default_reference,
    normalize_overlay_point_style,
    resolve_reference,
    selected_reference_from_click,
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
                available_at_utc=datetime(2026, 9, 24, 13, 30, tzinfo=UTC),
                revision=0,
                band=MembershipBand.FOCUS,
                source="unit-test",
                reason="opening",
            ),
        ),
        quote_points=(
            OverlayQuotePoint(
                available_at_utc=datetime(2026, 9, 24, 13, 30, 5, tzinfo=UTC),
                scheduled_at_utc=datetime(2026, 9, 24, 13, 30, 5, tzinfo=UTC),
                acquisition_id="focus-1",
                channel="focus",
                channel_revision=0,
                status="quote",
                detail=None,
                values=MappingProxyType({"quote_last_price": 10.4}),
            ),
            OverlayQuotePoint(
                available_at_utc=datetime(2026, 9, 24, 13, 30, 30, tzinfo=UTC),
                scheduled_at_utc=datetime(2026, 9, 24, 13, 30, 30, tzinfo=UTC),
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
                    size="standard",
                    opacity=0.35,
                )
            },
        )

        focus = next(trace for trace in figure.data if trace.name == "Focus quote")
        self.assertEqual(focus.marker.size, 4)
        self.assertEqual(focus.marker.opacity, 0.35)

        hidden = build_observation_overlay_figure(
            overlay(),
            point_styles={"focus": OverlayPointStyle(visible=False)},
        )
        self.assertNotIn("Focus quote", tuple(trace.name for trace in hidden.data))
        self.assertGreaterEqual(len(hidden.layout.shapes), 3)

    def test_bounds_point_style_and_uses_fixed_chart_height(self) -> None:
        self.assertEqual(
            normalize_overlay_point_style(
                OverlayPointStyle(size="unknown", opacity=0.0)
            ),
            OverlayPointStyle(size="big", opacity=0.1),
        )
        figure = build_observation_overlay_figure(overlay())
        self.assertEqual(figure.layout.height, 800)

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
            {shape.yref for shape in figure.layout.shapes if shape.layer == "below"},
            {"y domain", "y2 domain"},
        )
        self.assertEqual(
            {shape.xref for shape in figure.layout.shapes if shape.layer == "below"},
            {"x2", "x3"},
        )

    def test_percent_ticks_preserve_price_range_and_reference_lines(self) -> None:
        evidence = overlay()
        zoom = {"yaxis.range": [9.0, 11.0]}
        price = build_observation_overlay_figure(evidence, relayout_data=zoom)
        percent = build_observation_overlay_figure(
            evidence, units="percent", relayout_data=zoom
        )

        self.assertEqual(default_reference(evidence), (10.25, "09:30 ET candle open"))
        self.assertEqual(
            tuple(price.layout.yaxis.range), tuple(percent.layout.yaxis.range)
        )
        self.assertEqual(tuple(price.data[0].open), tuple(percent.data[0].open))
        self.assertEqual(percent.layout.yaxis.tickmode, "array")
        self.assertEqual(percent.layout.yaxis.title.text, "Change from reference")
        self.assertIn("0%", tuple(percent.layout.yaxis.ticktext))
        reference_lines = [
            shape for shape in percent.layout.shapes if shape.yref == "y"
        ]
        self.assertEqual(reference_lines[0].y0, 10.25)
        self.assertEqual(reference_lines[0].line.dash, "solid")
        self.assertIn("dash", {shape.line.dash for shape in reference_lines})

    def test_off_hours_filter_and_reference_causality(self) -> None:
        evidence = overlay()
        regular = build_observation_overlay_figure(evidence, off_hours=False)
        self.assertEqual(len(regular.data[0].x), 1)
        self.assertEqual(len(regular.data[1].x), 1)
        self.assertEqual(
            tuple(value.strftime("%H:%M") for value in regular.layout.xaxis3.range),
            ("09:30", "16:00"),
        )
        candle_click = {"points": [{"curveNumber": 0, "pointNumber": 1}]}
        selected = selected_reference_from_click(
            evidence, candle_click, {"data": regular.to_plotly_json()["data"]}, "high"
        )
        self.assertIsNone(selected)  # second candle is absent after filtering
        full = build_observation_overlay_figure(evidence)
        selected = selected_reference_from_click(
            evidence, candle_click, full.to_plotly_json(), "high"
        )
        self.assertEqual(selected["price"], 11.0)
        self.assertEqual(resolve_reference(evidence, selected)[0], 11.0)
        earlier = dict(selected, available_at_utc="2026-09-24T13:36:00+00:00")
        self.assertEqual(
            resolve_reference(evidence, earlier), default_reference(evidence)
        )

    def test_offscreen_reference_does_not_expand_regular_hours_axis(self) -> None:
        evidence = overlay()
        selected = {
            "session_date": evidence.session_date.isoformat(),
            "symbol": evidence.symbol,
            "available_at_utc": "2026-09-24T13:30:00+00:00",
            "price": 50.0,
            "label": "Selected off-hours price",
        }
        figure = build_observation_overlay_figure(
            evidence, off_hours=False, units="percent", selected_reference=selected
        )

        self.assertEqual(figure.layout.yaxis.tickmode, "array")
        self.assertFalse(any(shape.yref == "y" for shape in figure.layout.shapes))

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
