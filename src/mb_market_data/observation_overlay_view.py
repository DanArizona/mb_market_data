"""Framework-light visual projection for one Observation Overlay snapshot."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from mb_market_data.observation_overlay import (
    MEMBERSHIP_BAND_COLORS,
    MembershipBand,
    ObservationOverlayData,
    OverlayQuotePoint,
)


ET = ZoneInfo("America/New_York")
CHANNEL_COLORS: Mapping[str, str] = MappingProxyType(
    {"uni": "#00bcd4", "focus": "#d4a017", "hot": "#d100d1"}
)


@dataclass(frozen=True, slots=True)
class ObservationOverlayView:
    """Compact text and count projection rendered beside the chart."""

    symbol: str
    replay_time_et_text: str
    current_band: MembershipBand
    current_band_color: str
    candle_count: int
    quote_point_count: int
    quote_status_counts: Mapping[str, int]

    @property
    def current_band_label(self) -> str:
        return self.current_band.value.replace("_", " ").title()

    @property
    def evidence_text(self) -> str:
        statuses = ", ".join(
            f"{name} {count:,}"
            for name, count in self.quote_status_counts.items()
        )
        quote_text = statuses or "no quote outcomes"
        return (
            f"{self.candle_count:,} completed candles · "
            f"{self.quote_point_count:,} quote outcomes ({quote_text})"
        )


def build_observation_overlay_view(
    overlay: ObservationOverlayData,
) -> ObservationOverlayView:
    """Build deterministic display metadata from prepared overlay evidence."""

    statuses = Counter(point.status for point in overlay.quote_points)
    return ObservationOverlayView(
        symbol=overlay.symbol,
        replay_time_et_text=(
            overlay.replay_time_utc.astimezone(ET).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )[:-3]
            + " ET"
        ),
        current_band=overlay.current_band,
        current_band_color=overlay.current_band_color,
        candle_count=len(overlay.candles),
        quote_point_count=len(overlay.quote_points),
        quote_status_counts=MappingProxyType(dict(sorted(statuses.items()))),
    )


def _quote_price(point: OverlayQuotePoint) -> float | None:
    value = point.values.get("quote_last_price")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _rgba(color: str, alpha: float) -> str:
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    return f"rgba({red},{green},{blue},{alpha})"


def _membership_shapes(overlay: ObservationOverlayData) -> list[dict[str, Any]]:
    start = overlay.cache.request_start_et
    end = overlay.replay_time_utc.astimezone(ET)
    if end <= start:
        return []

    shapes: list[dict[str, Any]] = []
    cursor = start
    band = MembershipBand.OUTSIDE_UNI
    for transition in overlay.membership_transitions:
        boundary = transition.available_at_utc.astimezone(ET)
        if boundary > cursor:
            shapes.append(
                {
                    "type": "rect",
                    "xref": "x",
                    "yref": "paper",
                    "x0": cursor,
                    "x1": min(boundary, end),
                    "y0": 0,
                    "y1": 1,
                    "line": {"width": 0},
                    "fillcolor": _rgba(MEMBERSHIP_BAND_COLORS[band], 0.08),
                    "layer": "below",
                }
            )
        if boundary <= end:
            shapes.append(
                {
                    "type": "line",
                    "xref": "x",
                    "yref": "paper",
                    "x0": boundary,
                    "x1": boundary,
                    "y0": 0,
                    "y1": 1,
                    "line": {
                        "color": MEMBERSHIP_BAND_COLORS[transition.band],
                        "width": 1,
                        "dash": "dot",
                    },
                    "layer": "below",
                }
            )
        cursor = max(cursor, boundary)
        band = transition.band
    if cursor < end:
        shapes.append(
            {
                "type": "rect",
                "xref": "x",
                "yref": "paper",
                "x0": cursor,
                "x1": end,
                "y0": 0,
                "y1": 1,
                "line": {"width": 0},
                "fillcolor": _rgba(MEMBERSHIP_BAND_COLORS[band], 0.08),
                "layer": "below",
            }
        )
    return shapes


def build_observation_overlay_figure(
    overlay: ObservationOverlayData,
    *,
    theme: str = "dark",
) -> Any:
    """Build a Plotly candlestick/volume figure with causal quote points."""

    if theme not in {"dark", "light"}:
        raise ValueError("theme must be 'dark' or 'light'")

    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Plotly is required for the Observation Overlay visual surface."
        ) from exc

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=(0.78, 0.22),
    )
    candles = overlay.candles
    if candles:
        candle_times = [item.start_et for item in candles]
        figure.add_trace(
            go.Candlestick(
                x=candle_times,
                open=[item.open for item in candles],
                high=[item.high for item in candles],
                low=[item.low for item in candles],
                close=[item.close for item in candles],
                name="5-minute OHLC",
                increasing_line_color="#34d6ad",
                decreasing_line_color="#ff7b85",
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Bar(
                x=candle_times,
                y=[item.volume for item in candles],
                name="Volume",
                marker_color="#6fc8ff",
                opacity=0.55,
                hovertemplate="%{x}<br>Volume %{y:,}<extra></extra>",
            ),
            row=2,
            col=1,
        )

    channels = tuple(sorted({point.channel for point in overlay.quote_points}))
    for channel in channels:
        priced = tuple(
            (point, price)
            for point in overlay.quote_points
            if point.channel == channel
            if (price := _quote_price(point)) is not None
        )
        if not priced:
            continue
        figure.add_trace(
            go.Scatter(
                x=[point.available_at_utc.astimezone(ET) for point, _ in priced],
                y=[price for _, price in priced],
                mode="markers",
                name=f"{channel.title()} quote",
                marker={
                    "color": CHANNEL_COLORS.get(channel, "#eef7ff"),
                    "size": 6,
                    "line": {"color": "#06101c", "width": 0.5},
                },
                customdata=[
                    [point.status, point.channel_revision, point.acquisition_id]
                    for point, _ in priced
                ],
                hovertemplate=(
                    "%{x}<br>Last %{y}<br>Status %{customdata[0]}<br>"
                    "Revision r%{customdata[1]}<br>%{customdata[2]}<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )

    figure.update_layout(
        template="plotly_white" if theme == "light" else "plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 54, "r": 22, "t": 16, "b": 34},
        height=520,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02, "x": 0},
        uirevision=f"{overlay.session_date.isoformat()}:{overlay.symbol}",
        shapes=_membership_shapes(overlay),
    )
    figure.update_xaxes(rangeslider_visible=False, title_text="Eastern Time", row=2)
    figure.update_yaxes(title_text="Price", row=1, col=1)
    figure.update_yaxes(title_text="Volume", row=2, col=1)
    return figure
