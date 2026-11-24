"""Interactive Dash application for a projected quote-event replay."""

from __future__ import annotations

import re
import secrets
import time as perf_timer
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


def _activity_button(
    html: Any, section: str, glyph: str, title: str, *,
    active: bool, disabled: bool = False,
) -> Any:
    return html.Button(
        [
            html.Span(glyph, className="activity-glyph", **{"aria-hidden": "true"}),
            html.Span(title, className="activity-accessible-label"),
        ],
        id=f"activity-{section}",
        className="activity-button" + (" activity-button-selected" if active else ""),
        title=title,
        type="button",
        **({"disabled": True} if disabled else {}),
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


def _overlay_display_controls(html: Any, dcc: Any, selected_symbol: str) -> Any:
    """Reusable chart controls, mounted once in the workspace side panel."""

    return html.Div(
        [
            html.Fieldset(
                [
                    html.Legend("Chart symbol"),
                    html.Div(
                        [
                            dcc.Input(
                                id="observation-overlay-symbol",
                                type="text",
                                value=selected_symbol,
                                maxLength=20,
                                autoComplete="off",
                                n_submit=0,
                                className="membership-search-input",
                            ),
                            html.Button(
                                "Apply",
                                id="observation-overlay-symbol-apply",
                                n_clicks=0,
                                className="membership-find-button",
                            ),
                        ],
                        className="membership-search-row",
                    ),
                    html.Small("Choose a listed member. Charts and Membership share one symbol."),
                    html.Div(
                        "Select a listed member or enter a ticker in either search field.",
                        id="observation-overlay-search-status",
                        role="status",
                        className="membership-search-status",
                    ),
                ],
                className="overlay-session-control",
            ),
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


def _membership_inspector(
    html: Any, view: QuoteDashboardView, symbol: str | None, *,
    live_mode: bool = False, operator_enabled: bool = False,
) -> Any:
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
                (
                    (
                        "Real Time · committed observations may be delayed, incomplete "
                        "or stale. Membership requests require explicit Preview and "
                        "Confirm; the existing worker alone publishes revisions."
                        if operator_enabled else
                        "Real Time · read-only committed observations. The latest quote "
                        "may be delayed, incomplete, or stale. No manual membership "
                        "actions are enabled."
                    )
                    if live_mode else
                    "Historical · read-only. This information is from saved, "
                    "replay-causal observations; it is not a live price feed. "
                    "A rest-of-day suppression policy is not implied by an ordinary demotion."
                ),
                className="membership-details-note",
            ),
        ]
    )


def _membership_tier_grid(html: Any, dag: Any, tier: str, records: list[dict[str, Any]]) -> Any:
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
                    {"field": "symbol", "headerName": "Symbol", "width": 115, "pinned": "left", "cellClass": "symbol-cell"},
                    {"field": "last_price", "headerName": "Last", "type": "numericColumn", "width": 95},
                    {"field": "description", "headerName": "Name", "minWidth": 130, "flex": 1},
                ],
                defaultColDef={"sortable": True, "filter": True, "floatingFilter": True, "resizable": True},
                dashGridOptions={
                    "rowSelection": "single",
                    # The one selected row across all three grids is owned by
                    # the membership click callback, not by each grid alone.
                    "suppressRowClickSelection": True,
                    "animateRows": False,
                    "getRowId": {"function": "params.data.symbol"},
                    "tooltipShowDelay": 350,
                },
                selectedRows=[],
                persistence=False,
                style={"height": "min(46vh, 565px)"},
            ),
        ],
        className=f"membership-tier membership-tier-{tier}",
    )


def _membership_search_tier(
    records: Mapping[str, list[dict[str, Any]]], query: str | None
) -> tuple[str, str | None]:
    """Resolve one exact ticker against exclusive effective-time tier lists."""
    symbol = (query or "").strip().upper()
    if not symbol or len(symbol) > 20:
        return symbol, None
    for tier in TIERS:
        if any(row.get("symbol") == symbol for row in records.get(tier, ())):
            return symbol, tier
    return symbol, None


