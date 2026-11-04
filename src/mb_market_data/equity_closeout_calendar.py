"""Reviewed U.S. equity trading schedule for closeout (2026-2028).

The reviewed official calendars are *locally frozen* to make closeout gates
deterministic and available offline. Unsupported years fail closed. Emergency
unscheduled closures/changes require an explicit operational correction;
this module does not pretend to observe live exchange status.

Sources (verified 2026-11-04):
NYSE https://www.nyse.com/trade/hours-calendars
Nasdaq 2026 https://www.nasdaqtrader.com/trader.aspx?id=calendar

Regular U.S. equity core hours: 09:30-16:00 ET.
Published early-close sessions: 09:30-13:00 ET.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


ET=ZoneInfo("America/New_York")
SUPPORTED_YEARS=frozenset({2026,2027,2028})

CLOSED_BY_YEAR={
    2026: frozenset({
        "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
        "2026-05-25","2026-06-19","2026-07-03","2026-09-07",
        "2026-11-26","2026-12-25",
    }),
    2027: frozenset({
        "2027-01-01","2027-01-18","2027-02-15","2027-03-26",
        "2027-05-31","2027-06-18","2027-07-05","2027-09-06",
        "2027-11-25","2027-12-24",
    }),
    2028: frozenset({
        "2028-01-17","2028-02-21","2028-04-14","2028-05-29",
        "2028-06-19","2028-07-04","2028-09-04","2028-11-23",
        "2028-12-25",
    }),
}
EARLY_CLOSES={
    "2026-11-27":"13:00",
    "2026-12-24":"13:00",
    "2027-11-26":"13:00",
    "2028-07-03":"13:00",
    "2028-11-24":"13:00",
}


@dataclass(frozen=True, slots=True)
class EquitySession:
    session_date: date
    kind: str   # REGULAR / EARLY_CLOSE / CLOSED
    close_time: time | None
    reason: str
    calendar_contract: str = "us-equities-reviewed-2026-2028-v1"

    @property
    def is_open(self) -> bool:
        return self.kind != "CLOSED"

    @property
    def closes_at(self) -> datetime:
        if self.close_time is None:
            raise ValueError(f"No exchange close for {self.session_date}: {self.reason}")
        return datetime.combine(self.session_date,self.close_time,tzinfo=ET)


def equity_session(day: date) -> EquitySession:
    """Use reviewed calendar only, fail closed outside its supported years."""
    if day.year not in SUPPORTED_YEARS:
        raise ValueError(f"Unreviewed exchange-calendar year {day.year}: cannot assume session")
    if day.weekday() >= 5:
        return EquitySession(day,"CLOSED",None,"weekend")
    if day.isoformat() in CLOSED_BY_YEAR[day.year]:
        return EquitySession(day,"CLOSED",None,"published exchange holiday")
    if day.isoformat() in EARLY_CLOSES:
        return EquitySession(day,"EARLY_CLOSE",time.fromisoformat(EARLY_CLOSES[day.isoformat()]),
                             "published early equity close")
    return EquitySession(day,"REGULAR",time(16,0),"scheduled regular equity close")


def require_exchange_session(day:date) -> EquitySession:
    session=equity_session(day)
    if not session.is_open:
        raise ValueError(f"{day} is not an exchange trading session ({session.reason})")
    return session


def next_exchange_session(source:date) -> date:
    require_exchange_session(source)
    candidate=source+timedelta(days=1)
    for _ in range(12):
        if equity_session(candidate).is_open:
            return candidate
        candidate+=timedelta(days=1)
    raise ValueError(f"No next reviewed exchange trading session after {source}")


def validate_next_exchange_session(source:date,target:date) -> None:
    expected=next_exchange_session(source)
    if target!=expected:
        raise ValueError(f"Expected next exchange session {expected} after {source}, got {target}")


def require_postclose_source_time(source:date, observed:datetime) -> EquitySession:
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Post-close time must be timezone-aware")
    session=require_exchange_session(source)
    now=observed.astimezone(ET)
    if now.date()>source:
        raise ValueError("Snapshot must be acquired on source ET calendar date; next-day snapshots are not valid completed-session volume evidence")
    if now.date()<source:
        raise ValueError("Snapshot source session must not be in the future")
    if now<session.closes_at:
        raise ValueError(f"Post-close snapshot requires {session.close_time:%H:%M} ET or later")
    return session


def reminder_due_at(source:date, *, minutes_after_close:int=10) -> datetime:
    session=require_exchange_session(source)
    return session.closes_at+timedelta(minutes=minutes_after_close)
