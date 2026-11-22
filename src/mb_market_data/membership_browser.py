"""Read-only, replay-causal three-tier membership browser data.

The journal stores nested Uni/Focus/Hot memberships.  The browser shows each
symbol exactly once, in its *highest* current tier, without changing the
underlying hierarchy or asserting any operator command capability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from mb_market_data.quote_dashboard_view import DashboardSymbolRow, QuoteDashboardView


TIERS = ("uni", "focus", "hot")


def highest_tier(channels: tuple[str, ...]) -> str:
    """Return the exclusive display tier of an already-member symbol."""

    for tier in reversed(TIERS):
        if tier in channels:
            return tier
    raise ValueError("symbol has no Uni, Focus, or Hot membership")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class MembershipSymbolDetails:
    symbol: str
    highest_tier: str
    channels: tuple[str, ...]
    revisions: Mapping[str, int]
    description: str | None
    last_price: float | None
    previous_close: float | None
    change_from_close_percent: float | None
    volume: int | None
    day_open: float | None
    day_high: float | None
    day_low: float | None
    estimated_market_cap: float | None
    security_status: str | None
    observation_status: str
    latest_channel: str | None
    observed_at_et: str | None

    def list_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "description": self.description,
        }


def describe_membership_symbol(row: DashboardSymbolRow) -> MembershipSymbolDetails:
    """Use only the latest quote already available at the replay cutoff."""

    observation = row.latest.observation if row.latest is not None else None
    values = observation.values if observation is not None else {}
    last_price = _number(values.get("quote_last_price"))
    close = _number(values.get("quote_close_price"))
    shares = _number(values.get("shares_outstanding"))
    volume = _number(values.get("quote_total_volume"))
    change = (
        100.0 * (last_price / close - 1.0)
        if last_price is not None and close is not None and close > 0
        else None
    )
    estimated_cap = (
        last_price * shares
        if last_price is not None and shares is not None and shares > 0
        else None
    )
    return MembershipSymbolDetails(
        symbol=row.symbol,
        highest_tier=highest_tier(row.channels),
        channels=row.channels,
        revisions=row.channel_revisions,
        description=values.get("description") or None,
        last_price=last_price,
        previous_close=close,
        change_from_close_percent=change,
        volume=int(volume) if volume is not None and volume >= 0 else None,
        day_open=_number(values.get("quote_open_price")),
        day_high=_number(values.get("quote_high_price")),
        day_low=_number(values.get("quote_low_price")),
        estimated_market_cap=estimated_cap,
        security_status=values.get("quote_security_status") or None,
        observation_status=observation.status if observation is not None else "not_observed",
        latest_channel=row.latest.acquisition.channel if row.latest is not None else None,
        observed_at_et=row.as_grid_record()["observed_at_et"],
    )


def membership_tier_records(view: QuoteDashboardView) -> dict[str, list[dict[str, Any]]]:
    """Partition a projected journal view into disjoint browser columns."""

    output: dict[str, list[dict[str, Any]]] = {tier: [] for tier in TIERS}
    for row in view.rows:
        details = describe_membership_symbol(row)
        output[details.highest_tier].append(details.list_record())
    return output


def selected_symbol_details(
    view: QuoteDashboardView, symbol: str | None
) -> MembershipSymbolDetails | None:
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    selected = symbol.strip().upper()
    return next(
        (describe_membership_symbol(row) for row in view.rows if row.symbol == selected),
        None,
    )