def _membership_operator_controls(
    html: Any, dcc: Any, selected_symbol: str | None,
) -> Any:
    """Explicitly opt-in, preview-first controls; no direct journal edits."""
    return html.Section(
        [
            html.H3("Manual Membership · controlled", className="membership-inspector-title"),
            html.P(
                "This viewer queues requests only after Preview and Confirm. "
                "The existing authenticated worker decides whether to publish.",
                className="membership-details-note",
            ),
            html.Label(
                [
                    "Symbol",
                    dcc.Input(
                        id="operator-symbol",
                        type="text",
                        value=selected_symbol or "",
                        maxLength=20,
                        placeholder="Ticker, including outside Uni",
                        autoComplete="off",
                        className="membership-search-input",
                    ),
                ],
                className="operator-field",
            ),
            html.Div(
                [
                    html.Label(
                        [
                            "Action",
                            dcc.Dropdown(
                                id="operator-action",
                                options=[
                                    {"label": "Promote", "value": "promote"},
                                    {"label": "Ordinary demote", "value": "demote"},
                                ],
                                value="promote",
                                clearable=False,
                            ),
                        ],
                        className="operator-field",
                    ),
                    html.Label(
                        [
                            "Target",
                            dcc.Dropdown(
                                id="operator-target",
                                options=[
                                    {"label": "Uni", "value": "uni"},
                                    {"label": "Focus", "value": "focus"},
                                    {"label": "Hot", "value": "hot"},
                                ],
                                value="focus",
                                clearable=False,
                            ),
                        ],
                        className="operator-field",
                    ),
                ],
                className="operator-action-inputs",
            ),
            html.Button(
                "Preview action",
                id="operator-preview-button",
                n_clicks=0,
                className="membership-find-button",
            ),
            html.Div(
                "No action previewed.",
                id="operator-preview-status",
                className="membership-search-status",
                role="status",
            ),
            html.Button(
                "Confirm and queue request",
                id="operator-confirm-button",
                n_clicks=0,
                disabled=True,
                className="control-button",
            ),
            html.Div(
                "No request queued.",
                id="operator-submit-status",
                className="membership-search-status",
                role="status",
            ),
            html.Div(
                "No worker receipt.",
                id="operator-receipt-status",
                className="membership-search-status",
                role="status",
            ),
            html.P(
                "Previews expire after 90 seconds and are rechecked against the "
                "current revision and worker heartbeat. Ordinary demotion does "
                "not suppress later distinct automatic halt promotions.",
                className="membership-details-note",
            ),
        ],
        id="operator-controls-panel",
        className="membership-inspector operator-controls-panel",
    )


def _membership_panel(
    html: Any, dag: Any, dcc: Any, view: QuoteDashboardView,
    selected_symbol: str | None, *, live_mode: bool = False,
    operator_enabled: bool = False,
) -> Any:
    """Shared Membership layout; operator controls are Real Time opt-in only."""
    by_tier = membership_tier_records(view)
    return html.Section(
        [
            html.Div(
                [
                    html.Div([
                        html.H2("Membership explorer"),
                        html.P(
                            "Three exclusive tier lists and one shared Find. "
                            "Select a ticker to inspect it in Membership and Charts."
                        ),
                    ]),
                    html.Span(
                        (
                            "REAL TIME · CONTROLLED" if operator_enabled
                            else "REAL TIME · READ-ONLY" if live_mode
                            else "HISTORICAL · READ-ONLY"
                        ),
                        className="membership-readonly",
                    ),
                ],
                className="membership-heading",
            ),
            html.P(
                "Each symbol appears in its highest tier: Uni only, Focus (not Hot), "
                "or Hot. Underlying membership remains nested: Hot ⊆ Focus ⊆ Uni.",
                className="membership-explanation",
            ),
            html.Div(
                [
                    *[_membership_tier_grid(html, dag, tier, by_tier[tier]) for tier in TIERS],
                    html.Div(
                        [
                            html.Section(
                                [
                                    html.H3("Find symbol", className="membership-search-title"),
                                    html.Div(
                                        [
                                            dcc.Input(
                                                id="membership-search-symbol",
                                                type="text",
                                                placeholder="Ticker",
                                                maxLength=20,
                                                autoComplete="off",
                                                n_submit=0,
                                                value=selected_symbol or "",
                                                className="membership-search-input",
                                            ),
                                            html.Button(
                                                "Find",
                                                id="membership-find-button",
                                                n_clicks=0,
                                                className="membership-find-button",
                                            ),
                                        ],
                                        className="membership-search-row",
                                    ),
                                ],
                                id="membership-search-panel",
                                className="membership-search-panel",
                            ),
                            html.Section(
                                [
                                    html.H3("Symbol details", className="membership-inspector-title"),
                                    html.Div(
                                        "Click a listed member or search by ticker.",
                                        id="membership-search-status",
                                        role="status",
                                        className="membership-search-status",
                                    ),
                                    html.Div(
                                        _membership_inspector(
                                            html, view, selected_symbol, live_mode=live_mode,
                                            operator_enabled=operator_enabled,
                                        ),
                                        id="membership-symbol-inspector",
                                    ),
                                ],
                                id="membership-inspector-panel",
                                className="membership-inspector",
                                **{"aria-label": "Selected symbol information"},
                            ),
                            *(
                                [_membership_operator_controls(html, dcc, selected_symbol)]
                                if operator_enabled else []
                            ),
                        ],
                        id="membership-lookup-column",
                        className="membership-lookup-column",
                    ),
                ],
                className="membership-tier-columns",
            ),
            html.P(
                (
                    (
                        "Controlled operator requests are possible only through Preview "
                        "and Confirm, never by editing the displayed roster."
                        if operator_enabled else
                        "Real Time is observing committed journal evidence; manual "
                        "Membership actions remain disabled in this experimental viewer."
                    )
                    if live_mode else
                    "Historical membership is read-only; no drag, admission, "
                    "Preview, Confirm, or membership write actions are available."
                ),
                className="membership-editing-note",
            ),
        ],
        className="membership-explorer",
    )


