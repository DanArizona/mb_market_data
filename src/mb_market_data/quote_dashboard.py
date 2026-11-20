"""Interactive Dash application for a projected quote-event replay."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from threading import Timer
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from mb_market_data.membership_browser import (
    TIERS,
    membership_tier_records,
    selected_symbol_details,
)
from mb_market_data.observation_overlay import ObservationOverlayData
from mb_market_data.observation_overlay_view import (
    CHANNEL_COLORS,
    OverlayPointStyle,
    build_observation_overlay_figure,
    build_observation_overlay_view,
    resolve_overlay_time_viewport,
    resolve_reference,
    selected_reference_from_click,
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
ACTIVITY_SECTIONS = ("information", "graph", "replay", "backtest", "trading")

from .overlay_membership_bridge import SyntheticOverlayMembership
from .daily_operator_ui import OperatorProblem
from .daily_membership import normalized



def _activity_button(html: Any, section: str, glyph: str, title: str, *, active: bool) -> Any:
    return html.Button(
        [
            html.Span(glyph, className="activity-glyph", **{"aria-hidden": "true"}),
            html.Span(title, className="activity-accessible-label"),
        ],
        id=f"activity-{section}",
        className="activity-button" + (" activity-button-selected" if active else ""),
        title=title,
        type="button",
        **{"aria-label": title},
    )


def _overlay_point_style(
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
        size=size if size in {"standard", "big"} else "standard",
        opacity=min(1.0, max(0.1, opacity)),
    )


def _overlay_channel_control(html: Any, dcc: Any, channel: str) -> Any:
    label = channel.title()
    return html.Fieldset(
        [
            html.Legend(
                label,
                style={"color": CHANNEL_COLORS[channel]},
            ),
            html.Label(
                [
                    html.Span("Point size"),
                    dcc.RadioItems(
                        id=f"observation-overlay-{channel}-size",
                        options=[
                            {"label": "Standard", "value": "standard"},
                            {"label": "Big", "value": "big"},
                        ],
                        value="standard",
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


def _overlay_display_controls(html: Any, dcc: Any) -> Any:
    """Reusable chart controls, mounted once in the workspace side panel."""

    return html.Div(
        [
            html.Fieldset(
                [
                    html.Legend("Hover"),
                    dcc.Checklist(
                        id="observation-overlay-detailed-hover",
                        options=[{"label": "Detailed hover", "value": "show"}],
                        value=[],
                    ),
                    html.Small("Hold Shift for bars and features."),
                ],
                className="overlay-session-control",
            ),
            html.Fieldset(
                [
                    html.Legend("Vertical axis"),
                    dcc.RadioItems(
                        id="observation-overlay-units",
                        options=[
                            {"label": "Price", "value": "price"},
                            {"label": "Percent", "value": "percent"},
                        ],
                        value="price",
                        className="overlay-units-control",
                    ),
                    html.Label(
                        [
                            html.Span("Candle price on click"),
                            dcc.RadioItems(
                                id="observation-overlay-candle-field",
                                options=[
                                    {"label": name.title(), "value": name}
                                    for name in ("open", "high", "low", "close")
                                ],
                                value="close",
                                className="overlay-candle-field",
                            ),
                        ]
                    ),
                    html.Button(
                        "Set 0% reference",
                        id="observation-overlay-set-reference",
                        className="control-button overlay-reference-button",
                    ),
                    html.Button(
                        "Reset to first regular open",
                        id="observation-overlay-reset-reference",
                        className="overlay-reset-button",
                    ),
                    html.Div(
                        id="observation-overlay-reference-label",
                        className="overlay-reference-label",
                    ),
                ],
                className="overlay-axis-control",
            ),
            html.Div(
                [
                    html.Strong("Quote points"),
                    html.Span("Display settings only; evidence is unchanged."),
                ],
                className="overlay-control-intro",
            ),
            *[
                _overlay_channel_control(html, dcc, channel)
                for channel in OVERLAY_CHANNELS
            ],
        ],
        className="overlay-display-controls",
        **{"aria-label": "Observation Overlay display controls"},
    )


def _status_text(status_counts: Mapping[str, int]) -> str:
    if not status_counts:
        return "No observations"
    return " · ".join(f"{name} {count:,}" for name, count in status_counts.items())


def _time_text(value: Any) -> str:
    if value is None:
        return "No events"
    return value.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " ET"


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


def _membership_price(value: float | None) -> str:
    if value is None:
        return "Unavailable"
    return f"${value:,.4f}" if abs(value) < 1 else f"${value:,.2f}"


def _membership_inspector(html: Any, view: QuoteDashboardView, symbol: str | None) -> Any:
    """Show only evidence available at this replay time; never infer a block."""

    details = selected_symbol_details(view, symbol)
    if details is None:
        return html.Div(
            [
                html.H3("Symbol details"),
                html.P(
                    "Select a current member to inspect it. The selected symbol "
                    "may not yet have been admitted at this replay time."
                ),
            ],
            className="membership-details-empty",
        )

    band = details.highest_tier.title()
    status = details.security_status or "Unavailable"
    cap = (
        f"Approx. ${details.estimated_market_cap:,.0f} "
        "(latest quote × shares outstanding)"
        if details.estimated_market_cap is not None
        else "Unavailable in recorded quote"
    )
    change = (
        f"{details.change_from_close_percent:+.2f}%"
        if details.change_from_close_percent is not None
        else "Unavailable"
    )
    fields = (
        ("Membership", f"{band} · " + ", ".join(c.upper() for c in details.channels)),
        ("Revisions", ", ".join(f"{c.upper()} r{r}" for c, r in sorted(details.revisions.items()))),
        ("Last price", _membership_price(details.last_price)),
        ("Reference (quote close)", _membership_price(details.previous_close)),
        ("Change vs quote close", change),
        ("Volume", f"{details.volume:,}" if details.volume is not None else "Unavailable"),
        ("Today's open", _membership_price(details.day_open)),
        ("Today's range", f"{_membership_price(details.day_low)} – {_membership_price(details.day_high)}" if details.day_low is not None and details.day_high is not None else "Unavailable"),
        ("Estimated market cap", cap),
        ("Security status", status),
        ("Quote outcome", details.observation_status),
        ("Latest observation", f"{details.observed_at_et} via {details.latest_channel.upper()}" if details.observed_at_et and details.latest_channel else "No quote observed yet"),
        ("Promotion block", "Not recorded in this quote journal; status unknown"),
    )
    return html.Div(
        [
            html.Div(
                [
                    html.Div([html.H3(details.symbol), html.P(details.description or "Company name unavailable")]),
                    html.Span(band.upper(), className=f"membership-band membership-band-{details.highest_tier}"),
                ],
                className="membership-details-heading",
            ),
            html.Dl(
                [html.Div([html.Dt(label), html.Dd(value)]) for label, value in fields],
                className="membership-details-grid",
            ),
            html.P(
                "Historical · read-only. This information is from saved, "
                "replay-causal observations; it is not a live price feed. "
                "A rest-of-day suppression policy is not implied by an ordinary demotion.",
                className="membership-details-note",
            ),
        ]
    )


def _membership_tier_grid(html: Any, dag: Any, tier: str, records: list[dict[str, Any]], *, drag_enabled: bool = False) -> Any:
    name = {"uni": "Uni only", "focus": "Focus only", "hot": "Hot"}[tier]
    return html.Section(
        [
            html.Div(
                [
                    html.H3(name),
                    html.Span(f"{len(records):,}", id=f"membership-{tier}-count", className="membership-tier-count"),
                ],
                className="membership-tier-heading",
            ),
            dag.AgGrid(
                id=f"membership-{tier}-grid",
                className="ag-theme-quartz-dark market-grid membership-symbol-grid",
                rowData=records,
                columnDefs=[
                    {"field": "symbol", "headerName": "Symbol", "minWidth": 82, "flex": 1,
                     "pinned": "left", "cellClass": "symbol-cell", "sort": "asc",
                     "rowDrag": drag_enabled,
                     "sortingOrder": ["asc", "desc"],
                     "headerTooltip": "Click to sort symbols ascending or descending"},
                    {"field": "last_price", "headerName": "Last", "type": "numericColumn",
                     "minWidth": 70, "width": 82, "maxWidth": 110,
                     "sortingOrder": ["asc", "desc"]},
                ],
                defaultColDef={"sortable": True, "filter": False, "floatingFilter": False, "resizable": True},
                dashGridOptions={
                    "rowSelection": "single",
                    # The one selected row across all three grids is owned by
                    # the membership click callback, not by each grid alone.
                    "suppressRowClickSelection": True,
                    "animateRows": False,
                    # Sorted rows must never be reordered before Preview/Confirm.
                    "rowDragManaged": drag_enabled,
                    "rowDragMultiRow": False,
                    "suppressMoveWhenRowDragging": True,
                    "suppressRowDrag": True,  # Enabled only by the synthetic JS binder.
                    "getRowId": {"function": "params.data.symbol"},
                    "tooltipShowDelay": 350,
                },
                selectedRows=[],
                persistence=False,
                style={"height": "min(42vh, 480px)"},
            ),
        ],
        className=f"membership-tier membership-tier-{tier}",
        id=f"membership-tier-{tier}",
    )



def _synthetic_membership_inspector(html: Any, snapshot: dict[str, Any]) -> Any:
    """Show synthetic quote provenance without implying real market coverage."""
    item = snapshot.get("selected")
    if not item:
        return html.P("Select a symbol in any list, or search above.")
    price = lambda key: _membership_price(item.get(key))
    number = lambda key: f"{item[key]:,}" if isinstance(item.get(key), int) else "Unavailable"
    cap = item.get("estimated_market_cap")
    fields = (
        ("Current tier", item["tier"].upper()),
        ("Last", price("last")),
        ("Previous close", price("close")),
        ("Open", price("open")),
        ("Day range", f"{price('low')} – {price('high')}"),
        ("Volume", number("volume")),
        ("Estimated market cap", f"${cap:,.0f}" if isinstance(cap, (float, int)) else "Unavailable"),
        ("Security status", item.get("security_status") or "Unavailable"),
        ("Latest quote channel", (item.get("channel") or "Unavailable").upper()),
        ("Quote time UTC", item.get("quote_time_utc") or "Unavailable"),
        ("Last trade UTC", item.get("last_trade_time_utc") or "Unavailable"),
        ("API response UTC", item.get("api_response_utc") or "Unavailable"),
        ("Acquisition completed UTC", item.get("recorded_at_utc") or "Unavailable"),
        ("Effective revision", str(snapshot.get("effective_revision"))),
    )
    return html.Div([
        html.Div([
            html.Div([
                html.H3(item["symbol"]),
                html.P(item.get("description") or "Synthetic security name unavailable"),
            ]),
            html.Span(item["tier"].upper(),
                      className=f"membership-band membership-band-{item['tier']}"
                      if item["tier"] in TIERS else "membership-band"),
        ], className="membership-details-heading"),
        html.Dl([html.Div([html.Dt(label), html.Dd(value)]) for label, value in fields],
                className="membership-details-grid"),
        html.P("OFFLINE SYNTHETIC QA · Fabricated quotes. Not a live Schwab feed.",
               className="membership-details-note"),
    ])


def _membership_search_tier(records: Mapping[str, list[dict[str, Any]]],
                            query: str | None) -> tuple[str, str | None]:
    """Find an exact symbol in the three exclusive tiers, regardless of channel."""
    symbol = (query or "").strip().upper()
    if not symbol or len(symbol) > 20:
        return symbol, None
    for tier in TIERS:
        if any(row.get("symbol") == symbol for row in records.get(tier, ())):
            return symbol, tier
    return symbol, None



def _synthetic_operator_controls(html: Any, dcc: Any) -> Any:
    return html.Section([
        html.H3("Manual membership · OFFLINE SYNTHETIC QA"),
        html.P("Preview and confirm using the disposable membership worker. Real production controls are disabled."),
        html.Div([
            html.Label(["Symbol", dcc.Input(id="operator-symbol", type="text", placeholder="e.g. INTC", maxLength=20)]),
            html.Label(["Action", dcc.Dropdown(id="operator-action", options=[
                {"label": "Promote", "value": "promote"},
                {"label": "Ordinary demote", "value": "demote"},
            ], value="demote", clearable=False, className="operator-bridge-dropdown")]),
            html.Label(["Destination", dcc.Dropdown(id="operator-target", options=[
                {"label": "Uni", "value": "uni"},
                {"label": "Focus", "value": "focus"},
                {"label": "Hot", "value": "hot"},
            ], value="focus", clearable=False, className="operator-bridge-dropdown")]),
        ], className="operator-bridge-inputs"),
        html.Button("Preview action", id="operator-preview-button", n_clicks=0),
        html.Div(id="operator-preview-status", role="status"),
        html.Button("Confirm and queue request", id="operator-confirm-button", disabled=True, n_clicks=0),
        html.Div(id="operator-confirm-status", role="status"),
        html.Div(id="operator-receipt-status", role="status"),
        dcc.Store(id="operator-preview-proposal"),
        dcc.Store(id="operator-command-id"),
        dcc.Store(id="membership-drag-request"),
        dcc.Store(id="membership-drag-binding-status"),
        dcc.Store(id="membership-admission-candidate"),
        dcc.Interval(id="operator-receipt-tick", interval=1000),
    ], id="operator-bridge-panel", style={"display": "none"}, className="operator-bridge-panel")


def _membership_panel(html: Any, dag: Any, view: QuoteDashboardView, selected_symbol: str | None, *, operator_enabled: bool = False, dcc: Any = None) -> Any:
    by_tier = membership_tier_records(view)
    return html.Section(
        [
            html.Div(
                [
                    html.Div([
                        html.H2("Membership explorer"),
                        html.P("Three sorted lists and one shared symbol finder. Click a column heading to reverse its sort."),
                    ]),
                    html.Span("HISTORICAL · READ-ONLY", className="membership-readonly", id="membership-context-status"),
                ],
                className="membership-heading",
            ),
            html.P("Each symbol appears once, in its highest tier: Uni only, Focus (not Hot), or Hot. Underlying membership remains nested: Hot ⊆ Focus ⊆ Uni.", className="membership-explanation"),
            *([html.P(
                "Synthetic Real Time: drag a symbol by its grip to another tier to preview; "
                "the actual membership stays unchanged until explicit Confirm and worker effectiveness.",
                className="membership-drag-instructions",
            ), html.Div("Drag-and-drop disabled in Historical mode.",
                        id="membership-drag-status", role="status",
                        className="membership-drag-status")] if operator_enabled else []),
            html.Div(
                [
                    *[_membership_tier_grid(html, dag, tier, by_tier[tier],
                                            drag_enabled=operator_enabled) for tier in TIERS],
                    html.Div([
                        html.Section([
                            html.H3("Symbol search", className="membership-search-title"),
                            html.Div([
                                dcc.Input(id="membership-search-symbol", type="text",
                                          placeholder="Ticker", maxLength=20,
                                          autoComplete="off", n_submit=0,
                                          className="membership-search-input"),
                                html.Button("Find", id="membership-find-button",
                                            n_clicks=0, className="membership-find-button"),
                            ], className="membership-search-row"),
                        ], id="membership-search-panel", className="membership-search-panel",
                           **{"aria-label": "Search symbol across all three tiers"}),
                        *([html.Section([
                            html.Div([
                                html.Strong(id="membership-admission-label"),
                                html.Span("UNVERIFIED", className="membership-admission-unverified"),
                            ], className="membership-admission-heading"),
                            html.P(
                                "Ticker format accepted; equity identity is not yet verified. "
                                "The real worker validates exact EQUITY identity and capacity "
                                "before publication; this offline demonstration simulates identity.",
                                className="membership-admission-explanation",
                            ),
                            html.Button("Preview admission to Uni",
                                        id="membership-admit-button", n_clicks=0,
                                        className="membership-admit-button"),
                        ], id="membership-outside-panel", style={"display": "none"},
                           className="membership-outside-panel",
                           **{"aria-label": "Propose admission of an outside-Universe symbol"})]
                           if operator_enabled else []),
                        html.Section([
                            html.H3("Symbol details", className="membership-inspector-title"),
                            html.Div("Search by ticker or click a symbol in any list.",
                                     id="membership-search-status", role="status",
                                     className="membership-search-status"),
                            html.Div(_membership_inspector(html, view, selected_symbol),
                                     id="membership-symbol-inspector"),
                        ], className="membership-inspector",
                            id="membership-inspector-panel",
                            **{"aria-label": "Selected symbol details"}),
                    ], id="membership-lookup-column", className="membership-lookup-column"),
                ],
                className="membership-tier-columns",
                id="membership-tier-columns",
            ),
            html.P("Historical replay remains read-only. Synthetic operator actions are only available in explicitly selected Real Time (synthetic QA) mode.", className="membership-editing-note"),
            *([_synthetic_operator_controls(html, dcc)] if operator_enabled else []),
        ],
        className="membership-explorer",
    )


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
                    dcc.Graph(
                        id="observation-overlay-chart",
                        figure=build_observation_overlay_figure(
                            overlay, off_hours=False
                        ),
                        config={
                            "displaylogo": False,
                            "responsive": True,
                            "scrollZoom": True,
                        },
                        className="overlay-chart",
                    ),
                ],
                className="overlay-workspace",
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
                "Sampling channel that supplied the newest displayed " "observation."
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
                "Schwab mark price from the newest displayed quote " "observation."
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
                    and replay.replay_time_utc == replay.first_event_time_utc
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


def _acquisition_health_panel(html: Any, status: dict[str, Any]) -> list[Any]:
    """Operational wall-clock state stays separate from replay projections."""
    if status["state"] == "UNAVAILABLE" and not status.get("channels"):
        return [
            html.P(
                [
                    html.Strong("Acquisition health · not attached"),
                    " · This experimental replay has no matching wall-clock "
                    "health report. Replay data is unaffected. To inspect "
                    "historical collection health, supply "
                    "--polling-health-directory when launching the viewer.",
                ],
                className="acquisition-health-unavailable",
            )
        ]
    current = status["state"] == "CURRENT"
    observed = status.get("observed_at_utc")
    rows = []
    for channel, info in status.get("channels", {}).items():
        committed = info.get("last_committed_slot_utc")
        rows.append(html.Tr([
            html.Td(channel.upper()), html.Td(info["state"] if current else "Last reported: " + info["state"]),
            html.Td(_time_text(datetime.fromisoformat(committed)) if committed else "None"),
            html.Td(str(info.get("missing_slot_count", 0))),
            html.Td(info.get("reason", "") + f" · Request errors: {info.get('latest_outcomes', {}).get('request_error', 0)}"),
        ]))
    detail = status["reason"]
    if status.get("journal_error"):
        detail += " Journal read failed: " + status["journal_error"]
    if status.get("process_inspection", {}).get("error"):
        detail += " Process inspection: " + status["process_inspection"]["error"]
    return [
        html.H3("Acquisition health · " + status["state"]),
        html.P("Current monitor information uses wall-clock time and is separate from replay time."),
        html.P(detail),
        html.P("Last completed health check: " + (_time_text(datetime.fromisoformat(observed)) if observed else "unavailable")),
        html.Table([html.Thead(html.Tr([html.Th(x) for x in ("Channel", "State", "Last committed slot", "Missing slots", "Details")])),
                    html.Tbody(rows)]),
    ]


def create_quote_dashboard(
    controller: QuoteDashboardReplayController,
    *,
    request_server_stop: Callable[[], None] | None = None,
    acquisition_health_reader: Callable[[], dict[str, Any]] | None = None,
    synthetic_operator_root: Path | None = None,
) -> Any:
    """Create a local interactive dashboard for one replay controller."""

    try:
        import dash_ag_grid as dag
        from dash import (
            Dash,
            ClientsideFunction,
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
            "Dash dependencies are not installed. Run: " "python -m pip install -e ."
        ) from exc

    operator_bridge = (SyntheticOverlayMembership(synthetic_operator_root)
                       if synthetic_operator_root is not None else None)
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
            *([dcc.Interval(id="acquisition-health-tick", interval=5_000)] if acquisition_health_reader else []),
            dcc.Store(
                id="rendered-event-count",
                data=replay.applied_event_count,
            ),
            dcc.Store(
                id="membership-selected-symbol",
                data=(replay.observation_overlay.symbol if replay.observation_overlay is not None else None),
            ),
            dcc.Store(
                id="theme-preference",
                data="dark",
                storage_type="local",
            ),
            dcc.Store(id="observation-overlay-reference"),
            dcc.Store(id="observation-overlay-reference-mode", data=False),
            dcc.Store(id="observation-overlay-viewport"),
            dcc.Store(id="observation-overlay-shift-hover", data=False),
            dcc.Store(
                id="activity-section",
                data="graph" if replay.observation_overlay is not None else "information",
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
                                        className=("theme-toggle server-stop-button"),
                                        title=(
                                            "Gracefully stop this local "
                                            "dashboard server"
                                        ),
                                    ),
                                    dcc.ConfirmDialog(
                                        id="server-stop-confirm",
                                        message=("Stop the local dashboard server?"),
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
                                        (
                                            view.session_date.isoformat()
                                            if view.session_date
                                            else "No session"
                                        ),
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
            html.Nav(
                [
                    _activity_button(html, "information", "ⓘ", "Information", active=replay.observation_overlay is None),
                    _activity_button(html, "graph", "▥", "Graph Setups", active=replay.observation_overlay is not None),
                    _activity_button(html, "replay", "◷", "Time Controls", active=False),
                    _activity_button(html, "backtest", "◇", "Backtest Settings", active=False),
                    _activity_button(html, "trading", "$", "Trading", active=False),
                ],
                className="activity-rail",
                **{"aria-label": "Overlay activity bar"},
            ),
            html.Aside(
                [
                    html.Section(
                        [
                            html.H2("Information"),
                            html.P("Read-only, one-session journal replay."),
                            html.Dl(
                                [
                                    html.Dt("Session"),
                                    html.Dd(str(view.session_date) if view.session_date else "Unavailable"),
                                    html.Dt("Chart symbol"),
                                    html.Dd(replay.observation_overlay.symbol if replay.observation_overlay is not None else "None loaded"),
                                    html.Dt("Evidence timing"),
                                    html.Dd("Membership at effective time · quotes at acquisition completion · candles at interval completion"),
                                ],
                                className="sidebar-information-list",
                            ),
                        ],
                        id="sidebar-information",
                        className="side-section",
                        style={"display": "none" if replay.observation_overlay is not None else "block"},
                    ),
                    html.Section(
                        [
                            html.H2("Graph Setups"),
                            _overlay_display_controls(html, dcc) if replay.observation_overlay is not None
                            else html.P("Load a session with Observation Overlay evidence to configure a chart."),
                        ],
                        id="sidebar-graph",
                        className="side-section",
                        style={"display": "block" if replay.observation_overlay is not None else "none"},
                    ),
                    html.Section(
                        [
                            html.H2("Backtest Settings"),
                            html.P("Reserved for a future, separately qualified backtesting workflow. No backtest executes here."),
                        ],
                        id="sidebar-backtest",
                        className="side-section",
                        style={"display": "none"},
                    ),
                    html.Section(
                        [
                            html.H2("Trading"),
                            html.P("Reserved for future work. This Overlay cannot place or simulate orders."),
                        ],
                        id="sidebar-trading",
                        className="side-section",
                        style={"display": "none"},
                    ),
                ],
                id="overlay-sidebar",
                className="overlay-sidebar",
                **{"aria-label": "Selected activity controls"},
            ),
            html.Section(
                [
                    html.H2("Time Controls", className="side-section-title"),
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
                                    "Seek within this journal day using " "Eastern Time"
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
                id="replay-controls-panel",
                className="replay-controls",
                style={"display": "none"},
                **{"aria-label": "Replay controls"},
            ),
            html.Div(
                [
                    html.Section(
                        [
                            html.Div(
                                [
                                    html.H2("Workspace mode", className="workspace-mode-heading"),
                                    dcc.RadioItems(
                                        id="workspace-mode",
                                        options=[
                                            {"label": "Real Time (synthetic QA)" if operator_bridge else "Real Time", "value": "real-time", "disabled": operator_bridge is None},
                                            {"label": "Historical", "value": "historical"},
                                            {"label": "Backtest", "value": "backtest", "disabled": True},
                                        ],
                                        value="historical",
                                        inline=True,
                                        className="workspace-mode-control",
                                    ),
                                ],
                                className="workspace-mode-picker",
                            ),
                            html.Div(
                                [
                                    html.Strong("Historical · read-only", className="workspace-mode-status", id="workspace-mode-status"),
                                    html.Span(
                                        "Real Time is synthetic-only if enabled; Backtest remains unavailable.",
                                        className="workspace-mode-note",
                                    ),
                                ],
                                className="workspace-mode-context",
                            ),
                        ],
                        className="workspace-context-bar",
                        **{"aria-label": "Workspace operating mode"},
                    ),
                    dcc.Tabs(
                        id="dashboard-tabs",
                        value="monitor",
                        className="dashboard-tabs",
                        children=[
                            dcc.Tab(
                                label="Membership",
                                value="membership",
                                className="dashboard-tab",
                                selected_className="dashboard-tab-selected",
                                children=[
                                    _membership_panel(
                                        html,
                                        dag,
                                        view,
                                        replay.observation_overlay.symbol if replay.observation_overlay is not None else None,
                                        operator_enabled=operator_bridge is not None,
                                        dcc=dcc,
                                    ),
                                ],
                            ),
                            *(
                                [
                                    dcc.Tab(
                                        label="Charts",
                                id="dashboard-tab-charts",
                                        value="charts",
                                        className="dashboard-tab",
                                        selected_className="dashboard-tab-selected",
                                        children=[
                                            _observation_overlay_panel(
                                                html,
                                                dcc,
                                                replay.observation_overlay,
                                            )
                                        ],
                                    )
                                ]
                                if replay.observation_overlay is not None
                                else [
                                    dcc.Tab(
                                        label="Charts",
                                id="dashboard-tab-charts",
                                        value="charts",
                                        className="dashboard-tab",
                                        selected_className="dashboard-tab-selected",
                                        children=[html.P("No Observation Overlay evidence was loaded for this session.", className="empty-state")],
                                    )
                                ]
                            ),
                            dcc.Tab(
                                label="Indicators",
                                id="dashboard-tab-indicators",
                                value="indicators",
                                className="dashboard-tab",
                                selected_className="dashboard-tab-selected",
                                children=[html.Section(
                                    [html.H2("Indicators"), html.P("Future workspace: indicators and trading signals are not computed in this read-only replay.")],
                                    className="workspace-placeholder",
                                )],
                            ),
                            dcc.Tab(
                                label="Monitor",
                                id="dashboard-tab-monitor",
                                value="monitor",
                                className="dashboard-tab",
                                selected_className="dashboard-tab-selected",
                                children=[
                                    html.Section(
                                        [
                                            html.Div(
                                                [html.Span("Unique members"), html.Strong(f"{view.unique_member_count:,}", id="metric-unique")],
                                                className="metric-card",
                                            ),
                                            html.Div(
                                                [html.Span("Multi-channel"), html.Strong(f"{view.multi_channel_count:,}", id="metric-multi")],
                                                className="metric-card",
                                            ),
                                            html.Div(
                                                [html.Span("Events applied"), html.Strong(f"{view.event_count:,}", id="metric-events")],
                                                className="metric-card",
                                            ),
                                            html.Div(
                                                [html.Span("Observations"), html.Strong(f"{view.observation_count:,}", id="metric-observations")],
                                                className="metric-card",
                                            ),
                                        ],
                                        className="metric-grid monitor-metric-grid",
                                        **{"aria-label": "Replay totals"},
                                    ),
                                    html.P(
                                        "Sampling revisions and quote outcomes use replay time; "
                                        "collection health below uses wall-clock time.",
                                        className="monitor-context-note",
                                    ),
                                    html.Section(
                                        _channel_cards(html, view),
                                        id="channel-grid",
                                        className="channel-grid monitor-channel-grid",
                                        **{"aria-label": "Sampling channels"},
                                    ),
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.Div([
                                                        html.H2("Recorded symbol observations"),
                                                        html.P("Filter or sort the latest channel results at this replay time."),
                                                    ]),
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
                                                style={"height": "min(52vh, 620px)"},
                                            ),
                                        ],
                                        className="table-panel monitor-table-panel",
                                        **{"aria-label": "Recorded symbol observations"},
                                    ),
                                    *([html.Section(
                                        _acquisition_health_panel(html, acquisition_health_reader()),
                                        id="acquisition-health-panel",
                                        className="acquisition-health-panel",
                                        **{"aria-label": "Acquisition health"},
                                    )] if acquisition_health_reader else []),
                                ],
                            ),
                            dcc.Tab(
                                label="Alerts",
                                id="dashboard-tab-alerts",
                                value="alerts",
                                className="dashboard-tab",
                                selected_className="dashboard-tab-selected",
                                children=[html.Section(
                                    [html.H2("Alerts"), html.P("No separate historical alert feed is connected. Collection health remains available under Monitor.")],
                                    className="workspace-placeholder",
                                )],
                            ),
                        ],
                    ),
                ],
                id="main-workspace",
                className="main-workspace",
            ),
        ],
        id="dashboard-page",
        className="dashboard-shell theme-dark",
    )

    if acquisition_health_reader:
        @app.callback(Output("acquisition-health-panel", "children"),
                      Input("acquisition-health-tick", "n_intervals"))
        def update_acquisition_health(_ticks: int) -> list[Any]:
            return _acquisition_health_panel(html, acquisition_health_reader())

    @app.callback(
        *[Output(f"membership-{tier}-grid", "rowData") for tier in TIERS],
        *[Output(f"membership-{tier}-count", "children") for tier in TIERS],
        Input("rendered-event-count", "data"),
        *([Input("workspace-mode", "value"),
           Input("operator-receipt-tick", "n_intervals")] if operator_bridge else []),
    )
    def update_membership_lists(_event_count: int, *mode_and_ticks: Any) -> tuple[Any, ...]:
        mode = mode_and_ticks[0] if operator_bridge is not None else "historical"
        if mode == "real-time" and operator_bridge is not None:
            records = operator_bridge.records()
        else:
            replay_view = build_quote_dashboard_view(controller.snapshot().state)
            records = membership_tier_records(replay_view)
        return (
            *[records[tier] for tier in TIERS],
            *[f"{len(records[tier]):,}" for tier in TIERS],
        )

    @app.callback(
        Output("membership-selected-symbol", "data"),
        *[Output(f"membership-{tier}-grid", "selectedRows") for tier in TIERS],
        Output("membership-search-status", "children"),
        *[Output(f"membership-{tier}-grid", "scrollTo") for tier in TIERS],
        Output("membership-search-symbol", "value"),
        *([Output("membership-admission-candidate", "data")] if operator_bridge else []),
        *[Input(f"membership-{tier}-grid", "cellClicked") for tier in TIERS],
        Input("membership-find-button", "n_clicks"),
        Input("membership-search-symbol", "n_submit"),
        Input("membership-search-symbol", "value"),
        *([Input("workspace-mode", "value")] if operator_bridge else []),
        State("workspace-mode", "value"),
        State("membership-selected-symbol", "data"),
        prevent_initial_call=True,
    )
    def select_membership_symbol(*args: Any) -> tuple[Any, ...]:
        """Keep selection, search, and any outside-Uni admission offer in sync.

        An admission candidate is offered only for a canonical ticker absent
        from the effective synthetic roster. This is NOT security identity
        verification; that occurs inside the membership writer.
        """
        trigger = ctx.triggered_id
        changed = ctx.triggered_prop_ids
        query = args[len(TIERS) + 2]
        mode, selected_symbol = args[-2:]
        no_scroll = (no_update,) * len(TIERS)
        candidate = (lambda value: (value,) if operator_bridge is not None else ())
        no_change = (no_update,) * (len(TIERS) * 2 + 3
                                   + (1 if operator_bridge is not None else 0))

        if operator_bridge is not None and trigger == "workspace-mode":
            return (None, [], [], [], "Select a symbol or search all three lists.",
                    *no_scroll, "", *candidate(None))

        # A user edit removes any prior admission offer, even when the old
        # symbol is absent from membership. Reflected clicks are ignored.
        submitted = "membership-search-symbol.n_submit" in changed
        edited = "membership-search-symbol.value" in changed
        if edited and not submitted and trigger == "membership-search-symbol":
            if query == selected_symbol:
                return no_change
            return (None, [], [], [],
                    "Search text changed. Press Find or Enter for updated details.",
                    *no_scroll, no_update, *candidate(None))

        if trigger == "membership-find-button" or submitted:
            raw = str(query or "").strip().upper()
            if not raw:
                return (None, [], [], [], "Enter a ticker symbol to find.",
                        *no_scroll, no_update, *candidate(None))
            try:
                symbol = normalized(raw)
            except ValueError:
                return (None, [], [], [],
                        "Invalid canonical ticker format. Admission is not offered.",
                        *no_scroll, no_update, *candidate(None))

            records = (
                operator_bridge.records()
                if operator_bridge is not None and mode == "real-time"
                else membership_tier_records(
                    build_quote_dashboard_view(controller.snapshot().state)
                )
            )
            symbol, tier = _membership_search_tier(records, symbol)
            if tier is None:
                offer = operator_bridge is not None and mode == "real-time"
                detail = (
                    " Preview admission to Uni is available; identity remains unverified."
                    if offer else ""
                )
                return (None, [], [], [],
                        f"{symbol} is not in Uni, Focus, or Hot at the current "
                        f"{'synthetic session' if mode == 'real-time' else 'replay'} time."
                        + detail, *no_scroll, no_update,
                        *candidate(symbol if offer else None))
            heading = {"uni": "Uni only", "focus": "Focus only", "hot": "Hot"}[tier]
            scroll = tuple(
                {"rowId": symbol, "rowPosition": "middle"}
                if current == tier else no_update for current in TIERS
            )
            return (symbol,
                    *({"ids": [symbol]} if t == tier else [] for t in TIERS),
                    f"{symbol} found in {heading}.", *scroll, symbol,
                    *candidate(None))

        for tier, click in zip(TIERS, args[:len(TIERS)]):
            if trigger == f"membership-{tier}-grid" and isinstance(click, dict):
                symbol = click.get("rowId")
                if isinstance(symbol, str) and symbol:
                    heading = {"uni": "Uni only", "focus": "Focus only", "hot": "Hot"}[tier]
                    return (symbol,
                            *({"ids": [symbol]} if t == tier else [] for t in TIERS),
                            f"{symbol} selected in {heading}.", *no_scroll, symbol,
                            *candidate(None))
        return no_change


    @app.callback(
        Output("membership-symbol-inspector", "children"),
        Input("membership-selected-symbol", "data"),
        Input("rendered-event-count", "data"),
        *([Input("workspace-mode", "value"),
           Input("operator-receipt-tick", "n_intervals")] if operator_bridge else []),
    )
    def update_membership_inspector(symbol: str | None, _event_count: int,
                                     *mode_and_ticks: Any) -> Any:
        mode = mode_and_ticks[0] if operator_bridge is not None else "historical"
        if mode == "real-time" and operator_bridge is not None:
            try:
                return _synthetic_membership_inspector(html, operator_bridge.snapshot(symbol))
            except (OSError, ValueError) as error:
                return html.P(f"Synthetic membership unavailable: {error}")
        replay_view = build_quote_dashboard_view(controller.snapshot().state)
        return _membership_inspector(html, replay_view, symbol)


    if operator_bridge is not None:
        @app.callback(
            Output("membership-outside-panel", "style"),
            Output("membership-admission-label", "children"),
            Output("membership-admit-button", "disabled"),
            Input("membership-admission-candidate", "data"),
            Input("workspace-mode", "value"),
            Input("operator-receipt-tick", "n_intervals"),
        )
        def show_outside_admission(candidate: str | None, mode: str,
                                   _receipt_tick: int) -> tuple:
            """Only offer admission for an effective outside member in QA."""
            if mode != "real-time" or not isinstance(candidate, str) or not candidate:
                return {"display": "none"}, "", True
            try:
                snapshot = operator_bridge.snapshot(candidate)
            except (OSError, ValueError, KeyError, TypeError):
                return {"display": "none"}, "", True
            if snapshot["selected"]["tier"] != "outside":
                # Once an effective revision admits the symbol, hide the offer.
                return {"display": "none"}, "", True
            ready = bool(snapshot["eligible"]) and not snapshot["pending_revision"]
            status = "Available for preview" if ready else "Worker not currently available"
            return {}, f"{candidate} — outside all tiers · {status}", not ready

    if operator_bridge is not None:
        # Native AG Grid external dropzones, enabled only by this disposable
        # synthetic session. The binder only reports a proposed drag event
        # into a Dash Store; it never changes rowData or writes membership.
        app.clientside_callback(
            ClientsideFunction(namespace="mbMembershipDrag", function_name="bind"),
            Output("membership-drag-binding-status", "data"),
            Input("workspace-mode", "value"),
            *[Input(f"membership-{tier}-grid", "id") for tier in TIERS],
        )

        @app.callback(
            Output("membership-drag-status", "children"),
            Input("membership-drag-binding-status", "data"),
            Input("workspace-mode", "value"),
        )
        def display_drag_status(binding: dict | None, mode: str) -> str:
            if mode != "real-time":
                return "Historical replay is read-only; drag handles are disabled."
            if binding and binding.get("ready") is True:
                return ("Drag a symbol by its grip onto another tier. "
                        "Drop creates a PREVIEW only; confirm below.")
            if binding and binding.get("phase") == "drag-unavailable":
                return ("Drag setup unavailable; use manual Preview instead. "
                        + str(binding.get("reason") or ""))
            return "Preparing synthetic drag handles; manual Preview remains available."

    if operator_bridge is not None:
        @app.callback(
            Output("dashboard-tabs", "value"),
            Output("dashboard-tab-charts", "disabled"),
            Output("dashboard-tab-indicators", "disabled"),
            Output("dashboard-tab-monitor", "disabled"),
            Output("dashboard-tab-alerts", "disabled"),
            Input("workspace-mode", "value"),
        )
        def isolate_synthetic_workspace(mode: str) -> tuple[Any, ...]:
            if mode == "real-time":
                return "membership", True, True, True, True
            return no_update, False, False, False, False

    if operator_bridge is not None:
        @app.callback(
            Output("workspace-mode-status", "children"),
            Output("membership-context-status", "children"),
            Output("operator-bridge-panel", "style"),
            Output("operator-confirm-button", "disabled"),
            Input("workspace-mode", "value"),
            Input("operator-preview-proposal", "data"),
        )
        def operator_mode_status(mode: str, proposal: dict | None) -> tuple:
            if mode != "real-time":
                return ("Historical · read-only", "HISTORICAL · READ-ONLY",
                        {"display": "none"}, True)
            return ("REAL TIME · OFFLINE SYNTHETIC QA (NO SCHWAB)",
                    "SYNTHETIC QA · MANUAL CONTROLS",
                    {"display": "block"},
                    not bool(proposal and proposal.get("proposal_id")))

        @app.callback(
            Output("operator-symbol", "value"),
            Output("operator-action", "value"),
            Output("operator-target", "value"),
            Input("membership-selected-symbol", "data"),
            Input("membership-drag-request", "data"),
            Input("membership-admit-button", "n_clicks"),
            State("workspace-mode", "value"),
            State("membership-admission-candidate", "data"),
            State("membership-search-symbol", "value"),
            prevent_initial_call=True,
        )
        def reflect_operator_selection(symbol: str | None, drag: dict | None,
                                       _admit_clicks: int, mode: str,
                                       candidate: str | None, search: str | None) -> tuple:
            if mode != "real-time":
                return (no_update, no_update, no_update)
            if ctx.triggered_id == "membership-admit-button" and candidate:
                try:
                    if normalized(search or "") == candidate:
                        return candidate, "promote", "uni"
                except ValueError:
                    pass
                return (no_update,) * 3
            if ctx.triggered_id == "membership-drag-request" and isinstance(drag, dict):
                source = drag.get("source_tier")
                target = drag.get("destination_tier")
                if source in TIERS and target in TIERS and source != target:
                    action = ("promote" if TIERS.index(target) > TIERS.index(source)
                              else "demote")
                    return (drag.get("symbol"), action, target)
                return (no_update, no_update, no_update)
            return (symbol, no_update, no_update) if symbol else (no_update,) * 3

        @app.callback(
            Output("operator-preview-proposal", "data"),
            Output("operator-preview-status", "children"),
            Input("operator-preview-button", "n_clicks"),
            Input("membership-drag-request", "data"),
            Input("membership-admit-button", "n_clicks"),
            Input("membership-search-symbol", "value"),
            Input("workspace-mode", "value"),
            State("operator-symbol", "value"),
            State("operator-action", "value"),
            State("operator-target", "value"),
            State("membership-admission-candidate", "data"),
            prevent_initial_call=True,
        )
        def operator_preview(_clicks: int, drag: dict | None,
                             _admit_clicks: int, search: str | None, mode: str,
                             symbol: str | None, action: str, target: str,
                             candidate: str | None) -> tuple:
            source = ctx.triggered_id
            if source == "membership-search-symbol":
                return None, "Search changed; any previous action preview was cleared."
            try:
                if source == "membership-drag-request":
                    if not isinstance(drag, dict):
                        return None, "Drop ignored: invalid drag event."
                    proposal = operator_bridge.preview_drop(
                        mode, drag.get("symbol", ""),
                        drag.get("source_tier", ""),
                        drag.get("destination_tier", ""),
                    )
                    reason = "DRAG PREVIEW ONLY: "
                elif source == "membership-admit-button":
                    if not candidate or normalized(search or "") != candidate:
                        return None, "Admission preview blocked: search again for the symbol."
                    proposal = operator_bridge.preview_admission(mode, candidate)
                    reason = "ADMISSION PREVIEW ONLY: "
                elif source == "operator-preview-button":
                    proposal = operator_bridge.preview(mode, symbol or "", action, target)
                    reason = "PREVIEW ONLY: "
                else:
                    return None, ("Historical replay is read-only." if mode != "real-time"
                                  else "Select a symbol, then drag or Preview.")
            except (OperatorProblem, OSError, ValueError, TypeError) as error:
                return None, f"No request queued; preview blocked: {error}"
            return proposal, (
                f"{reason}{proposal['symbol']} {proposal['before'].upper()} → "
                f"{proposal['after'].upper()} at published revision "
                f"r{proposal['based_on_revision']}. "
                "Nothing changed. Confirm explicitly below to queue the request."
            )

        @app.callback(
            Output("operator-command-id", "data"),
            Output("operator-confirm-status", "children"),
            Input("operator-confirm-button", "n_clicks"),
            State("workspace-mode", "value"),
            State("operator-preview-proposal", "data"),
            prevent_initial_call=True,
        )
        def operator_confirm(_clicks: int, mode: str,
                             proposal: dict | None) -> tuple[Any, str]:
            if not proposal or not proposal.get("proposal_id"):
                return None, "No current preview. Preview again."
            try:
                result = operator_bridge.confirm(mode, proposal["proposal_id"])
            except (OperatorProblem, OSError, ValueError) as error:
                return None, f"No request queued: {error}"
            command = result["request"]
            return command["id"], f"Request queued: {command['id']}. Waiting for worker receipt."

        @app.callback(
            Output("operator-receipt-status", "children"),
            Input("operator-receipt-tick", "n_intervals"),
            Input("operator-command-id", "data"),
            State("workspace-mode", "value"),
        )
        def operator_receipt(_ticks: int, command_id: str | None,
                             mode: str) -> str:
            if mode != "real-time" or not command_id:
                return "No synthetic operator request in progress."
            try:
                receipt = operator_bridge.result(command_id)
            except (OSError, ValueError) as error:
                return f"Receipt unavailable: {error}"
            return (f"Worker receipt: {receipt.get('status', 'UNKNOWN')} · "
                    f"Command {command_id}")

    @app.callback(
        Output("activity-section", "data"),
        *[Input(f"activity-{section}", "n_clicks") for section in ACTIVITY_SECTIONS],
        prevent_initial_call=True,
    )
    def select_activity(*_clicks: int | None) -> str | Any:
        selected = str(ctx.triggered_id or "").removeprefix("activity-")
        return selected if selected in ACTIVITY_SECTIONS else no_update

    @app.callback(
        Output("overlay-sidebar", "style"),
        Output("replay-controls-panel", "style"),
        *[Output(f"sidebar-{section}", "style") for section in ("information", "graph", "backtest", "trading")],
        *[Output(f"activity-{section}", "className") for section in ACTIVITY_SECTIONS],
        Input("activity-section", "data"),
    )
    def display_activity(selected: str | None) -> tuple[Any, ...]:
        selected = selected if selected in ACTIVITY_SECTIONS else "information"
        visible = {"display": "block"}
        hidden = {"display": "none"}
        return (
            hidden if selected == "replay" else visible,
            {"display": "grid"} if selected == "replay" else hidden,
            *[visible if selected == section else hidden for section in ("information", "graph", "backtest", "trading")],
            *[
                "activity-button" + (" activity-button-selected" if selected == section else "")
                for section in ACTIVITY_SECTIONS
            ],
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
            Output("observation-overlay-reference", "data"),
            Output("observation-overlay-reference-mode", "data"),
            Output("observation-overlay-set-reference", "children"),
            Input("observation-overlay-set-reference", "n_clicks"),
            Input("observation-overlay-reset-reference", "n_clicks"),
            Input("observation-overlay-chart", "clickData"),
            State("observation-overlay-reference-mode", "data"),
            State("observation-overlay-chart", "figure"),
            State("observation-overlay-candle-field", "value"),
            prevent_initial_call=True,
        )
        def select_overlay_reference(
            _set_clicks: int | None,
            _reset_clicks: int | None,
            click_data: Mapping[str, Any] | None,
            choosing: bool,
            figure_data: Mapping[str, Any] | None,
            candle_field: str,
        ) -> tuple[Any, bool, str]:
            trigger = ctx.triggered_id
            if trigger == "observation-overlay-reset-reference":
                return None, False, "Set 0% reference"
            if trigger == "observation-overlay-set-reference":
                return no_update, True, "Click a candle or quote…"
            if trigger != "observation-overlay-chart" or not choosing:
                return no_update, bool(choosing), no_update
            overlay = controller.snapshot().observation_overlay
            if overlay is None:
                return no_update, True, "Click a candle or quote…"
            selected = selected_reference_from_click(
                overlay, click_data, figure_data, candle_field
            )
            if selected is None:
                return no_update, True, "Choose a candle or quote price"
            return selected, False, "Set 0% reference"

        @app.callback(
            Output("observation-overlay-band", "children"),
            Output("observation-overlay-band", "style"),
            Output("observation-overlay-as-of", "children"),
            Output("observation-overlay-evidence", "children"),
            Output("observation-overlay-chart", "figure"),
            Output("observation-overlay-chart", "className"),
            Output("observation-overlay-reference-label", "children"),
            Output("observation-overlay-viewport", "data"),
            Input("replay-clock", "children"),
            Input("theme-preference", "modified_timestamp"),
            Input("observation-overlay-uni-size", "value"),
            Input("observation-overlay-uni-opacity", "value"),
            Input("observation-overlay-focus-size", "value"),
            Input("observation-overlay-focus-opacity", "value"),
            Input("observation-overlay-hot-size", "value"),
            Input("observation-overlay-hot-opacity", "value"),
            Input("observation-overlay-detailed-hover", "value"),
            Input("observation-overlay-shift-hover", "data"),
            Input("observation-overlay-units", "value"),
            Input("observation-overlay-reference", "data"),
            Input("observation-overlay-chart", "relayoutData"),
            State("theme-preference", "data"),
            State("observation-overlay-viewport", "data"),
        )
        def update_observation_overlay(
            _replay_clock: str,
            _theme_modified: int | None,
            uni_size: str | None,
            uni_opacity: float | None,
            focus_size: str | None,
            focus_opacity: float | None,
            hot_size: str | None,
            hot_opacity: float | None,
            detailed_hover_value: list[str] | None,
            shift_hover: bool | None,
            units: str | None,
            selected_reference: Mapping[str, Any] | None,
            relayout_data: Mapping[str, Any] | None,
            stored_theme: str | None,
            stored_viewport: Mapping[str, Any] | None,
        ) -> tuple[Any, ...]:
            overlay = controller.snapshot().observation_overlay
            if overlay is None:
                return (no_update,) * 8
            overlay_view = build_observation_overlay_view(overlay)
            theme = "light" if stored_theme == "light" else "dark"
            triggered = ctx.triggered_prop_ids
            viewport = resolve_overlay_time_viewport(
                overlay,
                stored_viewport,
                relayout_data
                if "observation-overlay-chart.relayoutData" in triggered
                else None,
                off_hours=False,
            )
            figure = build_observation_overlay_figure(
                overlay,
                theme=theme,
                point_styles={
                    "uni": _overlay_point_style(uni_size, uni_opacity),
                    "focus": _overlay_point_style(focus_size, focus_opacity),
                    "hot": _overlay_point_style(hot_size, hot_opacity),
                },
                off_hours=False,
                units=units if units in {"price", "percent"} else "price",
                selected_reference=selected_reference,
                detailed_hover=(
                    isinstance(detailed_hover_value, list)
                    and "show" in detailed_hover_value
                ),
                shift_hover=shift_hover is True,
                relayout_data={
                    key: value
                    for key, value in (relayout_data or {}).items()
                    if key.startswith("yaxis")
                },
                viewport=viewport,
            )
            reference = resolve_reference(overlay, selected_reference)
            reference_label = (
                f"0%: ${reference[0]:g} · {reference[1]}"
                if reference is not None
                else "0% reference waits for the first completed regular candle"
            )
            return (
                overlay_view.current_band_label,
                {
                    "borderColor": overlay_view.current_band_color,
                    "color": overlay_view.current_band_color,
                },
                overlay_view.replay_time_et_text,
                overlay_view.evidence_text,
                figure,
                "overlay-chart",
                reference_label,
                viewport,
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
        state_changed = updated.applied_event_count != rendered_event_count
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
