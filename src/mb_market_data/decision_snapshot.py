"""
Immutable per-symbol decision-time market-data snapshot.

This module combines:

    - ThinkOrSwim OV_DECISION data
    - Schwab batched quote data
    - actual acquisition timing

into a typed, immutable record representing what MasterBot knew when
making a market-open decision.

This layer intentionally does NOT calculate ranking features, historical
overnight-volume statistics, market capitalization, or Watchlist scores.

Internal timestamps are stored in UTC. Human-facing applications may
display them in America/New_York as appropriate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from mb_market_data.schwab_quotes import (
    QuoteResult,
    QuoteStatus,
)
from mb_market_data.tos_watchlist import (
    OVDecisionStatus,
    TosWatchlistRow,
    USABLE_OV_STATUSES,
)


UTC = timezone.utc


class DecisionSnapshotError(RuntimeError):
    """Base exception for decision-snapshot construction."""


class DecisionSnapshotDataError(DecisionSnapshotError):
    """Source data contained an unexpected or unusable value."""


@dataclass(frozen=True)
class DecisionSnapshot:
    """
    Immutable decision-time data for one symbol.

    The snapshot preserves raw acquisition facts. Derived strategy
    features belong in later processing layers.
    """

    # Identity
    trade_date: date
    symbol: str

    # ThinkOrSwim OV_DECISION
    ov_decision: int | None
    ov_decision_status: OVDecisionStatus
    raw_ov_decision: str
    tos_observed_at_utc: datetime

    # Schwab acquisition provenance
    quote_status: QuoteStatus
    quote_detail: str | None
    schwab_batch_number: int
    schwab_request_started_at_utc: datetime
    schwab_response_received_at_utc: datetime | None

    # Schwab top-level identity/status
    asset_main_type: str | None
    asset_sub_type: str | None
    realtime: bool | None

    # Quote prices
    bid_price: float | None
    ask_price: float | None
    last_price: float | None
    mark: float | None
    close_price: float | None
    open_price: float | None
    high_price: float | None
    low_price: float | None

    # Quote sizes
    bid_size: int | None
    ask_size: int | None
    last_size: int | None

    # Quote volume/status
    total_volume: int | None
    security_status: str | None

    # Market-data event times
    quote_time_utc: datetime | None
    trade_time_utc: datetime | None
    bid_time_utc: datetime | None
    ask_time_utc: datetime | None

    # Regular-market fields
    regular_market_last_price: float | None
    regular_market_last_size: int | None
    regular_market_trade_time_utc: datetime | None

    # Fundamental fields
    shares_outstanding: int | None
    avg_10_days_volume: float | None
    avg_1_year_volume: float | None

    # Reference fields
    exchange: str | None
    exchange_name: str | None
    description: str | None

    @property
    def has_usable_ov_decision(self) -> bool:
        """True when today's OV_DECISION is usable."""

        return (
            self.ov_decision_status
            in USABLE_OV_STATUSES
        )

    @property
    def has_schwab_quote(self) -> bool:
        """True when Schwab returned quote data."""

        return (
            self.quote_status
            == QuoteStatus.QUOTE
        )


def _to_utc(
    value: datetime,
    *,
    field_name: str,
) -> datetime:
    """
    Require a timezone-aware datetime and normalize it to UTC.
    """

    if not isinstance(value, datetime):
        raise TypeError(
            f"{field_name} must be a datetime"
        )

    if (
        value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(
            f"{field_name} must be timezone-aware"
        )

    return value.astimezone(UTC)


def _section(
    payload: Mapping[str, Any],
    name: str,
) -> Mapping[str, Any]:
    """
    Return an optional nested Schwab object.

    A missing section is allowed. A present non-object section is not.
    """

    value = payload.get(name)

    if value is None:
        return {}

    if not isinstance(value, Mapping):
        raise DecisionSnapshotDataError(
            f"Schwab field {name!r} must be an object."
        )

    return value


def _optional_string(
    payload: Mapping[str, Any],
    key: str,
) -> str | None:
    value = payload.get(key)

    if value is None:
        return None

    if not isinstance(value, str):
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must be a string."
        )

    return value


def _optional_bool(
    payload: Mapping[str, Any],
    key: str,
) -> bool | None:
    value = payload.get(key)

    if value is None:
        return None

    if not isinstance(value, bool):
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must be boolean."
        )

    return value