def _overlay_description(
    overlay: ObservationOverlayData, *, live_mode: bool = False
) -> str:
    if live_mode and overlay.cache is None:
        return "Read-only committed quote and Membership evidence; no matching five-minute OHLCV cache."
    if overlay.candles:
        return "Completed five-minute OHLCV with replay-causal quote and membership evidence."
    if overlay.cache is not None:
        return "No completed five-minute candles at this replay time; quote and membership evidence only."
    return "Quote-only historical view: no matching OHLCV cache for this symbol and date."


def _observation_overlay_panel(
    html: Any,
    dcc: Any,
    overlay: ObservationOverlayData, *, live_mode: bool = False,
) -> Any:
    view = build_observation_overlay_view(overlay)
    return html.Section(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H2(
                                f"Observation Overlay · {view.symbol}",
                                id="observation-overlay-title",
                            ),
                            html.P(
                                _overlay_description(overlay, live_mode=live_mode),
                                id="observation-overlay-description",
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
    live_refresh: Callable[[], tuple[int, str]] | None = None,
    live_mode: bool = False,
    operator_actions: Any | None = None,
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
            "Dash dependencies are not installed. Run: " "python -m pip install -e ."
        ) from exc

    if live_mode and live_refresh is None:
        raise ValueError("Real Time dashboard requires a read-only committed event source")
    if operator_actions is not None and not live_mode:
        raise ValueError("Manual operator actions are not permitted in Historical or Backtest")
    operator_enabled = operator_actions is not None
    operator_token = secrets.token_urlsafe(32) if operator_enabled else None
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
            *([dcc.Interval(id="live-tail-tick", interval=5_000)] if live_mode else []),
            *([dcc.Store(id="live-refresh-count", data=replay.applied_event_count)] if live_mode else []),
            *(
                [
                    dcc.Store(id="operator-csrf", data=operator_token),
                    dcc.Store(id="operator-proposal"),
                    dcc.Store(id="operator-command"),
                    dcc.Interval(id="operator-receipt-tick", interval=3_000),
                ] if operator_enabled else []
            ),
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
                                        (
                                            "Real Time · committed journal"
                                            if live_mode else "Interactive exact-day replay"
                                        ),
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
                    _activity_button(html, "replay", "◷", "Time Controls", active=False, disabled=live_mode),
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
                                    html.Dd(replay.observation_overlay.symbol if replay.observation_overlay is not None else "None loaded", id="chart-symbol-current"),
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
                            _overlay_display_controls(html, dcc, replay.observation_overlay.symbol) if replay.observation_overlay is not None
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
                                            {"label": "Real Time", "value": "real-time", "disabled": not live_mode},
                                            {"label": "Historical", "value": "historical", **({"disabled": True} if live_mode else {})},
                                            {"label": "Backtest", "value": "backtest", "disabled": True},
                                        ],
                                        value="real-time" if live_mode else "historical",
                                        inline=True,
                                        className="workspace-mode-control",
                                    ),
                                ],
                                className="workspace-mode-picker",
                            ),
                            html.Div(
                                [
                                    html.Strong(
                                        (
                                            "Real Time · controlled Membership"
                                            if operator_enabled else "Real Time · committed read-only"
                                        )
                                        if live_mode else "Historical · read-only",
                                        className="workspace-mode-status",
                                    ),
                                    html.Span(
                                        (
                                            (
                                                "Commit-only quote stream; operator requests require "
                                                "explicit Preview and Confirm and existing worker authority. "
                                                "Saved phase does not verify liveness."
                                                if operator_enabled else
                                                "Live process not verified. Saved controller phase "
                                                "does not prove liveness. Historical mode uses a "
                                                "separately launched viewer; operator actions disabled."
                                            )
                                            if live_mode else
                                            "Real Time requires an explicit validated daily attachment; "
                                            "Backtest remains disabled."
                                        ),
                                        className="workspace-mode-note",
                                    ),
                                    *(
                                        [
                                            html.Div(
                                                "Checking committed journal progress…",
                                                id="live-data-status",
                                                className="workspace-mode-note",
                                            )
                                        ] if live_mode else []
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
                                        dcc,
                                        view,
                                        replay.observation_overlay.symbol if replay.observation_overlay is not None else None,
                                        live_mode=live_mode,
                                        operator_enabled=operator_enabled,
                                    ),
                                ],
                            ),
                            *(
                                [
                                    dcc.Tab(
                                        label="Charts",
                                        value="charts",
                                        className="dashboard-tab",
                                        selected_className="dashboard-tab-selected",
                                        children=[
                                            _observation_overlay_panel(
                                                html,
                                                dcc,
                                                replay.observation_overlay,
                                                live_mode=live_mode,
                                            )
                                        ],
                                    )
                                ]
                                if replay.observation_overlay is not None
                                else [
                                    dcc.Tab(
                                        label="Charts",
                                        value="charts",
                                        className="dashboard-tab",
                                        selected_className="dashboard-tab-selected",
                                        children=[html.P("No Observation Overlay evidence was loaded for this session.", className="empty-state")],
                                    )
                                ]
                            ),
                            dcc.Tab(
                                label="Indicators",
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

    if live_mode:
        @app.callback(
            Output("live-refresh-count", "data"),
            Output("live-data-status", "children"),
            Input("live-tail-tick", "n_intervals"),
        )
        def refresh_committed_live(_tick: int) -> tuple[Any, str]:
            """Observe committed additions; never request quotes or write journals."""
            assert live_refresh is not None
            try:
                new_events, detail = live_refresh()
            except Exception as error:
                return no_update, (
                    f"READ-ONLY ATTACHMENT ERROR: {type(error).__name__}: {error}. "
                    "Displayed evidence is not current; no collector was changed."
                )
            if new_events:
                return controller.snapshot().applied_event_count, detail
            return no_update, detail

    @app.callback(
        *[Output(f"membership-{tier}-grid", "rowData") for tier in TIERS],
        *[Output(f"membership-{tier}-count", "children") for tier in TIERS],
        Input("rendered-event-count", "data"),
    )
    def update_membership_lists(_event_count: int) -> tuple[Any, ...]:
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
        Output("chart-symbol-current", "children"),
        *(
            [
                Output("observation-overlay-symbol", "value"),
                Output("observation-overlay-search-status", "children"),
            ]
            if replay.observation_overlay is not None else []
        ),
        *[Input(f"membership-{tier}-grid", "cellClicked") for tier in TIERS],
        Input("membership-find-button", "n_clicks"),
        Input("membership-search-symbol", "n_submit"),
        *(
            [
                Input("observation-overlay-symbol-apply", "n_clicks"),
                Input("observation-overlay-symbol", "n_submit"),
            ]
            if replay.observation_overlay is not None else []
        ),
        State("membership-search-symbol", "value"),
        *(
            [State("observation-overlay-symbol", "value")]
            if replay.observation_overlay is not None else []
        ),
        prevent_initial_call=True,
    )
    def select_membership_symbol(*args: Any) -> tuple[Any, ...]:
        """Synchronize submitted ticker and result across read-only workspaces.

        An unsuccessful search clears selection/inspector, not the chart data.
        Neither outcome publishes or alters membership.
        """
        has_chart = replay.observation_overlay is not None
        trigger = ctx.triggered_id
        count = len(TIERS)
        clicks = args[:count]
        search_text = args[count + (4 if has_chart else 2)]
        graph_text = args[count + 5] if has_chart else None
        no_scroll = (no_update,) * count
        output_count = count * 2 + 4 + (2 if has_chart else 0)

        if trigger in {"membership-find-button", "membership-search-symbol"}:
            raw = search_text
            from_grid = False
        elif has_chart and trigger in {
            "observation-overlay-symbol-apply", "observation-overlay-symbol"
        }:
            raw = graph_text
            from_grid = False
        else:
            raw = None
            from_grid = False
            for tier, click in zip(TIERS, clicks):
                if trigger == f"membership-{tier}-grid" and isinstance(click, dict):
                    raw = click.get("rowId")
                    from_grid = True
                    break
            if raw is None:
                return (no_update,) * output_count

        attempted = str(raw or "").strip().upper()

        def rejected(message: str) -> tuple[Any, ...]:
            # Clearing the selection also clears the inspector. The previous
            # chart is intentionally left intact until a valid ticker is found.
            chart_symbol = (
                controller.snapshot().observation_overlay.symbol if has_chart
                else None
            )
            return (
                None,
                *([] for _ in TIERS),
                message,
                *no_scroll,
                attempted,
                chart_symbol if chart_symbol else "None loaded",
                *((attempted, message) if has_chart else ()),
            )

        if not attempted or len(attempted) > 20:
            return rejected("Enter a valid ticker (maximum 20 characters).")
        records = membership_tier_records(
            build_quote_dashboard_view(controller.snapshot().state)
        )
        symbol, tier = _membership_search_tier(records, attempted)
        if tier is None:
            message = f"{symbol} is not a listed member at the current replay time."
            if has_chart:
                message += " The displayed chart is unchanged."
            return rejected(message)

        if has_chart:
            started = perf_timer.perf_counter()
            try:
                controller.select_overlay_symbol(symbol)
            except (ValueError, RuntimeError) as error:
                return rejected(f"Chart selection unavailable: {error}")
            finally:
                print(
                    f"OVERLAY SYMBOL SELECTION {symbol}: "
                    f"{perf_timer.perf_counter() - started:.3f}s backend",
                    flush=True,
                )

        tier_name = {"uni": "Uni only", "focus": "Focus only", "hot": "Hot"}[tier]
        message = f"{symbol} selected in {tier_name}."
        scroll = tuple(
            {"rowId": symbol, "rowPosition": "middle"}
            if current == tier and not from_grid else no_update
            for current in TIERS
        )
        return (
            symbol,
            *({"ids": [symbol]} if current == tier else [] for current in TIERS),
            message,
            *scroll,
            symbol,
            symbol if has_chart else "None loaded",
            *((symbol, message) if has_chart else ()),
        )

    @app.callback(
        Output("membership-symbol-inspector", "children"),
        Input("membership-selected-symbol", "data"),
        Input("rendered-event-count", "data"),
    )
    def update_membership_inspector(symbol: str | None, _event_count: int) -> Any:
        replay_view = build_quote_dashboard_view(controller.snapshot().state)
        return _membership_inspector(
            html, replay_view, symbol, live_mode=live_mode,
            operator_enabled=operator_enabled,
        )

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
            Input("membership-selected-symbol", "data"),
            State("observation-overlay-reference-mode", "data"),
            State("observation-overlay-chart", "figure"),
            State("observation-overlay-candle-field", "value"),
            prevent_initial_call=True,
        )
        def select_overlay_reference(
            _set_clicks: int | None,
            _reset_clicks: int | None,
            click_data: Mapping[str, Any] | None,
            _selected_symbol: str | None,
            choosing: bool,
            figure_data: Mapping[str, Any] | None,
            candle_field: str,
        ) -> tuple[Any, bool, str]:
            trigger = ctx.triggered_id
            if trigger == "membership-selected-symbol":
                current_overlay = controller.snapshot().observation_overlay
                if (
                    current_overlay is None
                    or _selected_symbol != current_overlay.symbol
                ):
                    return no_update, no_update, no_update
                return None, False, "Set 0% reference"
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
            Output("observation-overlay-title", "children"),
            Output("observation-overlay-description", "children"),
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
            Input("membership-selected-symbol", "data"),
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
            _selected_symbol: str | None,
            stored_theme: str | None,
            stored_viewport: Mapping[str, Any] | None,
        ) -> tuple[Any, ...]:
            overlay = controller.snapshot().observation_overlay
            if overlay is None:
                return (no_update,) * 10
            if (
                set(ctx.triggered_prop_ids) == {"membership-selected-symbol.data"}
                and _selected_symbol != overlay.symbol
            ):
                # A rejected ticker clears Membership details but must not
                # redraw or otherwise change the previously selected chart.
                return (no_update,) * 10
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
                f"Observation Overlay · {overlay_view.symbol}",
                _overlay_description(overlay, live_mode=live_mode),
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
        *([Input("live-refresh-count", "data")] if live_mode else []),
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
        *tail_and_state: Any,
    ) -> tuple[Any, ...]:
        if live_mode:
            _last_live_event_count, rendered_event_count, seek_time = tail_and_state
        else:
            rendered_event_count, seek_time = tail_and_state
        trigger = ctx.triggered_id
        if live_mode:
            updated = controller.snapshot()
        else:
            selected_speed = float(speed)
            if controller.snapshot().speed != selected_speed:
                controller.set_speed(selected_speed)

        seek_error: str | None = None
        if live_mode:
            pass  # The live projector is advanced only by committed tail events.
        elif trigger == "replay-toggle":
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
