"""Framework-neutral view model for the quote-state dashboard."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from mb_market_data.quote_event_state import (
    ProjectedQuoteObservation,
    QuoteEventStateSnapshot,
)


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class DashboardChannelSummary:
    """Current membership and latest-result coverage for one channel."""

    channel: str
    revision: int
    member_count: int
    latest_count: int
    status_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class DashboardSymbolRow:
    """One unique current symbol across all sampling channels."""

    symbol: str
    channels: tuple[str, ...]
    channel_revisions: Mapping[str, int]
    latest: ProjectedQuoteObservation | None

    def as_grid_record(self) -> dict[str, Any]:
        values = self.latest.observation.values if self.latest else {}
        acquisition = self.latest.acquisition if self.latest else None
        observation = self.latest.observation if self.latest else None
        return {
            "symbol": self.symbol,
            "channels": ", ".join(self.channels),
            "uni_revision": _revision_label(
                self.channel_revisions.get("uni")
            ),
            "focus_revision": _revision_label(
                self.channel_revisions.get("focus")
            ),
            "hot_revision": _revision_label(
                self.channel_revisions.get("hot")
            ),
            "latest_channel": acquisition.channel if acquisition else None,
            "status": observation.status if observation else "not_observed",
            "last_price": values.get("quote_last_price"),
            "bid_price": values.get("quote_bid_price"),
            "ask_price": values.get("quote_ask_price"),
            "mark": values.get("quote_mark"),
            "total_volume": values.get("quote_total_volume"),
            "observed_at_et": (
                acquisition.completed_at_utc.astimezone(ET).isoformat(
                    timespec="milliseconds"
                )
                if acquisition is not None
                else None
            ),
            "exchange": values.get("exchange"),
            "description": values.get("description"),
        }


@dataclass(frozen=True, slots=True)
class QuoteDashboardView:
    """Complete read-only dashboard state at one event-stream position."""

    session_date: date | None
    as_of_utc: datetime | None
    event_count: int
    acquisition_count: int
    observation_count: int
    unique_member_count: int
    multi_channel_count: int
    channels: tuple[DashboardChannelSummary, ...]
    rows: tuple[DashboardSymbolRow, ...]

    @property
    def as_of_et_text(self) -> str:
        if self.as_of_utc is None:
            return "No events"
        return (
            self.as_of_utc.astimezone(ET).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )[:-3]
            + " ET"
        )

    def grid_records(self) -> list[dict[str, Any]]:
        return [row.as_grid_record() for row in self.rows]


def _revision_label(revision: int | None) -> str | None:
    return f"r{revision}" if revision is not None else None


def _latest_for_symbol(
    state: QuoteEventStateSnapshot,
    symbol: str,
    channels: tuple[str, ...],
) -> ProjectedQuoteObservation | None:
    candidates = tuple(
        latest
        for channel in channels
        if (latest := state.latest(channel, symbol)) is not None
    )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.available_at_utc,
            item.acquisition.channel,
            item.acquisition.acquisition_id,
        ),
    )


def build_quote_dashboard_view(
    state: QuoteEventStateSnapshot,
) -> QuoteDashboardView:
    """Collapse channel state into one deterministic row per current symbol."""

    memberships: dict[str, list[str]] = {}
    channel_revisions: dict[str, int] = {}
    channel_summaries: list[DashboardChannelSummary] = []

    for channel in state.channels:
        revision = state.channel_revisions[channel]
        channel_revisions[channel] = revision.revision
        current_rows = state.channel_rows(channel)
        latest_rows = tuple(row for row in current_rows if row.latest)
        statuses = Counter(
            row.latest.observation.status for row in latest_rows
        )
        channel_summaries.append(
            DashboardChannelSummary(
                channel=channel,
                revision=revision.revision,
                member_count=len(current_rows),
                latest_count=len(latest_rows),
                status_counts=MappingProxyType(
                    dict(sorted(statuses.items()))
                ),
            )
        )
        for symbol in revision.symbols:
            memberships.setdefault(symbol, []).append(channel)

    symbol_rows: list[DashboardSymbolRow] = []
    for symbol in sorted(memberships):
        channels = tuple(sorted(memberships[symbol]))
        symbol_rows.append(
            DashboardSymbolRow(
                symbol=symbol,
                channels=channels,
                channel_revisions=MappingProxyType(
                    {
                        channel: channel_revisions[channel]
                        for channel in channels
                    }
                ),
                latest=_latest_for_symbol(state, symbol, channels),
            )
        )

    return QuoteDashboardView(
        session_date=state.session_date,
        as_of_utc=state.current_time_utc,
        event_count=state.event_count,
        acquisition_count=state.acquisition_count,
        observation_count=state.observation_count,
        unique_member_count=len(memberships),
        multi_channel_count=sum(
            len(channels) > 1 for channels in memberships.values()
        ),
        channels=tuple(channel_summaries),
        rows=tuple(symbol_rows),
    )
