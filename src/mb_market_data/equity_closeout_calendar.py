"""NYSE/Nasdaq U.S.-equity closeout schedule via exchange_calendars.

A single local, offline exchange calendar controls closeout reminder timing,
post-close acquisition admissibility and the next trading session. XNAS is
an alias of XNYS in exchange_calendars (both U.S. equity core sessions).

This is scheduled exchange time, NOT live open/closed status. Unexpected
exchange closures require explicit human handling; no speculative recovery.
Missing package, unsupported calendar, and API/calendar errors fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from functools import lru_cache
from importlib.metadata import version as package_version, PackageNotFoundError
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
EXCHANGE_CALENDAR = "XNYS"
CALENDAR_CONTRACT = "exchange-calendars-xnys-core-equities-v1"


@lru_cache(maxsize=16)
def _calendar_for_year(year: int):
    try:
        import exchange_calendars as xcals
    except ImportError as error:
        raise RuntimeError(
            "exchange_calendars is required for closeout. Install and "
            "qualify the reviewed dependency before enabling production."
        ) from error
    # Explicit start/end avoid get_calendar's default rolling one-year range.
    # Include the preceding December and following January for year boundaries.
    try:
        return xcals.get_calendar(
            EXCHANGE_CALENDAR,
            start=f"{year - 1}-12-01",
            end=f"{year + 1}-01-31",
        )
    except Exception as error:
        raise RuntimeError(
            f"Unable to resolve reviewed exchange calendar for {year}"
        ) from error


def _package_revision() -> str:
    try:
        return package_version("exchange_calendars")
    except PackageNotFoundError as error:
        raise RuntimeError("exchange_calendars distribution is not installed") from error


@dataclass(frozen=True, slots=True)
class EquitySession:
    session_date: date
    kind: str
    close_time: time | None
    reason: str
    calendar_contract: str = CALENDAR_CONTRACT
    calendar_revision: str = "unknown"

    @property
    def is_open(self) -> bool:
        return self.kind != "CLOSED"

    @property
    def closes_at(self) -> datetime:
        if self.close_time is None:
            raise ValueError(f"No exchange close for {self.session_date}: {self.reason}")
        return datetime.combine(self.session_date, self.close_time, tzinfo=ET)


def equity_session(day: date) -> EquitySession:
    """Resolve the actual locally scheduled NYSE/Nasdaq core close."""
    calendar = _calendar_for_year(day.year)
    revision = _package_revision()
    if not calendar.is_session(day.isoformat()):
        return EquitySession(day, "CLOSED", None,
                             "exchange calendar: holiday or weekend",
                             calendar_revision=revision)
    close_utc = calendar.session_close(day.isoformat())
    close_et = close_utc.to_pydatetime().astimezone(ET)
    if close_et.date() != day:
        raise RuntimeError(f"Calendar close date differs from session {day}")
    if not (time(12, 0) <= close_et.time() <= time(16, 0)):
        raise RuntimeError(f"Unexpected equity core-session close {close_et.isoformat()}")
    early = close_et.time() < time(16, 0)
    return EquitySession(day, "EARLY_CLOSE" if early else "REGULAR",
                         close_et.time(), "scheduled early close" if early
                         else "scheduled regular close", calendar_revision=revision)


def require_exchange_session(day: date) -> EquitySession:
    session = equity_session(day)
    if not session.is_open:
        raise ValueError(f"{day} is not an exchange trading session ({session.reason})")
    return session


def next_exchange_session(source: date) -> date:
    require_exchange_session(source)
    cal = _calendar_for_year(source.year)
    try:
        result = cal.next_session(source.isoformat())
    except Exception as error:
        raise RuntimeError(f"Cannot resolve next exchange session after {source}") from error
    target = result.date()
    require_exchange_session(target)
    return target


def previous_exchange_session(target: date) -> date:
    """Return the preceding actual equity trading session, including holidays."""
    require_exchange_session(target)
    cal = _calendar_for_year(target.year)
    try:
        result = cal.previous_session(target.isoformat())
    except Exception as error:
        raise RuntimeError(f"Cannot resolve previous exchange session before {target}") from error
    source = result.date()
    require_exchange_session(source)
    return source


def validate_next_exchange_session(source: date, target: date) -> None:
    expected = next_exchange_session(source)
    if target != expected:
        raise ValueError(
            f"Expected next exchange session {expected} after {source}, got {target}"
        )


def require_postclose_source_time(source: date, observed: datetime) -> EquitySession:
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Post-close time must be timezone-aware")
    session = require_exchange_session(source)
    now = observed.astimezone(ET)
    if now.date() > source:
        raise ValueError(
            "Snapshot must be acquired on source session ET calendar date; "
            "next-day snapshots are not valid completed-session volume evidence"
        )
    if now.date() < source:
        raise ValueError("Snapshot source session must not be in the future")
    if now < session.closes_at:
        raise ValueError(
            f"same-day daily-universe acquisition must run at or after "
            f"{session.close_time:%H:%M} ET"
        )
    return session


def reminder_due_at(source: date, *, minutes_after_close: int = 10) -> datetime:
    if minutes_after_close < 0 or minutes_after_close > 60:
        raise ValueError("Invalid reminder delay")
    session = require_exchange_session(source)
    return session.closes_at + timedelta(minutes=minutes_after_close)
