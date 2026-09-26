"""Interactive Dash application for a projected quote-event replay."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from threading import Timer
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from mb_market_data.observation_overlay import ObservationOverlayData
from mb_market_data.observation_overlay_view import (
    CHANNEL_COLORS,
    OverlayPointStyle,
    apply_observation_overlay_view_state,
    build_observation_overlay_figure,
    build_observation_overlay_view,
    normalize_overlay_height,
)
from mb_market_data.quote_dashboard_replay import (
    DashboardReplaySnapshot,
    DashboardReplayStatus,
    QuoteDashboardReplayController,
)
from mb_market_data.quote_dashboard_view import (
    QuoteDashboardView,
    build_quote_dashboard_view,
)


ASSETS_DIRECTORY = Path(__file__).with_name("assets")
ET = ZoneInfo("America/New_York")
OVERLAY_CHANNELS = ("uni", "focus", "hot")


def _overlay_point_style(
    visible: list[str] | None,
    size: str | None,
    opacity_percent: float | None,
) -> OverlayPointStyle:
    """Translate browser controls into a bounded display-only style."""

    opacity = (
        float(opacity_percent) / 100.0
        if isinstance(opacity_percent, (int, float))
        and not isinstance(opacity_percent, bool)
        else 1.0
    )
    return OverlayPointStyle(
        visible=isinstance(visible, list) and "show" in visible,
        size=size if size in {"small", "medium", "big"} else "big",
        opacity=min(1.0, max(0.1, opacity)),
    )


def _overlay_chart_class(height: str | None) -> str:
    return f"overlay-chart overlay-height-{normalize_overlay_height(height)}"


def _overlay_channel_control(html: Any, dcc: Any, channel: str) -> Any:
    label = channel.title()
    return html.Fieldset(
        [
            html.Legend(
                label,
                style={"color": CHANNEL_COLORS[channel]},
            ),
            dcc.Checklist(
                id=f"observation-overlay-{channel}-visible",
                options=[{"label": "Show", "value": "show"}],
                value=["show"],
                className="overlay-show-control",
            ),
            html.Label(
                [
                    html.Span("Point size"),
                    dcc.RadioItems(
                        id=f"observation-overlay-{channel}-size",
                        options=[
                            {"label": "Small", "value": "small"},
                            {"label": "Medium", "value": "medium"},
                            {"label": "Big", "value": "big"},
                        ],
                        value="big",
                        inline=True,
                        className="overlay-size-control",
                    ),
                ]
            ),
            html.Label(
                [
                    html.Span("Opacity"),
                    dcc.Slider(
                        id=f"observation-overlay-{channel}-opacity",
                        min=10,
                        max=100,
                        step=1,
                        value=100,
                        marks={10: "10%", 50: "50%", 100: "100%"},
                        tooltip={
                            "placement": "bottom",
                            "always_visible": False,
                        },
                    ),
                ],
                className="overlay-opacity-control",
            ),
        ],
        className=f"overlay-channel-control overlay-channel-{channel}",
    )


def _status_text(status_counts: Mapping[str, int]) -> str:
    if not status_counts:
        return "No observations"
    return " · ".join(
        f"{name} {count:,}" for name, count in status_counts.items()
    )


def _time_text(value: Any) -> str:
    if value is None:
        return "No events"
    return (
        value.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        + " ET"
    )


def _channel_cards(html: Any, view: QuoteDashboardView) -> list[Any]:
    if not view.channels:
        return [
            html.Div(
                "No sampling channels are active at this replay position.",
                className="empty-state",
            )
        ]
    return [
        html.Div(
            [
                html.Div(
                    [
                        html.Span(summary.channel.upper()),
                        html.Span(
                            f"r{summary.revision}",
                            className="revision-pill",
                        ),
                    ],
                    className="channel-card-title",
                ),
                html.Div(
                    f"{summary.member_count:,}",
                    className="channel-card-value",
                ),
                html.Div(
                    f"{summary.latest_count:,} observed · "
                    f"{_status_text(summary.status_counts)}",
                    className="channel-card-detail",
                ),
            ],
            className=f"channel-card channel-{summary.channel}",
        )
        for summary in view.channels
    ]


def _observation_overlay_panel(
    html: Any,
    dcc: Any,
    overlay: ObservationOverlayData,
) -> Any:
    view = build_observation_overlay_view(overlay)
    return html.Section(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H2(f"Observation Overlay · {view.symbol}"),
                            html.P(
                                "Completed five-minute OHLCV with "
                                "replay-causal quote and membership evidence."
                            ),
                        ]
                    ),
                    html.Span(
                        view.current_band_label,
                        id="observation-overlay-band",
                        className="overlay-band",
                        style={
                            "borderColor": view.current_band_color,
                            "color": view.current_band_color,
                        },
                    ),
                ],
                className="overlay-heading",
            ),
            html.Div(
                [
                    html.Span(
                        view.replay_time_et_text,
                        id="observation-overlay-as-of",
                    ),
                    html.Span(
                        view.evidence_text,
                        id="observation-overlay-evidence",
                    ),
                ],
                className="overlay-metadata",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Strong("Quote points"),
                            html.Span(
                                "Display settings only; evidence is unchanged."
                            ),
                        ],
                        className="overlay-control-intro",
                    ),
                    *[
                        _overlay_channel_control(html, dcc, channel)
                        for channel in OVERLAY_CHANNELS
                    ],
                    html.Fieldset(
                        [
                            html.Legend("Plot height"),
                            dcc.RadioItems(
                                id="observation-overlay-height",
                                options=[
                                    {
                                        "label": "Standard",
                                        "value": "standard",
                                    },
                                    {"label": "Tall", "value": "tall"},
                                    {"label": "Full window", "value": "full"},
                                ],
                                value="standard",
                                inline=True,
                                className="overlay-height-control",
                            ),
                        ],
                        className="overlay-height-picker",
                    ),
                ],
                className="overlay-display-controls",
                **{"aria-label": "Observation Overlay display controls"},
            ),
            dcc.Graph(
                id="observation-overlay-chart",
                figure=build_observation_overlay_figure(overlay),
                config={
                    "displaylogo": False,
                    "responsive": True,
                    "scrollZoom": True,
                },
                className=_overlay_chart_class("standard"),
            ),
        ],
        id="observation-overlay-panel",
        className="overlay-panel",
        **{"aria-label": f"Observation Overlay for {view.symbol}"},
    )


def _column_definitions() -> list[dict[str, Any]]:
    return [
        {
            "field": "symbol",
            "headerName": "Symbol",
            "pinned": "left",
            "width": 112,
            "cellClass": "symbol-cell",
        },
        {
            "field": "channels",
            "headerName": "Channels",
            "width": 150,
            "cellClass": "channel-cell",
        },
        {
            "field": "uni_revision",
            "headerName": "Uni",
            "headerTooltip": (
                "Active Uni membership revision; blank means the symbol "
                "is not currently in Uni."
            ),
            "width": 88,
        },
        {
            "field": "focus_revision",
            "headerName": "Focus",
            "headerTooltip": (
                "Active Focus membership revision; blank means the symbol "
                "is not currently in Focus."
            ),
            "width": 94,
        },
        {
            "field": "hot_revision",
            "headerName": "Hot",
            "headerTooltip": (
                "Active Hot membership revision; blank means the symbol "
                "is not currently in Hot."
            ),
            "width": 88,
        },
        {
            "field": "status",
            "headerName": "Status",
            "width": 126,
            "cellClassRules": {
                "status-quote": "params.value === 'quote'",
                "status-invalid": "params.value === 'invalid'",
                "status-missing": "params.value === 'missing'",
                "status-error": "params.value === 'request_error'",
            },
        },
        {
            "field": "latest_channel",
            "headerName": "Latest via",
            "headerTooltip": (
                "Sampling channel that supplied the newest displayed "
                "observation."
            ),
            "width": 112,
        },
        {
            "field": "last_price",
            "headerName": "Last",
            "type": "numericColumn",
            "width": 112,
        },
        {
            "field": "bid_price",
            "headerName": "Bid",
            "type": "numericColumn",
            "width": 104,
        },
        {
            "field": "ask_price",
            "headerName": "Ask",
            "type": "numericColumn",
            "width": 104,
        },
        {
            "field": "mark",
            "headerName": "Mark",
            "headerTooltip": (
                "Schwab mark price from the newest displayed quote "
                "observation."
            ),
            "type": "numericColumn",
            "width": 104,
        },
        {
            "field": "total_volume",
            "headerName": "Volume",
            "type": "numericColumn",
            "width": 132,
        },
        {
            "field": "observed_at_et",
            "headerName": "Observed ET",
            "width": 224,
        },
        {
            "field": "exchange",
            "headerName": "Exchange",
            "width": 112,
        },
        {
            "field": "description",
            "headerName": "Description",
            "minWidth": 260,
            "flex": 1,
        },
    ]


def _control_values(
    replay: DashboardReplaySnapshot,
) -> tuple[str, bool, bool, bool, str, str, str, str, dict[str, str]]:
    status = replay.status
    status_label = (
        "Playing"
        if status is DashboardReplayStatus.PLAYING
        else (
            "End of recorded data"
            if status is DashboardReplayStatus.COMPLETE
            else (
                "Ready"
                if (
                    replay.applied_event_count == 0
                    and replay.replay_time_utc
                    == replay.first_event_time_utc
                )
                else "Paused"
            )
        )
    )
    percent = replay.progress_fraction * 100.0
    return (
        "Pause" if status is DashboardReplayStatus.PLAYING else "Play",
        status is DashboardReplayStatus.COMPLETE,
        status is not DashboardReplayStatus.PAUSED,
        status is not DashboardReplayStatus.PLAYING,
        status_label,
        f"replay-status status-{status.value}",
        _time_text(replay.replay_time_utc),
        (
            f"{replay.applied_event_count:,} / "
            f"{replay.total_event_count:,} events · {percent:.1f}%"
        ),
        {"width": f"{percent:.3f}%"},
    )


def _parse_seek_time_utc(
    session_date: date,
    value: str | None,
) -> datetime:
    """Parse an ET wall-clock second as an inclusive replay cutoff."""

    text = value.strip() if isinstance(value, str) else ""
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", text) is None:
        raise ValueError("Seek time must use HH:MM:SS ET.")
    hour, minute, second = (int(part) for part in text.split(":"))
    target_et = datetime.combine(
        session_date,
        time(hour, minute, second, 999_999),
        tzinfo=ET,
    )
    return target_et.astimezone(timezone.utc)


def create_quote_dashboard(
    controller: QuoteDashboardReplayController,
    *,
    request_server_stop: Callable[[], None] | None = None,
) -> Any:
    """Create a local interactive dashboard for one replay controller."""

    try:
        import dash_ag_grid as dag
        from dash import (
            Dash,
            Input,
            Output,
            State,
            ctx,
            dcc,
            html,
            no_update,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Dash dependencies are not installed. Run: "
            "python -m pip install -e ."
        ) from exc

    replay = controller.snapshot()
    view = build_quote_dashboard_view(replay.state)
    controls = _control_values(replay)

    app = Dash(
        __name__,
        title="MasterBot Market State",
        assets_folder=str(ASSETS_DIRECTORY),
    )
    app.index_string = """<!DOCTYPE html>
