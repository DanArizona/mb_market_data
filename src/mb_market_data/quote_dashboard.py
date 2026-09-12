"""Interactive Dash application for a projected quote-event replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

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
        {"field": "uni_revision", "headerName": "Uni", "width": 88},
        {
            "field": "focus_revision",
            "headerName": "Focus",
            "width": 94,
        },
        {"field": "hot_revision", "headerName": "Hot", "width": 88},
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
            "Complete"
            if status is DashboardReplayStatus.COMPLETE
            else (
                "Ready"
                if replay.applied_event_count == 0
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


def create_quote_dashboard(
    controller: QuoteDashboardReplayController,
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
        Input("replay-tick", "n_intervals"),
        Input("replay-speed", "value"),
        State("rendered-event-count", "data"),
    )
    def update_replay(
        _toggle_clicks: int | None,
        _step_clicks: int | None,
        _restart_clicks: int | None,
        _ticks: int,
        speed: float,
        rendered_event_count: int,
    ) -> tuple[Any, ...]:
        trigger = ctx.triggered_id
        if trigger == "replay-toggle":
            updated = controller.toggle()
        elif trigger == "replay-step":
            updated = controller.step()
        elif trigger == "replay-restart":
            updated = controller.restart()
        elif trigger == "replay-speed":
            updated = controller.set_speed(float(speed))
        elif trigger == "replay-tick":
            updated = controller.tick()
        else:
            updated = controller.snapshot()

        control_values = _control_values(updated)
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