def _optional_number(
    payload: Mapping[str, Any],
    key: str,
) -> float | None:
    """
    Read an optional finite numeric value.
    """

    value = payload.get(key)

    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(
            value,
            (int, float),
        )
    ):
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must be numeric."
        )

    result = float(value)

    if not math.isfinite(result):
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must be finite."
        )

    return result


def _optional_integer(
    payload: Mapping[str, Any],
    key: str,
) -> int | None:
    """
    Read an optional nonnegative integral value.

    Integral floats such as 123.0 are accepted.
    """

    value = payload.get(key)

    if value is None:
        return None

    if isinstance(value, bool):
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must be "
            "a nonnegative integer."
        )

    if isinstance(value, int):
        result = value

    elif (
        isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
    ):
        result = int(value)

    else:
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must be "
            "a nonnegative integer."
        )

    if result < 0:
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must be "
            "nonnegative."
        )

    return result


def _optional_epoch_ms(
    payload: Mapping[str, Any],
    key: str,
) -> datetime | None:
    """
    Convert optional Schwab epoch milliseconds to UTC.
    """

    value = payload.get(key)

    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(
            value,
            (int, float),
        )
    ):
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must contain "
            "numeric epoch milliseconds."
        )

    numeric_value = float(value)

    if not math.isfinite(numeric_value):
        raise DecisionSnapshotDataError(
            f"Schwab field {key!r} must contain "
            "finite epoch milliseconds."
        )

    return datetime.fromtimestamp(
        numeric_value / 1000.0,
        tz=UTC,
    )


