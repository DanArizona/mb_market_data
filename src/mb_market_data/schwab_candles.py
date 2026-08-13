"""
Schwab five-minute candle acquisition and selection.

This module does not create or authenticate Schwab clients.  Callers
provide an already-authenticated client having a price_history() method.

All human-facing market times use America/New_York.

The immediate production requirement is retrieving the five-minute
candle beginning at 09:25 ET:

    09:25 <= ET < 09:30

Its volume can be combined with OV_DECISION to derive OV_FINAL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from mb_market_data.overnight_volume import validate_volume


ET = ZoneInfo("America/New_York")
CANDLE_0925_TIME = time(9, 25)


class SchwabCandleError(RuntimeError):
    """Base exception for Schwab candle operations."""


class PriceHistoryRequestError(SchwabCandleError):
    """Schwab price-history request failed."""


class CandleNotFoundError(SchwabCandleError):
    """Requested candle was not present in the response."""


class CandleDataError(SchwabCandleError):
    """A returned candle contained unusable data."""


@dataclass(frozen=True)
class SchwabCandle:
    """One Schwab five-minute OHLCV candle."""

    symbol: str
    start_et: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def normalize_trade_date(
    value: date | datetime,
) -> date:
    """
    Normalize a date or datetime to a trading date.

    If a datetime is supplied, its date component is used.
    """

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    raise TypeError(
        "trade_date must be a date or datetime, "
        f"got {type(value).__name__}"
    )


def candle_datetime_et(
    epoch_ms: int | float,
) -> datetime:
    """Convert Schwab epoch milliseconds to Eastern Time."""

    if isinstance(epoch_ms, bool) or not isinstance(
        epoch_ms,
        (int, float),
    ):
        raise CandleDataError(
            "Candle datetime must be numeric epoch milliseconds."
        )

    return datetime.fromtimestamp(
        epoch_ms / 1000.0,
        tz=ET,
    )


def _numeric_field(
    candle: Mapping[str, Any],
    field: str,
) -> float:
    """Read one required numeric candle field."""

    value = candle.get(field)

    if isinstance(value, bool) or not isinstance(
        value,
        (int, float),
    ):
        raise CandleDataError(
            f"Candle field {field!r} must be numeric."
        )

    return float(value)


def _volume_field(
    candle: Mapping[str, Any],
) -> int:
    """Read and validate a candle volume."""

    value = candle.get("volume")

    if isinstance(value, bool):
        raise CandleDataError(
            "Candle volume must be a nonnegative integer."
        )

    if isinstance(value, int):
        try:
            return validate_volume(
                value,
                name="candle volume",
            )
        except (TypeError, ValueError) as exc:
            raise CandleDataError(str(exc)) from exc

    if isinstance(value, float) and value.is_integer():
        integer_value = int(value)

        try:
            return validate_volume(
                integer_value,
                name="candle volume",
            )
        except (TypeError, ValueError) as exc:
            raise CandleDataError(str(exc)) from exc

    raise CandleDataError(
        "Candle volume must be a nonnegative integral value."
    )


def parse_candle(
    symbol: str,
    candle: Mapping[str, Any],
) -> SchwabCandle:
    """Convert one raw Schwab candle into a validated object."""

    if "datetime" not in candle:
        raise CandleDataError(
            "Candle does not contain a datetime field."
        )

    start_et = candle_datetime_et(
        candle["datetime"]
    )

    return SchwabCandle(
        symbol=symbol.strip().upper(),
        start_et=start_et,
        open=_numeric_field(candle, "open"),
        high=_numeric_field(candle, "high"),
        low=_numeric_field(candle, "low"),
        close=_numeric_field(candle, "close"),
        volume=_volume_field(candle),
    )


def find_five_minute_candle(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date | datetime,
    candle_time: time,
) -> SchwabCandle:
    """
    Find one exact five-minute candle in a Schwab price-history payload.

    Matching is performed in America/New_York using both trading date
    and candle start time.
    """

    wanted_date = normalize_trade_date(
        trade_date
    )

    candles = payload.get("candles")

    if not isinstance(candles, list):
        raise CandleDataError(
            "Price-history payload does not contain a candle list."
        )

    for raw_candle in candles:
        if not isinstance(
            raw_candle,
            Mapping,
        ):
            continue

        if "datetime" not in raw_candle:
            continue

        candle_dt = candle_datetime_et(
            raw_candle["datetime"]
        )

        candle_clock = candle_dt.time().replace(
            second=0,
            microsecond=0,
        )

        if (
            candle_dt.date() == wanted_date
            and candle_clock == candle_time
        ):
            return parse_candle(
                symbol,
                raw_candle,
            )

    raise CandleNotFoundError(
        f"No {candle_time.strftime('%H:%M')} ET candle "
        f"found for {symbol.upper()} on {wanted_date}."
    )


def find_0925_candle(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date | datetime,
) -> SchwabCandle:
    """Find the five-minute candle beginning at 09:25 ET."""

    return find_five_minute_candle(
        payload,
        symbol=symbol,
        trade_date=trade_date,
        candle_time=CANDLE_0925_TIME,
    )


def fetch_0925_candle(
    client: Any,
    *,
    symbol: str,
    trade_date: date | datetime,
) -> SchwabCandle:
    """
    Fetch and return the Schwab 09:25 ET five-minute candle.

    The supplied client must already be authenticated and must provide
    the schwabdev-style price_history() method.
    """

    wanted_date = normalize_trade_date(
        trade_date
    )

    start_dt = datetime.combine(
        wanted_date,
        time.min,
        tzinfo=ET,
    )

    end_dt = datetime.combine(
        wanted_date,
        time.max,
        tzinfo=ET,
    )

    response = client.price_history(
        symbol.upper(),
        frequencyType="minute",
        frequency=5,
        startDate=start_dt,
        endDate=end_dt,
        needExtendedHoursData=True,
        needPreviousClose=True,
    )

    if not getattr(
        response,
        "ok",
        False,
    ):
        status_code = getattr(
            response,
            "status_code",
            "unknown",
        )

        raise PriceHistoryRequestError(
            f"Schwab price_history failed for "
            f"{symbol.upper()} on {wanted_date}: "
            f"HTTP {status_code}"
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise PriceHistoryRequestError(
            "Schwab price_history returned "
            "an unreadable JSON response."
        ) from exc

    if not isinstance(
        payload,
        Mapping,
    ):
        raise PriceHistoryRequestError(
            "Schwab price_history JSON response "
            "is not an object."
        )

    return find_0925_candle(
        payload,
        symbol=symbol,
        trade_date=wanted_date,
    )
