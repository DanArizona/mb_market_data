from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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
    resolve_request_error_segments,
    resolve_overlay_time_viewport,
    resolve_reference,
    resolve_halt_segments,
    selected_reference_from_click,
)
from mb_market_data.overlay_halts import OverlayHaltInterval

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
    def test_halt_segment_uses_first_in_halt_quote_and_recorded_release(self) -> None:
        value = overlay()
        halt = OverlayHaltInterval(
            datetime(2026, 9, 24, 9, 30, 0, tzinfo=ET),
            datetime(2026, 9, 24, 9, 31, 0, tzinfo=ET),
            "M",
        )
        segments = resolve_halt_segments(
            replace(value, halt_intervals=(halt,)),
            off_hours=True,
        )
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start_et, halt.start_et)
        self.assertEqual(segments[0].end_et, halt.end_et)
        self.assertEqual(segments[0].price, 10.4)

    def test_consecutive_request_errors_form_one_scheduled_time_span(self) -> None:
        def point(second: int, status: str) -> OverlayQuotePoint:
            scheduled = datetime(2026, 9, 24, 13, 30, tzinfo=UTC) + timedelta(
                seconds=second
            )
            return OverlayQuotePoint(
                available_at_utc=scheduled,
                scheduled_at_utc=scheduled,
                acquisition_id=f"uni-{second}",
                channel="uni",
                channel_revision=0,
                status=status,
                detail="bad JSON" if status == "request_error" else None,
                values=MappingProxyType(
                    {"quote_last_price": 10.5} if status == "quote" else {}
                ),
            )

        value = replace(
            overlay(),
            quote_points=(
                point(-30, "quote"),
                point(0, "request_error"),
                point(30, "request_error"),
                point(60, "quote"),
            ),
        )
        segments = resolve_request_error_segments(value, off_hours=True)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start_et.strftime("%H:%M:%S"), "09:30:00")
        self.assertEqual(segments[0].end_et.strftime("%H:%M:%S"), "09:31:00")
        self.assertEqual(segments[0].count, 2)
        self.assertEqual(segments[0].start_price, 10.5)
        self.assertEqual(segments[0].end_price, 10.5)

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
        self.assertEqual(figure.data[0].increasing.line.color, "#1A6B57")
        self.assertEqual(figure.data[0].decreasing.line.color, "#803D43")

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

    def test_uses_readable_theme_specific_hover(self) -> None:
        dark = build_observation_overlay_figure(overlay(), theme="dark")
        light = build_observation_overlay_figure(overlay(), theme="light")

        self.assertEqual(dark.layout.hovermode, "closest")
        self.assertEqual(
            dark.layout.hoverlabel.bgcolor,
            "rgba(20,30,40,0.38)",
        )
        self.assertEqual(dark.layout.hoverlabel.bordercolor, "#8797a6")
        self.assertEqual(dark.layout.hoverlabel.font.color, "#eef7ff")
        self.assertEqual(
            light.layout.hoverlabel.bgcolor,
            "rgba(246,248,250,0.50)",
        )
        focus = next(trace for trace in dark.data if trace.name == "Focus quote")
        self.assertNotIn("%{x}", focus.hovertemplate)
        self.assertIn("Price", focus.hovertemplate)
        self.assertNotIn("Acquisition", focus.hovertemplate)

    def test_four_hover_modes_keep_observations_and_features_distinct(self) -> None:
        evidence = overlay()
        for detailed in (False, True):
            for shift in (False, True):
                with self.subTest(detailed=detailed, shift=shift):
                    figure = build_observation_overlay_figure(
                        evidence, detailed_hover=detailed, shift_hover=shift
                    )
                    candle, volume, point = figure.data[:3]
                    self.assertEqual(point.hoverinfo == "all", not shift)
                    self.assertEqual(candle.hoverinfo == "all", shift)
                    self.assertEqual(volume.hoverinfo == "all", shift)
                    if shift:
                        self.assertIn("09:30–09:35", candle.customdata[1][5])
                        self.assertIn("Volume", candle.hovertemplate)
                        self.assertEqual(point.hovertemplate, None)
                    else:
                        self.assertIn("Price", point.hovertemplate)
                        self.assertEqual(candle.hovertemplate, None)
                        self.assertEqual("Acquisition" in point.hovertemplate, detailed)
                    self.assertEqual(
                        "Schwab 5-minute" in (candle.hovertemplate or ""),
                        detailed and shift,
                    )

    def test_detailed_observation_fields_describe_quote_result_and_trade_age(self) -> None:
        evidence = overlay()
        point = replace(
            evidence.quote_points[0],
            values=MappingProxyType(
                {
                    "quote_last_price": 10.4,
                    "quote_total_volume": 12_345,
                    "quote_security_status": "Normal",
                    "quote_trade_time_ms": int(
                        datetime(2026, 9, 24, 13, 30, tzinfo=UTC).timestamp() * 1000
                    ),
                }
            ),
        )
        figure = build_observation_overlay_figure(
            replace(evidence, quote_points=(point,)), detailed_hover=True
        )
        custom = figure.data[2].customdata[0]
        self.assertEqual(custom[0], "quote")
        self.assertEqual(custom[3], "+1.46%")
        self.assertEqual(custom[7], "5.0 s")
        self.assertEqual(custom[8], "12,345")
        self.assertEqual(custom[9], "Normal")
        self.assertIn(
            "API result: %{customdata[0]} object returned (trade may be stale)",
            figure.data[2].hovertemplate,
        )

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
        self.assertEqual(reference_lines[0].line.width, 1)
        self.assertEqual(reference_lines[0].line.color, "rgba(241,185,93,0.7)")
        self.assertIn("dash", {shape.line.dash for shape in reference_lines})

    def test_off_hours_filter_and_reference_causality(self) -> None:
        evidence = overlay()
        regular = build_observation_overlay_figure(evidence, off_hours=False)
        self.assertEqual(sum(value is not None for value in regular.data[0].close), 1)
        self.assertEqual(len(regular.data[1].x), 1)
        self.assertEqual(
            tuple(value.strftime("%H:%M") for value in regular.layout.xaxis3.range),
            ("09:30", "16:00"),
        )
        self.assertEqual(
            tuple(
                value.strftime("%H:%M")
                for value in regular.layout.xaxis3.rangeslider.range
            ),
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
            tuple(
                datetime.fromisoformat(str(value)).strftime("%H:%M")
                for value in figure.layout.xaxis3.range
            ),
            ("09:30", "09:40"),
        )
        self.assertEqual(tuple(figure.layout.yaxis.range), (10.0, 11.0))
        self.assertFalse(figure.layout.xaxis3.autorange)
        self.assertFalse(figure.layout.yaxis.autorange)

    def test_replay_starts_with_full_session_and_keeps_navigator_fixed(self) -> None:
        evidence = overlay()
        figure = build_observation_overlay_figure(evidence, off_hours=True)
        self.assertEqual(
            tuple(value.strftime("%H:%M") for value in figure.layout.xaxis3.range),
            ("09:30", "16:00"),
        )
        self.assertEqual(
            tuple(
                value.strftime("%H:%M")
                for value in figure.layout.xaxis3.rangeslider.range
            ),
            ("09:30", "16:00"),
        )

    def test_all_time_axes_stay_on_session_when_first_candle_appears(self) -> None:
        evidence = overlay()
        for frame in (
            replace(evidence, candles=(), quote_points=()),
            replace(evidence, candles=(evidence.candles[1],)),
        ):
            figure = build_observation_overlay_figure(frame)
            for name in ("xaxis", "xaxis2", "xaxis3"):
                axis = getattr(figure.layout, name)
                self.assertEqual(axis.type, "date")
                self.assertFalse(axis.autorange)
                self.assertEqual(
                    tuple(value.strftime("%H:%M") for value in axis.range),
                    ("09:30", "16:00"),
                )

    def test_zoom_keeps_price_volume_and_navigator_viewports_aligned(self) -> None:
        figure = build_observation_overlay_figure(
            overlay(),
            relayout_data={
                "xaxis2.range[0]": "2026-09-24 09:35:00",
                "xaxis2.range[1]": "2026-09-24 10:05:00",
            },
        )
        for name in ("xaxis", "xaxis2", "xaxis3"):
            axis = getattr(figure.layout, name)
            self.assertEqual(
                tuple(
                    datetime.fromisoformat(str(value)).strftime("%H:%M")
                    for value in axis.range
                ),
                ("09:35", "10:05"),
            )
        self.assertEqual(
            tuple(
                value.strftime("%H:%M")
                for value in figure.layout.xaxis3.rangeslider.range
            ),
            ("09:30", "16:00"),
        )
        self.assertEqual(figure.layout.xaxis3.rangeslider.yaxis.rangemode, "fixed")
        self.assertEqual(figure.layout.yaxis2.rangemode, "tozero")
        self.assertTrue(figure.layout.xaxis2.showgrid)

    def test_navigator_preserves_full_price_scale_when_main_plot_is_zoomed(self) -> None:
        evidence = overlay()
        full = build_observation_overlay_figure(evidence, off_hours=False)
        zoomed = build_observation_overlay_figure(
            evidence,
            off_hours=False,
            relayout_data={
                "xaxis3.range": ["2026-09-24 09:30:00", "2026-09-24 09:32:00"],
                "yaxis.range": [10.5, 10.6],
            },
        )
        full_range = tuple(full.layout.xaxis3.rangeslider.yaxis.range)
        self.assertEqual(tuple(zoomed.layout.xaxis3.rangeslider.yaxis.range), full_range)
        self.assertLess(full_range[0], 10.0)
        self.assertGreater(full_range[1], 11.0)
        self.assertEqual(tuple(zoomed.layout.yaxis.range), (10.5, 10.6))
        self.assertEqual(
            tuple(value.strftime("%H:%M") for value in zoomed.layout.xaxis3.rangeslider.range),
            ("09:30", "16:00"),
        )

        later_point = replace(
            evidence.quote_points[0],
            available_at_utc=datetime(2026, 9, 24, 14, 0, tzinfo=UTC),
            values=MappingProxyType({"quote_last_price": 15.0}),
        )
        later = build_observation_overlay_figure(
            replace(evidence, quote_points=(later_point,)), off_hours=False
        )
        self.assertGreater(later.layout.xaxis3.rangeslider.yaxis.range[1], 15.0)

    def test_pan_and_zoom_cannot_leave_session_and_volume_stays_on_zero(self) -> None:
        evidence = overlay()
        for requested, expected in (
            (("09:25", "09:35"), ("09:30", "09:40")),
            (("15:55", "16:05"), ("15:50", "16:00")),
            (("09:00", "17:00"), ("09:30", "16:00")),
        ):
            with self.subTest(requested=requested):
                selected = [f"2026-09-24 {time}:00" for time in requested]
                viewport = resolve_overlay_time_viewport(
                    evidence, None, {"xaxis3.range": selected}, off_hours=False
                )
                figure = build_observation_overlay_figure(
                    evidence,
                    viewport=viewport,
                    relayout_data={"yaxis2.range": [100, 500]},
                )
                for name in ("xaxis", "xaxis2", "xaxis3"):
                    axis = getattr(figure.layout, name)
                    self.assertEqual(
                        tuple(value.strftime("%H:%M") for value in axis.range),
                        expected,
                    )
                    self.assertEqual(axis.minallowed.strftime("%H:%M"), "09:30")
                    self.assertEqual(axis.maxallowed.strftime("%H:%M"), "16:00")
                self.assertEqual(tuple(figure.layout.yaxis2.range), (0, 500))
                self.assertEqual(
                    tuple(
                        value.strftime("%H:%M")
                        for value in figure.layout.xaxis3.rangeslider.range
                    ),
                    ("09:30", "16:00"),
                )

    def test_manual_zoom_stays_put_when_replay_crosses_view_edge(self) -> None:
        evidence = replace(
            overlay(),
            replay_time_utc=datetime(2026, 9, 24, 20, 5, tzinfo=UTC),
        )
        zoom = {"xaxis3.range": ["2026-09-24 09:30:00", "2026-09-24 10:00:00"]}
        figure = build_observation_overlay_figure(evidence, relayout_data=zoom)
        self.assertEqual(
            tuple(
                datetime.fromisoformat(str(value)).strftime("%H:%M")
                for value in figure.layout.xaxis3.range
            ),
            ("09:30", "10:00"),
        )
        self.assertEqual(
            tuple(
                value.strftime("%H:%M")
                for value in figure.layout.xaxis3.rangeslider.range
            ),
            ("09:30", "16:00"),
        )
        volume_zoom = {
            "xaxis2.range[0]": "2026-09-24 09:35:00",
            "xaxis2.range[1]": "2026-09-24 10:05:00",
        }
        volume_figure = build_observation_overlay_figure(
            evidence, relayout_data=volume_zoom
        )
        self.assertEqual(
            tuple(
                datetime.fromisoformat(str(value)).strftime("%H:%M")
                for value in volume_figure.layout.xaxis3.range
            ),
            ("09:35", "10:05"),
        )

    def test_zoom_ahead_of_data_follows_when_data_reaches_right_edge(self) -> None:
        first = overlay()
        zoom = {"xaxis3.range": ["2026-09-24 09:30:00", "2026-09-24 09:45:00"]}
        viewport = resolve_overlay_time_viewport(
            first, None, zoom, off_hours=True
        )
        self.assertEqual(viewport["mode"], "follow")
        self.assertEqual(
            [
                datetime.fromisoformat(item).strftime("%H:%M")
                for item in viewport["range"]
            ],
            ["09:30", "09:45"],
        )
        point = replace(
            first.quote_points[0],
            available_at_utc=datetime(2026, 9, 24, 13, 50, tzinfo=UTC),
        )
        evidence = replace(
            first,
            replay_time_utc=datetime(2026, 9, 24, 13, 50, tzinfo=UTC),
            quote_points=(point,),
        )
        viewport = resolve_overlay_time_viewport(
            evidence, viewport, None, off_hours=True
        )
        self.assertEqual(viewport["mode"], "follow")
        self.assertEqual(
            [
                datetime.fromisoformat(item).strftime("%H:%M")
                for item in viewport["range"]
            ],
            ["09:35", "09:50"],
        )
        figure = build_observation_overlay_figure(evidence, viewport=viewport)
        self.assertEqual(
            tuple(
                value.strftime("%H:%M")
                for value in figure.layout.xaxis3.rangeslider.range
            ),
            ("09:30", "16:00"),
        )

    def test_zoom_inside_existing_data_stays_fixed_through_replay(self) -> None:
        first = overlay()
        viewport = resolve_overlay_time_viewport(
            first,
            None,
            {"xaxis3.range": ["2026-09-24 09:30:00", "2026-09-24 09:34:00"]},
            off_hours=True,
        )
        self.assertEqual(viewport["mode"], "inspect")
        point = replace(
            first.quote_points[0],
            available_at_utc=datetime(2026, 9, 24, 14, 0, tzinfo=UTC),
        )
        later = replace(
            first, replay_time_utc=point.available_at_utc, quote_points=(point,)
        )
        later_viewport = resolve_overlay_time_viewport(
            later, viewport, {"yaxis.range": [9, 12]}, off_hours=True
        )
        self.assertEqual(later_viewport["mode"], "inspect")
        self.assertEqual(later_viewport["range"], viewport["range"])

    def test_no_zoom_stays_at_regular_session_even_after_close(self) -> None:
        first = overlay()
        point = replace(
            first.quote_points[0],
            available_at_utc=datetime(2026, 9, 24, 20, 5, tzinfo=UTC),
        )
        evidence = replace(
            first, replay_time_utc=point.available_at_utc, quote_points=(point,)
        )
        viewport = resolve_overlay_time_viewport(evidence, None, None, off_hours=True)
        self.assertEqual(viewport["mode"], "full")
        figure = build_observation_overlay_figure(evidence, viewport=viewport)
        self.assertEqual(
            tuple(value.strftime("%H:%M") for value in figure.layout.xaxis3.range),
            ("09:30", "16:00"),
        )

    def test_horizontal_autorange_reset_restores_regular_session(self) -> None:
        figure = build_observation_overlay_figure(
            overlay(), relayout_data={"xaxis3.autorange": True}
        )
        self.assertFalse(figure.layout.xaxis3.autorange)
        self.assertEqual(
            tuple(value.strftime("%H:%M") for value in figure.layout.xaxis3.range),
            ("09:30", "16:00"),
        )

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
