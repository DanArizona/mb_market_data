"""Framework-light visual projection for one Observation Overlay snapshot."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from types import MappingProxyType
from typing import Any, Mapping, NamedTuple
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
    {"standard": 4, "big": 6}
)
OVERLAY_HEIGHT_PIXELS = 800
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
HALT_COLOR = "#d100d1"
HALT_LINE_WIDTH = 7
REQUEST_ERROR_COLOR = "#ff4d5d"
REQUEST_ERROR_LINE_WIDTH = 7


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

    style = value if isinstance(value, OverlayPointStyle) else OverlayPointStyle()
    size = style.size if style.size in POINT_SIZE_PIXELS else "big"
    opacity = min(1.0, max(0.1, float(style.opacity)))
    return OverlayPointStyle(
        visible=bool(style.visible),
        size=size,
        opacity=opacity,
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
            f"{name} {count:,}" for name, count in self.quote_status_counts.items()
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
            overlay.replay_time_utc.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
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


class OverlayHaltSegment(NamedTuple):
    start_et: datetime
    end_et: datetime
    price: float
    reason_code: str


class OverlayRequestErrorSegment(NamedTuple):
    channel: str
    start_et: datetime
    end_et: datetime
    count: int
    detail: str
    start_price: float | None
    end_price: float | None


def resolve_halt_segments(
    overlay: ObservationOverlayData,
    *,
    off_hours: bool,
) -> tuple[OverlayHaltSegment, ...]:
    """Resolve replay-visible halt spans at the first in-halt quote price."""

    replay_time_et = overlay.replay_time_utc.astimezone(ET)
    segments: list[OverlayHaltSegment] = []
    for interval in overlay.halt_intervals:
        visible_end = min(interval.end_et or replay_time_et, replay_time_et)
        if visible_end <= interval.start_et:
            continue
        if not off_hours and not _regular_hours(interval.start_et):
            continue
        price = next(
            (
                price
                for point in overlay.quote_points
                if interval.start_et
                <= point.available_at_utc.astimezone(ET)
                <= visible_end
                if (price := _quote_price(point)) is not None
            ),
            None,
        )
        if price is None:
            continue
        segments.append(
            OverlayHaltSegment(
                interval.start_et,
                visible_end,
                price,
                interval.reason_code,
            )
        )
    return tuple(segments)


def resolve_request_error_segments(
    overlay: ObservationOverlayData,
    *,
    off_hours: bool,
) -> tuple[OverlayRequestErrorSegment, ...]:
    """Group consecutive request-error outcomes by channel and slot time."""

    replay_time_et = overlay.replay_time_utc.astimezone(ET)
    segments: list[OverlayRequestErrorSegment] = []
    for channel in sorted({point.channel for point in overlay.quote_points}):
        points = sorted(
            (point for point in overlay.quote_points if point.channel == channel),
            key=lambda point: point.scheduled_at_utc,
        )
        priced_points = [
            (point.scheduled_at_utc.astimezone(ET), price)
            for point in points
            if (price := _quote_price(point)) is not None
        ]
        start_et: datetime | None = None
        count = 0
        detail = ""
        for point in points:
            scheduled_et = point.scheduled_at_utc.astimezone(ET)
            if point.status == "request_error":
                if start_et is None:
                    start_et = scheduled_et
                    detail = point.detail or "Request failed"
                count += 1
                continue
            if start_et is not None:
                _append_request_error_segment(
                    segments,
                    channel=channel,
                    start_et=start_et,
                    end_et=scheduled_et,
                    count=count,
                    detail=detail,
                    session_date=overlay.session_date,
                    off_hours=off_hours,
                    priced_points=priced_points,
                )
                start_et = None
                count = 0
                detail = ""
        if start_et is not None:
            cadence = timedelta(seconds=15 if channel == "focus" else 30)
            _append_request_error_segment(
                segments,
                channel=channel,
                start_et=start_et,
                end_et=min(
                    replay_time_et,
                    points[-1].scheduled_at_utc.astimezone(ET) + cadence,
                ),
                count=count,
                detail=detail,
                session_date=overlay.session_date,
                off_hours=off_hours,
                priced_points=priced_points,
            )
    return tuple(segments)


def _append_request_error_segment(
    segments: list[OverlayRequestErrorSegment],
    *,
    channel: str,
    start_et: datetime,
    end_et: datetime,
    count: int,
    detail: str,
    session_date: Any,
    off_hours: bool,
    priced_points: list[tuple[datetime, float]],
) -> None:
    if not off_hours:
        start_et = max(
            start_et,
            datetime.combine(session_date, REGULAR_OPEN, tzinfo=ET),
        )
        end_et = min(
            end_et,
            datetime.combine(session_date, REGULAR_CLOSE, tzinfo=ET),
        )
    if end_et <= start_et:
        return
    segments.append(
        OverlayRequestErrorSegment(
            channel,
            start_et,
            end_et,
            count,
            detail,
            _nearest_quote_price(priced_points, start_et),
            _nearest_quote_price(priced_points, end_et),
        )
    )


def _nearest_quote_price(
    priced_points: list[tuple[datetime, float]],
    target_et: datetime,
) -> float | None:
    if not priced_points:
        return None
    return min(
        priced_points,
        key=lambda item: abs((item[0] - target_et).total_seconds()),
    )[1]


def _request_error_band_price(
    overlay: ObservationOverlayData,
    *,
    off_hours: bool,
) -> float | None:
    prices = [
        value
        for candle in overlay.candles
        if off_hours or _regular_hours(candle.start_et)
        for value in (candle.low, candle.high)
    ] + [
        price
        for point in overlay.quote_points
        if off_hours or _regular_hours(point.available_at_utc)
        if (price := _quote_price(point)) is not None
    ]
    if not prices:
        return None
    lower, upper = min(prices), max(prices)
    span = upper - lower
    return upper - span * 0.08 if span > 0 else upper


def _regular_hours(value: datetime) -> bool:
    return REGULAR_OPEN <= value.astimezone(ET).time() < REGULAR_CLOSE


def default_reference(overlay: ObservationOverlayData) -> tuple[float, str] | None:
    """Use the first *visible* regular-session candle without future leakage."""

    for candle in overlay.candles:
        if _regular_hours(candle.start_et) and candle.open > 0:
            return candle.open, f"{candle.start_et:%H:%M} ET candle open"
    return None


def resolve_reference(
    overlay: ObservationOverlayData,
    selected: Mapping[str, Any] | None,
) -> tuple[float, str] | None:
    """Accept a selected price only after its source is replay-visible."""

    if isinstance(selected, Mapping):
        try:
            available_at = datetime.fromisoformat(selected["available_at_utc"])
            price = float(selected["price"])
            if (
                selected.get("session_date") == overlay.session_date.isoformat()
                and selected.get("symbol") == overlay.symbol
                and available_at.tzinfo is not None
                and available_at <= overlay.replay_time_utc
                and math.isfinite(price)
                and price > 0
            ):
                return price, str(selected.get("label") or "Selected price")
        except (KeyError, TypeError, ValueError):
            pass
    return default_reference(overlay)


def selected_reference_from_click(
    overlay: ObservationOverlayData,
    click_data: Mapping[str, Any] | None,
    figure_data: Mapping[str, Any] | None,
    candle_field: str,
) -> dict[str, Any] | None:
    """Resolve a clicked candle field or quote marker to visible evidence."""

    if candle_field not in {"open", "high", "low", "close"}:
        return None
    try:
        point = click_data["points"][0]
        trace = figure_data["data"][point["curveNumber"]]
        index = point["pointNumber"]
        if not isinstance(index, int) or index < 0:
            return None
        if trace["name"] == "5-minute OHLC":
            row = trace["customdata"][index]
            start = datetime.fromisoformat(row[0])
            price = float(
                row[{"open": 1, "high": 2, "low": 3, "close": 4}[candle_field]]
            )
            available_at = start + OHLCV_INTERVAL
            label = f"{start.astimezone(ET):%H:%M} ET candle {candle_field}"
        elif trace["name"].endswith(" quote"):
            available_value = trace["x"][index]
            available_at = (
                datetime.fromisoformat(available_value)
                if isinstance(available_value, str)
                else available_value
            )
            price = float(trace["y"][index])
            label = f"{available_at.astimezone(ET):%H:%M:%S} ET {trace['name']} last"
        else:
            return None
        if (
            available_at.tzinfo is None
            or available_at > overlay.replay_time_utc
            or not math.isfinite(price)
            or price <= 0
        ):
            return None
        return {
            "session_date": overlay.session_date.isoformat(),
            "symbol": overlay.symbol,
            "available_at_utc": available_at.isoformat(),
            "price": price,
            "label": label,
        }
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def _nice_percent_ticks(
    low: float, high: float, reference: float
) -> tuple[list[float], list[str]]:
    """Choose round percent labels while retaining price coordinates."""

    percent_low = 100 * (low / reference - 1)
    percent_high = 100 * (high / reference - 1)
    span = max(percent_high - percent_low, 0.0001)
    rough = span / 6
    magnitude = 10 ** math.floor(math.log10(rough))
    step = next(
        (v * magnitude for v in (1, 2, 5, 10) if v * magnitude >= rough), 10 * magnitude
    )
    first = math.ceil(percent_low / step)
    last = math.floor(percent_high / step)
    percents = [index * step for index in range(first, last + 1)]
    decimals = max(0, min(4, -math.floor(math.log10(step))))
    return (
        [reference * (1 + value / 100) for value in percents],
        [f"{value:.{decimals}f}%" for value in percents],
    )


def configure_overlay_axis(
    figure: Any,
    overlay: ObservationOverlayData,
    *,
    units: str,
    reference: tuple[float, str] | None,
    off_hours: bool,
) -> Any:
    """Label price coordinates in either price or percent without rescaling."""

    candles = tuple(
        c for c in overlay.candles if off_hours or _regular_hours(c.start_et)
    )
    points = tuple(
        price
        for point in overlay.quote_points
        if off_hours or _regular_hours(point.available_at_utc)
        if (price := _quote_price(point)) is not None
    )
    prices = [
        value for candle in candles for value in (candle.low, candle.high)
    ] + list(points)
    axis = figure.layout.yaxis
    if units != "percent" or reference is None:
        axis.title.text = "Price"
        axis.tickmode = "auto"
        axis.tickvals = None
        axis.ticktext = None
    else:
        price, _ = reference
        axis.title.text = "Change from reference"
        if prices:
            lower, upper = min(prices), max(prices)
            if axis.range is not None:
                lower, upper = axis.range
            else:
                margin = max((upper - lower) * 0.05, price * 0.001)
                lower -= margin
                upper += margin
            ticks, labels = _nice_percent_ticks(lower, upper, price)
            axis.tickmode = "array"
            axis.tickvals = ticks
            axis.ticktext = labels

    if reference is not None and prices:
        price, _ = reference
        data_low, data_high = min(prices), max(prices)
        for percent, dash, width, opacity in (
            (0, "solid", 2, 0.9),
            (1, "solid", 1, 0.5),
            (-1, "solid", 1, 0.5),
            (2, "dash", 1, 0.35),
            (-2, "dash", 1, 0.35),
        ):
            level = price * (1 + percent / 100)
            # A hidden off-hours reference may lie outside the visible data.
            # Shapes participate in autorange, so omit every offscreen level.
            if not data_low <= level <= data_high:
                continue
            figure.add_shape(
                type="line",
                xref="paper",
                yref="y",
                x0=0,
                x1=1,
                y0=level,
                y1=level,
                layer="above",
                line={
                    "color": f"rgba(241,185,93,{opacity})",
                    "width": width,
                    "dash": dash,
                },
            )
    return figure


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
        if isinstance(direct_range, (list, tuple)) and len(direct_range) == 2:
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
    off_hours: bool = True,
    units: str = "price",
    selected_reference: Mapping[str, Any] | None = None,
    relayout_data: Mapping[str, Any] | None = None,
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
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=(0.779, 0.22, 0.001),
    )
    candles = tuple(
        item for item in overlay.candles if off_hours or _regular_hours(item.start_et)
    )
    if candles:
        candle_times = [item.start_et + OHLCV_INTERVAL / 2 for item in candles]
        figure.add_trace(
            go.Candlestick(
                x=candle_times,
                open=[item.open for item in candles],
                high=[item.high for item in candles],
                low=[item.low for item in candles],
                close=[item.close for item in candles],
                customdata=[
                    [
                        item.start_et.isoformat(),
                        item.open,
                        item.high,
                        item.low,
                        item.close,
                    ]
                    for item in candles
                ],
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
            if off_hours or _regular_hours(point.available_at_utc)
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

    for index, segment in enumerate(
        resolve_halt_segments(overlay, off_hours=off_hours)
    ):
        figure.add_trace(
            go.Scatter(
                x=[segment.start_et, segment.end_et],
                y=[segment.price, segment.price],
                mode="lines",
                name="Nasdaq halt",
                legendgroup="nasdaq-halt",
                showlegend=index == 0,
                line={"color": HALT_COLOR, "width": HALT_LINE_WIDTH},
                customdata=[
                    [
                        segment.reason_code,
                        segment.start_et.strftime("%H:%M:%S.%f")[:-3],
                        segment.end_et.strftime("%H:%M:%S.%f")[:-3],
                    ]
                ] * 2,
                hovertemplate=(
                    "Nasdaq halt %{customdata[0]}<br>"
                    "%{customdata[1]}–%{customdata[2]} ET<br>"
                    "Halt price %{y}<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )
        figure.data[-1].xaxis = "x3"

    error_segments = resolve_request_error_segments(
        overlay,
        off_hours=off_hours,
    )
    error_band_price = _request_error_band_price(
        overlay,
        off_hours=off_hours,
    )
    for channel in (
        sorted({segment.channel for segment in error_segments})
        if error_band_price is not None
        else ()
    ):
        channel_segments = tuple(
            segment for segment in error_segments if segment.channel == channel
        )
        x: list[Any] = []
        y: list[float | None] = []
        customdata: list[list[Any] | None] = []
        for segment in channel_segments:
            values = [
                segment.channel.title(),
                segment.start_et.strftime("%H:%M:%S"),
                segment.end_et.strftime("%H:%M:%S"),
                segment.count,
                segment.detail,
            ]
            x.extend((segment.start_et, segment.end_et, None))
            y.extend((error_band_price, error_band_price, None))
            customdata.extend((values, values, None))
        figure.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode="lines",
                name=f"{channel.title()} request errors",
                legendgroup=f"{channel}-request-errors",
                line={
                    "color": REQUEST_ERROR_COLOR,
                    "width": REQUEST_ERROR_LINE_WIDTH,
                },
                customdata=customdata,
                hovertemplate=(
                    "%{customdata[0]} request errors<br>"
                    "%{customdata[1]}–%{customdata[2]} ET<br>"
                    "%{customdata[3]} outcomes<br>"
                    "%{customdata[4]}<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )
        figure.data[-1].xaxis = "x3"

        connector_x: list[Any] = []
        connector_y: list[float | None] = []
        for segment in channel_segments:
            if segment.start_price is not None:
                connector_x.extend((segment.start_et, segment.start_et, None))
                connector_y.extend(
                    (error_band_price, segment.start_price, None)
                )
            if segment.end_price is not None:
                connector_x.extend((segment.end_et, segment.end_et, None))
                connector_y.extend((error_band_price, segment.end_price, None))
        if connector_x:
            figure.add_trace(
                go.Scatter(
                    x=connector_x,
                    y=connector_y,
                    mode="lines",
                    name=f"{channel.title()} request error endpoints",
                    legendgroup=f"{channel}-request-errors",
                    showlegend=False,
                    line={
                        "color": REQUEST_ERROR_COLOR,
                        "width": 1,
                        "dash": "dot",
                    },
                    hoverinfo="skip",
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
    layout_options["height"] = OVERLAY_HEIGHT_PIXELS
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
    if not off_hours:
        figure.layout.xaxis3.range = [
            datetime.combine(overlay.session_date, REGULAR_OPEN, tzinfo=ET),
            datetime.combine(overlay.session_date, REGULAR_CLOSE, tzinfo=ET),
        ]
    apply_observation_overlay_view_state(figure, relayout_data)
    configure_overlay_axis(
        figure,
        overlay,
        units=units,
        reference=resolve_reference(overlay, selected_reference),
        off_hours=off_hours,
    )
    return figure