<html lang="en">
  <head>
    {%metas%}
    <title>{%title%}</title>
    {%favicon%}
    <link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
    {%css%}
  </head>
  <body>
    {%app_entry%}
    <footer>{%config%}{%scripts%}{%renderer%}</footer>
  </body>
</html>"""

    app.layout = html.Main(
        [
            dcc.Interval(
                id="replay-tick",
                interval=250,
                disabled=controls[3],
            ),
            dcc.Store(
                id="rendered-event-count",
                data=replay.applied_event_count,
            ),
            dcc.Store(
                id="theme-preference",
                data="dark",
                storage_type="local",
            ),
            html.Header(
                [
                    html.Div(
                        [
                            html.Div("MB", className="brand-mark"),
                            html.Div(
                                [
                                    html.H1("Market State"),
                                    html.Div(
                                        "Interactive exact-day replay",
                                        className="mode-label",
                                    ),
                                ]
                            ),
                        ],
                        className="brand-lockup",
                    ),
                    html.Div(
                        [
                            *(
                                [
                                    html.Button(
                                        "Stop server",
                                        id="server-stop",
                                        className=(
                                            "theme-toggle server-stop-button"
                                        ),
                                        title=(
                                            "Gracefully stop this local "
                                            "dashboard server"
                                        ),
                                    ),
                                    dcc.ConfirmDialog(
                                        id="server-stop-confirm",
                                        message=(
                                            "Stop the local dashboard server?"
                                        ),
                                    ),
                                    html.Span(
                                        "",
                                        id="server-stop-status",
                                        className="server-stop-status",
                                        **{"aria-live": "polite"},
                                    ),
                                ]
                                if request_server_stop is not None
                                else []
                            ),
                            html.Button(
                                "Light mode",
                                id="theme-toggle",
                                className="theme-toggle",
                                title=(
                                    "Switch display theme; printing always "
                                    "uses the light print theme"
                                ),
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        view.session_date.isoformat()
                                        if view.session_date
                                        else "No session",
                                        className="session-date",
                                    ),
                                    html.Div(
                                        f"State as of {view.as_of_et_text}",
                                        id="state-as-of",
                                        className="as-of",
                                    ),
                                ],
                                className="session-identity",
                            ),
                        ],
                        className="session-tools",
                    ),
                ],
                className="topbar",
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Button(
                                controls[0],
                                id="replay-toggle",
                                disabled=controls[1],
                                className="control-button control-primary",
                            ),
                            html.Button(
                                "Step",
                                id="replay-step",
                                disabled=controls[2],
                                className="control-button",
                            ),
                            html.Button(
                                "Restart",
                                id="replay-restart",
                                className="control-button",
                            ),
                            html.Label(
                                [
                                    html.Span("Seek time ET"),
                                    dcc.Input(
                                        id="replay-seek-time",
                                        type="text",
                                        placeholder="HH:MM:SS",
                                        maxLength=8,
                                        debounce=True,
                                        className="replay-seek-input",
                                    ),
                                ],
                                className="seek-control",
                                title=(
                                    "Seek within this journal day using "
                                    "Eastern Time"
                                ),
                            ),
                            html.Button(
                                "Seek",
                                id="replay-seek",
                                className="control-button",
                            ),
                        ],
                        className="button-row",
                    ),
                    html.Label(
                        [
                            html.Span("Replay speed"),
                            dcc.Dropdown(
                                id="replay-speed",
                                options=[
                                    {
                                        "label": "1× real time",
                                        "value": 1,
                                    },
                                    {"label": "10×", "value": 10},
                                    {
                                        "label": "60× · ~6½ min",
                                        "value": 60,
                                    },
                                    {
                                        "label": "390× · ~1 min",
                                        "value": 390,
                                    },
                                ],
                                value=replay.speed,
                                clearable=False,
                                searchable=False,
                                className="replay-speed-dropdown",
                            ),
                        ],
                        className="speed-control",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span(
                                        controls[4],
                                        id="replay-status",
                                        className=controls[5],
                                    ),
                                    html.Strong(
                                        controls[6],
                                        id="replay-clock",
                                    ),
                                ],
                                className="replay-readout",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        id="replay-progress-fill",
                                        className="replay-progress-fill",
                                        style=controls[8],
                                    )
                                ],
                                className="replay-progress-track",
                            ),
                            html.Div(
                                controls[7],
                                id="replay-progress-label",
                                className="progress-label",
                            ),
                        ],
                        className="replay-position",
                    ),
                ],
                className="replay-controls",
                **{"aria-label": "Replay controls"},
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Span("Unique members"),
                            html.Strong(
                                f"{view.unique_member_count:,}",
                                id="metric-unique",
                            ),
                        ],
                        className="metric-card",
                    ),
                    html.Div(
                        [
                            html.Span("Multi-channel"),
                            html.Strong(
                                f"{view.multi_channel_count:,}",
                                id="metric-multi",
                            ),
                        ],
                        className="metric-card",
                    ),
                    html.Div(
                        [
                            html.Span("Events applied"),
                            html.Strong(
                                f"{view.event_count:,}",
                                id="metric-events",
                            ),
                        ],
                        className="metric-card",
                    ),
                    html.Div(
                        [
                            html.Span("Observations"),
                            html.Strong(
                                f"{view.observation_count:,}",
                                id="metric-observations",
                            ),
                        ],
                        className="metric-card",
                    ),
                ],
                className="metric-grid",
                **{"aria-label": "Replay totals"},
            ),
            html.Section(
                _channel_cards(html, view),
                id="channel-grid",
                className="channel-grid",
                **{"aria-label": "Sampling channels"},
            ),
            *(
                [
                    _observation_overlay_panel(
                        html,
                        dcc,
                        replay.observation_overlay,
                    )
                ]
                if replay.observation_overlay is not None
                else []
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H2("Current symbols"),
                                    html.P(
                                        "Sort any column or use the filters "
                                        "beneath the headings."
                                    ),
                                ]
                            ),
                            html.Div(
                                f"{len(view.rows):,} rows",
                                id="row-count",
                                className="row-count",
                            ),
                        ],
                        className="table-heading",
                    ),
                    dag.AgGrid(
                        id="market-state-grid",
                        className="ag-theme-quartz-dark market-grid",
                        rowData=view.grid_records(),
                        columnDefs=_column_definitions(),
                        defaultColDef={
                            "sortable": True,
                            "filter": True,
                            "floatingFilter": True,
                            "resizable": True,
                        },
                        dashGridOptions={
                            "animateRows": False,
                            "tooltipShowDelay": 350,
                            "tooltipHideDelay": 10_000,
                            "pagination": True,
                            "paginationPageSize": 50,
                            "paginationPageSizeSelector": [25, 50, 100, 250],
                            "enableCellTextSelection": True,
                            "ensureDomOrder": True,
                            "getRowId": {"function": "params.data.symbol"},
                        },
                        style={"height": "min(58vh, 700px)"},
                    ),
                ],
                className="table-panel",
                **{"aria-label": "Current symbols"},
            ),
        ],
        id="dashboard-page",
        className="dashboard-shell theme-dark",
    )

    @app.callback(
        Output("theme-preference", "data"),
        Input("theme-toggle", "n_clicks"),
        State("theme-preference", "data"),
        prevent_initial_call=True,
    )
    def remember_theme(
        _clicks: int,
        current_theme: str | None,
    ) -> str:
        return "dark" if current_theme == "light" else "light"

    @app.callback(
        Output("dashboard-page", "className"),
        Output("theme-toggle", "children"),
        Input("theme-preference", "modified_timestamp"),
        State("theme-preference", "data"),
    )
    def apply_theme(
        _modified_timestamp: int | None,
        stored_theme: str | None,
    ) -> tuple[str, str]:
        theme = "light" if stored_theme == "light" else "dark"
        next_theme = "Dark mode" if theme == "light" else "Light mode"
        return f"dashboard-shell theme-{theme}", next_theme

    if replay.observation_overlay is not None:

        @app.callback(
            Output("observation-overlay-band", "children"),
            Output("observation-overlay-band", "style"),
            Output("observation-overlay-as-of", "children"),
            Output("observation-overlay-evidence", "children"),
            Output("observation-overlay-chart", "figure"),
            Output("observation-overlay-chart", "className"),
            Input("replay-clock", "children"),
            Input("theme-preference", "modified_timestamp"),
            Input("observation-overlay-uni-visible", "value"),
            Input("observation-overlay-uni-size", "value"),
            Input("observation-overlay-uni-opacity", "value"),
            Input("observation-overlay-focus-visible", "value"),
            Input("observation-overlay-focus-size", "value"),
            Input("observation-overlay-focus-opacity", "value"),
            Input("observation-overlay-hot-visible", "value"),
            Input("observation-overlay-hot-size", "value"),
            Input("observation-overlay-hot-opacity", "value"),
            Input("observation-overlay-height", "value"),
            State("theme-preference", "data"),
            State("observation-overlay-chart", "relayoutData"),
        )
        def update_observation_overlay(
            _replay_clock: str,
            _theme_modified: int | None,
            uni_visible: list[str] | None,
            uni_size: str | None,
            uni_opacity: float | None,
            focus_visible: list[str] | None,
            focus_size: str | None,
            focus_opacity: float | None,
            hot_visible: list[str] | None,
            hot_size: str | None,
            hot_opacity: float | None,
            height: str | None,
            stored_theme: str | None,
            relayout_data: Mapping[str, Any] | None,
        ) -> tuple[Any, ...]:
            overlay = controller.snapshot().observation_overlay
            if overlay is None:
                return (no_update,) * 6
            overlay_view = build_observation_overlay_view(overlay)
            theme = "light" if stored_theme == "light" else "dark"
            figure = build_observation_overlay_figure(
                overlay,
                theme=theme,
                point_styles={
                    "uni": _overlay_point_style(
                        uni_visible, uni_size, uni_opacity
                    ),
                    "focus": _overlay_point_style(
                        focus_visible, focus_size, focus_opacity
                    ),
                    "hot": _overlay_point_style(
                        hot_visible, hot_size, hot_opacity
                    ),
                },
                height=normalize_overlay_height(height),
            )
            apply_observation_overlay_view_state(figure, relayout_data)
            return (
                overlay_view.current_band_label,
                {
                    "borderColor": overlay_view.current_band_color,
                    "color": overlay_view.current_band_color,
                },
                overlay_view.replay_time_et_text,
                overlay_view.evidence_text,
                figure,
                _overlay_chart_class(height),
            )

    if request_server_stop is not None:

        @app.callback(
            Output("server-stop-confirm", "displayed"),
            Input("server-stop", "n_clicks"),
            prevent_initial_call=True,
        )
        def confirm_server_stop(_clicks: int) -> bool:
            return True

        @app.callback(
            Output("server-stop-status", "children"),
            Input("server-stop-confirm", "submit_n_clicks"),
            prevent_initial_call=True,
        )
        def stop_server(_submit_clicks: int) -> str:
            timer = Timer(0.2, request_server_stop)
            timer.daemon = True
            timer.start()
            return "Stopping…"

    @app.callback(
        Output("replay-toggle", "children"),
        Output("replay-toggle", "disabled"),
        Output("replay-step", "disabled"),
        Output("replay-tick", "disabled"),
        Output("replay-status", "children"),
        Output("replay-status", "className"),
        Output("replay-clock", "children"),
        Output("replay-progress-label", "children"),
        Output("replay-progress-fill", "style"),
        Output("state-as-of", "children"),
        Output("metric-unique", "children"),
        Output("metric-multi", "children"),
        Output("metric-events", "children"),
        Output("metric-observations", "children"),
        Output("row-count", "children"),
        Output("channel-grid", "children"),
        Output("market-state-grid", "rowData"),
        Output("rendered-event-count", "data"),
        Input("replay-toggle", "n_clicks"),
        Input("replay-step", "n_clicks"),
        Input("replay-restart", "n_clicks"),
        Input("replay-seek", "n_clicks"),
        Input("replay-tick", "n_intervals"),
        Input("replay-speed", "value"),
        State("rendered-event-count", "data"),
        State("replay-seek-time", "value"),
    )
    def update_replay(
        _toggle_clicks: int | None,
        _step_clicks: int | None,
        _restart_clicks: int | None,
        _seek_clicks: int | None,
        _ticks: int,
        speed: float,
        rendered_event_count: int,
        seek_time: str | None,
    ) -> tuple[Any, ...]:
        trigger = ctx.triggered_id
        selected_speed = float(speed)
        if controller.snapshot().speed != selected_speed:
            controller.set_speed(selected_speed)

        seek_error: str | None = None
        if trigger == "replay-toggle":
            updated = controller.toggle()
        elif trigger == "replay-step":
            updated = controller.step()
        elif trigger == "replay-restart":
            updated = controller.restart()
        elif trigger == "replay-seek":
            current = controller.snapshot()
            session_date = current.state.session_date
            try:
                if session_date is None:
                    raise ValueError("This replay has no session date.")
                target_utc = _parse_seek_time_utc(
                    session_date,
                    seek_time,
                )
                updated = controller.seek(target_utc)
            except ValueError as exc:
                seek_error = str(exc)
                updated = controller.snapshot()
        elif trigger == "replay-speed":
            updated = controller.snapshot()
        elif trigger == "replay-tick":
            updated = controller.tick()
        else:
            updated = controller.snapshot()

        control_values: tuple[Any, ...] = _control_values(updated)
        if seek_error is not None:
            error_values = list(control_values)
            error_values[4] = seek_error
            error_values[5] = "replay-status status-error"
            control_values = tuple(error_values)
        state_changed = (
            updated.applied_event_count != rendered_event_count
        )
        if state_changed:
            updated_view = build_quote_dashboard_view(updated.state)
            state_values: tuple[Any, ...] = (
                f"State as of {updated_view.as_of_et_text}",
                f"{updated_view.unique_member_count:,}",
                f"{updated_view.multi_channel_count:,}",
                f"{updated_view.event_count:,}",
                f"{updated_view.observation_count:,}",
                f"{len(updated_view.rows):,} rows",
                _channel_cards(html, updated_view),
                updated_view.grid_records(),
                updated.applied_event_count,
            )
        else:
            state_values = (no_update,) * 9

        return (*control_values, *state_values)

    return app
