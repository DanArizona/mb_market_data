"""Dash application for inspecting a projected quote-event state."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from mb_market_data.quote_dashboard_view import QuoteDashboardView


ASSETS_DIRECTORY = Path(__file__).with_name("assets")


def _status_text(status_counts: Mapping[str, int]) -> str:
    if not status_counts:
        return "No observations"
    return " · ".join(
        f"{name} {count:,}" for name, count in status_counts.items()
    )


def create_quote_dashboard(view: QuoteDashboardView) -> Any:
    """Create a local Dash application for one projected state snapshot."""

    try:
        import dash_ag_grid as dag
        from dash import Dash, html
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Dash dependencies are not installed. Run: "
            "python -m pip install -e ."
        ) from exc

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

    column_definitions = [
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
            "width": 88,
        },
        {
            "field": "focus_revision",
            "headerName": "Focus",
            "width": 94,
        },
        {
            "field": "hot_revision",
            "headerName": "Hot",
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

    channel_cards = [
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
                    f"{_status_text(dict(summary.status_counts))}",
                    className="channel-card-detail",
                ),
            ],
            className=f"channel-card channel-{summary.channel}",
        )
        for summary in view.channels
    ]

    app.layout = html.Main(
        [
            html.Header(
                [
                    html.Div(
                        [
                            html.Div("MB", className="brand-mark"),
                            html.Div(
                                [
                                    html.H1("Market State"),
                                    html.Div(
                                        "Exact-day replay",
                                        className="mode-label",
                                    ),
                                ]
                            ),
                        ],
                        className="brand-lockup",
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
                                f"As of {view.as_of_et_text}",
                                className="as-of",
                            ),
                        ],
                        className="session-identity",
                    ),
                ],
                className="topbar",
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Span("Unique members"),
                            html.Strong(f"{view.unique_member_count:,}"),
                        ],
                        className="metric-card",
                    ),
                    html.Div(
                        [
                            html.Span("Multi-channel"),
                            html.Strong(f"{view.multi_channel_count:,}"),
                        ],
                        className="metric-card",
                    ),
                    html.Div(
                        [
                            html.Span("Events applied"),
                            html.Strong(f"{view.event_count:,}"),
                        ],
                        className="metric-card",
                    ),
                    html.Div(
                        [
                            html.Span("Observations"),
                            html.Strong(f"{view.observation_count:,}"),
                        ],
                        className="metric-card",
                    ),
                ],
                className="metric-grid",
                **{"aria-label": "Replay totals"},
            ),
            html.Section(
                channel_cards
                or [
                    html.Div(
                        "No sampling channels are present.",
                        className="empty-state",
                    )
                ],
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
                                className="row-count",
                            ),
                        ],
                        className="table-heading",
                    ),
                    dag.AgGrid(
                        id="market-state-grid",
                        className="ag-theme-quartz-dark market-grid",
                        rowData=view.grid_records(),
                        columnDefs=column_definitions,
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
                        },
                        style={"height": "min(66vh, 760px)"},
                    ),
                ],
                className="table-panel",
                **{"aria-label": "Current symbols"},
            ),
        ],
        className="dashboard-shell",
    )
    return app
