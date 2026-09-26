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
    OHLCV_INTERVAL,
    MembershipBand,
    ObservationOverlayData,
    OverlayQuotePoint,
)


ET = ZoneInfo("America/New_York")
CHANNEL_COLORS: Mapping[str, str] = MappingProxyType(
    {"uni": "#00bcd4", "focus": "#d4a017", "hot": "#d100d1"}
)
POINT_SIZE_PIXELS: Mapping[str, int] = MappingProxyType(
    {"small": 2, "medium": 4, "big": 6}
)
OVERLAY_HEIGHT_PIXELS: Mapping[str, int | None] = MappingProxyType(
    {"standard": 520, "tall": 720, "full": 900}
)


@dataclass(frozen=True, slots=True)
class OverlayPointStyle:
    """Display-only styling for one quote-observation channel."""

    visible: bool = True
    size: str = "big"
    opacity: float = 1.0


def normalize_overlay_point_style(
    value: OverlayPointStyle | None,
) -> OverlayPointStyle:
    """Return a bounded point style safe for rendering."""

    style = (
        value if isinstance(value, OverlayPointStyle) else OverlayPointStyle()
    )
    size = style.size if style.size in POINT_SIZE_PIXELS else "big"
    opacity = min(1.0, max(0.1, float(style.opacity)))
    return OverlayPointStyle(
        visible=bool(style.visible),
        size=size,
        opacity=opacity,
    )


def normalize_overlay_height(value: str | None) -> str:
    """Return a supported chart-height preset."""

    return value if value in OVERLAY_HEIGHT_PIXELS else "standard"


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


def _panel_membership_shape(
    *,
    shape_type: str,
    panel: int,
    x0: datetime,
    x1: datetime,
    color: str,
) -> dict[str, Any]:
    """Build one membership shape confined to a plotted data panel."""

    shape: dict[str, Any] = {
        "type": shape_type,
        "xref": "x3" if panel == 1 else "x2",
        "yref": "y domain" if panel == 1 else "y2 domain",
        "x0": x0,
        "x1": x1,
        "y0": 0,
        "y1": 1,
        "layer": "below",
    }
    if shape_type == "rect":
        shape.update(
            line={"width": 0},
            fillcolor=_rgba(color, 0.08),
        )
    else:
        shape["line"] = {"color": color, "width": 1, "dash": "dot"}
    return shape


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
            for panel in (1, 2):
                shapes.append(
                    _panel_membership_shape(
                        shape_type="rect",
                        panel=panel,
                        x0=cursor,
                        x1=min(boundary, end),
                        color=MEMBERSHIP_BAND_COLORS[band],
                    )
                )
        if boundary <= end:
            for panel in (1, 2):
                shapes.append(
                    _panel_membership_shape(
                        shape_type="line",
                        panel=panel,
                        x0=boundary,
                        x1=boundary,
                        color=MEMBERSHIP_BAND_COLORS[transition.band],
                    )
                )
        cursor = max(cursor, boundary)
        band = transition.band
    if cursor < end:
        for panel in (1, 2):
            shapes.append(
                _panel_membership_shape(
                    shape_type="rect",
                    panel=panel,
                    x0=cursor,
                    x1=end,
                    color=MEMBERSHIP_BAND_COLORS[band],
                )
            )
    return shapes


def apply_observation_overlay_view_state(
    figure: Any,
    relayout_data: Mapping[str, Any] | None,
) -> Any:
    """Reapply explicit browser axis ranges to a replacement figure."""

    if not isinstance(relayout_data, Mapping):
        return figure

    for axis_name in (
        "xaxis",
        "xaxis2",
        "xaxis3",
        "yaxis",
        "yaxis2",
    ):
        axis = getattr(figure.layout, axis_name)
        autorange = relayout_data.get(f"{axis_name}.autorange")
        if autorange is True:
            axis.autorange = True
            axis.range = None
            continue

        direct_range = relayout_data.get(f"{axis_name}.range")
        if (
            isinstance(direct_range, (list, tuple))
            and len(direct_range) == 2
        ):
            axis.autorange = False
            axis.range = list(direct_range)
            continue

        lower_key = f"{axis_name}.range[0]"
        upper_key = f"{axis_name}.range[1]"
        if lower_key in relayout_data and upper_key in relayout_data:
            axis.autorange = False
            axis.range = [
                relayout_data[lower_key],
                relayout_data[upper_key],
            ]
    return figure