def build_decision_snapshot(
    *,
    trade_date: date,
    tos_row: TosWatchlistRow,
    quote_result: QuoteResult,
    tos_observed_at_utc: datetime,
) -> DecisionSnapshot:
    """
    Build one immutable decision-time snapshot.

    Parameters
    ----------
    trade_date
        Trading date represented by the decision snapshot.

    tos_row
        Parsed ThinkOrSwim row containing OV_DECISION.

    quote_result
        Result of Schwab batched quote acquisition for the same symbol.

    tos_observed_at_utc
        Time at which the controlling process observed/accepted the ToS
        data used for this decision. The value must be timezone-aware.

    Returns
    -------
    DecisionSnapshot
        Immutable per-symbol raw decision input.
    """

    if not isinstance(trade_date, date):
        raise TypeError(
            "trade_date must be a date"
        )

    symbol = tos_row.symbol.strip().upper()

    quote_symbol = (
        quote_result.symbol
        .strip()
        .upper()
    )

    if symbol != quote_symbol:
        raise ValueError(
            "ToS and Schwab symbols do not match: "
            f"{symbol!r} != {quote_symbol!r}"
        )

    tos_time_utc = _to_utc(
        tos_observed_at_utc,
        field_name="tos_observed_at_utc",
    )

    request_started_at_utc = _to_utc(
        quote_result.request_started_at_utc,
        field_name=(
            "quote_result."
            "request_started_at_utc"
        ),
    )

    if (
        quote_result.response_received_at_utc
        is None
    ):
        response_received_at_utc = None
    else:
        response_received_at_utc = _to_utc(
            quote_result.response_received_at_utc,
            field_name=(
                "quote_result."
                "response_received_at_utc"
            ),
        )

    # Default all Schwab payload-derived fields to unavailable.
    asset_main_type = None
    asset_sub_type = None
    realtime = None

    bid_price = None
    ask_price = None
    last_price = None
    mark = None
    close_price = None
    open_price = None
    high_price = None
    low_price = None

    bid_size = None
    ask_size = None
    last_size = None

    total_volume = None
    security_status = None

    quote_time_utc = None
    trade_time_utc = None
    bid_time_utc = None
    ask_time_utc = None

    regular_market_last_price = None
    regular_market_last_size = None
    regular_market_trade_time_utc = None

    shares_outstanding = None
    avg_10_days_volume = None
    avg_1_year_volume = None

    exchange = None
    exchange_name = None
    description = None

    if quote_result.status == QuoteStatus.QUOTE:
        payload = quote_result.quote

        if not isinstance(
            payload,
            Mapping,
        ):
            raise DecisionSnapshotDataError(
                "A QUOTE result must contain "
                "a Schwab quote object."
            )

        payload_symbol = _optional_string(
            payload,
            "symbol",
        )

        if (
            payload_symbol is not None
            and payload_symbol.strip().upper()
            != symbol
        ):
            raise DecisionSnapshotDataError(
                "Schwab payload symbol does not "
                f"match requested symbol {symbol!r}."
            )

        quote = _section(
            payload,
            "quote",
        )

        fundamental = _section(
            payload,
            "fundamental",
        )

        reference = _section(
            payload,
            "reference",
        )

        regular = _section(
            payload,
            "regular",
        )

        asset_main_type = _optional_string(
            payload,
            "assetMainType",
        )

        asset_sub_type = _optional_string(
            payload,
            "assetSubType",
        )

        realtime = _optional_bool(
            payload,
            "realtime",
        )

        bid_price = _optional_number(
            quote,
            "bidPrice",
        )

        ask_price = _optional_number(
            quote,
            "askPrice",
        )

        last_price = _optional_number(
            quote,
            "lastPrice",
        )

        mark = _optional_number(
            quote,
            "mark",
        )

        close_price = _optional_number(
            quote,
            "closePrice",
        )

        open_price = _optional_number(
            quote,
            "openPrice",
        )

        high_price = _optional_number(
            quote,
            "highPrice",
        )

        low_price = _optional_number(
            quote,
            "lowPrice",
        )

        bid_size = _optional_integer(
            quote,
            "bidSize",
        )

        ask_size = _optional_integer(
            quote,
            "askSize",
        )

        last_size = _optional_integer(
            quote,
            "lastSize",
        )

        total_volume = _optional_integer(
            quote,
            "totalVolume",
        )

        security_status = _optional_string(
            quote,
            "securityStatus",
        )

        quote_time_utc = _optional_epoch_ms(
            quote,
            "quoteTime",
        )

        trade_time_utc = _optional_epoch_ms(
            quote,
            "tradeTime",
        )

        bid_time_utc = _optional_epoch_ms(
            quote,
            "bidTime",
        )

        ask_time_utc = _optional_epoch_ms(
            quote,
            "askTime",
        )

        regular_market_last_price = (
            _optional_number(
                regular,
                "regularMarketLastPrice",
            )
        )

        regular_market_last_size = (
            _optional_integer(
                regular,
                "regularMarketLastSize",
            )
        )

        regular_market_trade_time_utc = (
            _optional_epoch_ms(
                regular,
                "regularMarketTradeTime",
            )
        )

        shares_outstanding = (
            _optional_integer(
                fundamental,
                "sharesOutstanding",
            )
        )

        avg_10_days_volume = (
            _optional_number(
                fundamental,
                "avg10DaysVolume",
            )
        )

        avg_1_year_volume = (
            _optional_number(
                fundamental,
                "avg1YearVolume",
            )
        )

        exchange = _optional_string(
            reference,
            "exchange",
        )

        exchange_name = _optional_string(
            reference,
            "exchangeName",
        )

        description = _optional_string(
            reference,
            "description",
        )

    return DecisionSnapshot(
        trade_date=trade_date,
        symbol=symbol,

        ov_decision=tos_row.ov_decision,
        ov_decision_status=(
            tos_row.ov_decision_status
        ),
        raw_ov_decision=(
            tos_row.raw_ov_decision
        ),
        tos_observed_at_utc=tos_time_utc,

        quote_status=quote_result.status,
        quote_detail=quote_result.detail,
        schwab_batch_number=(
            quote_result.batch_number
        ),
        schwab_request_started_at_utc=(
            request_started_at_utc
        ),
        schwab_response_received_at_utc=(
            response_received_at_utc
        ),

        asset_main_type=asset_main_type,
        asset_sub_type=asset_sub_type,
        realtime=realtime,

        bid_price=bid_price,
        ask_price=ask_price,
        last_price=last_price,
        mark=mark,
        close_price=close_price,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,

        bid_size=bid_size,
        ask_size=ask_size,
        last_size=last_size,

        total_volume=total_volume,
        security_status=security_status,

        quote_time_utc=quote_time_utc,
        trade_time_utc=trade_time_utc,
        bid_time_utc=bid_time_utc,
        ask_time_utc=ask_time_utc,

        regular_market_last_price=(
            regular_market_last_price
        ),
        regular_market_last_size=(
            regular_market_last_size
        ),
        regular_market_trade_time_utc=(
            regular_market_trade_time_utc
        ),

        shares_outstanding=shares_outstanding,
        avg_10_days_volume=avg_10_days_volume,
        avg_1_year_volume=avg_1_year_volume,

        exchange=exchange,
        exchange_name=exchange_name,
        description=description,
    )