def build_observation_overlay_figure(
    overlay: ObservationOverlayData,
    *,
    theme: str = "dark",
    point_styles: Mapping[str, OverlayPointStyle] | None = None,
    height: str = "standard",
) -> Any:
    """Build a Plotly candlestick/volume figure with causal quote points."""

    if theme not in {"dark", "light"}:
        raise ValueError("theme must be 'dark' or 'light'")
    height = normalize_overlay_height(height)

    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Plotly is required for the Observation Overlay visual surface."
        ) from exc

    figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=(0.779, 0.22, 0.001),
    )
    candles = overlay.candles
    if candles:
        candle_times = [
            item.start_et + OHLCV_INTERVAL / 2 for item in candles
        ]
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
        figure.data[-1].xaxis = "x3"
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
        style = normalize_overlay_point_style(
            point_styles.get(channel) if point_styles is not None else None
        )
        if not style.visible:
            continue
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
                    "size": POINT_SIZE_PIXELS[style.size],
                    "opacity": style.opacity,
                    "line": {"color": "#06101c", "width": 0.5},
                },
                customdata=[
                    [point.status, point.channel_revision, point.acquisition_id]
                    for point, _ in priced
                ],
                hovertemplate=(
                    "Last %{y}<br>Status %{customdata[0]}<br>"
                    "Revision r%{customdata[1]}<br>"
                    "Acquisition %{customdata[2]}<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )
        figure.data[-1].xaxis = "x3"

    # Plotly positions a range slider below the lowest data-bearing subplot
    # associated with its x-axis.  Price traces deliberately use x3 so that
    # their candlesticks appear in the navigator, but without this inert host
    # trace Plotly considers the Price panel to be x3's lowest subplot and
    # draws the navigator over Volume.  Registering x3/y3 makes the hidden
    # third row the positioning host without adding visible or hoverable data.
    figure.add_trace(
        go.Scatter(
            x=[None],
            y=[None],
            mode="lines",
            name="Navigator host",
            line={"width": 0},
            opacity=0,
            showlegend=False,
            hoverinfo="skip",
        ),
        row=3,
        col=1,
    )

    hoverlabel = (
        {
            "bgcolor": "rgba(255,255,255,0.50)",
            "bordercolor": "#708091",
            "font": {"color": "#172431", "size": 13},
            "align": "left",
        }
        if theme == "light"
        else {
            "bgcolor": "rgba(11,24,40,0.50)",
            "bordercolor": "#6f8598",
            "font": {"color": "#eef7ff", "size": 13},
            "align": "left",
        }
    )
    layout_height = OVERLAY_HEIGHT_PIXELS[height]
    layout_options: dict[str, Any] = {
        "template": "plotly_white" if theme == "light" else "plotly_dark",
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "margin": {"l": 54, "r": 22, "t": 16, "b": 34},
        "autosize": True,
        "hovermode": "x unified",
        "hoverlabel": hoverlabel,
        "legend": {"orientation": "h", "y": 1.02, "x": 0},
        "uirevision": f"{overlay.session_date.isoformat()}:{overlay.symbol}",
        "shapes": _membership_shapes(overlay),
    }
    if layout_height is not None:
        layout_options["height"] = layout_height
    figure.update_layout(
        **layout_options,
    )
    figure.update_xaxes(
        rangeslider_visible=False,
        hoverformat="%H:%M:%S ET",
        row=1,
        col=1,
    )
    figure.update_xaxes(
        rangeslider_visible=False,
        hoverformat="%H:%M:%S ET",
        row=2,
        col=1,
    )
    figure.update_xaxes(
        rangeslider_visible=True,
        rangeslider_thickness=0.14,
        title_text="Eastern Time",
        hoverformat="%H:%M:%S ET",
        row=3,
        col=1,
    )
    figure.update_yaxes(title_text="Price", row=1, col=1)
    figure.update_yaxes(title_text="Volume", row=2, col=1)
    figure.update_yaxes(visible=False, fixedrange=True, row=3, col=1)
    return figure
