"""Validated Nasdaq historical halt intervals for one replay session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class OverlayHaltInterval:
    start_et: datetime
    end_et: datetime | None
    reason_code: str
    # None identifies older historical snapshots with no first-observed clock.
    # A daily Nasdaq episode is a point-in-time report, NOT a verified span.
    first_observed_at_utc: datetime | None = None


def load_overlay_halt_intervals(
    path: Path, *, symbol: str, session_date: date
) -> tuple[OverlayHaltInterval, ...]:
    """Load confirmed intervals from the probe's normalized.json."""

    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Nasdaq halt evidence must be a JSON record list")
    intervals: list[OverlayHaltInterval] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Nasdaq halt record must be an object")
        if str(record.get("symbol", "")).upper() != symbol.upper():
            continue
        if record.get("halt_date") != session_date.strftime("%m/%d/%Y"):
            continue
        if record.get("retrieval_mode") != "HISTORICAL":
            raise ValueError("Overlay requires historical Nasdaq halt evidence")
        reason = str(record.get("reason_code", "")).strip()
        if not reason:
            raise ValueError("Nasdaq halt reason is missing")
        halt = _time(session_date, record.get("halt_time"))
        resume_text = record.get("resumption_trade_time")
        resume: datetime | None = None
        if resume_text:
            resume_date = record.get("resumption_date")
            if resume_date != session_date.strftime("%m/%d/%Y"):
                raise ValueError("Resumption trade date differs from replay session")
            resume = _time(session_date, resume_text)
            if resume < halt:
                raise ValueError("Resumption trade precedes halt")
        intervals.append(OverlayHaltInterval(halt, resume, reason))
    return tuple(
        sorted(
            set(intervals),
            key=lambda interval: (
                interval.start_et,
                interval.end_et or interval.start_et,
                interval.reason_code,
            ),
        )
    )


def load_daily_nasdaq_halt_events(
    path: Path, *, symbol: str, session_date: date
) -> tuple[OverlayHaltInterval, ...]:
    """Load first-observed Nasdaq episodes from one completed daily session.

    The daily producer's events.json is keyed by ``SYMBOL@halted_at_utc``.
    Its last saved record can contain release fields learned later; those
    fields MUST NOT be treated as a release visible at the halt's observation.
    Accordingly, these are reported halt *events* with no asserted end time.
    """

    episodes = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(episodes, dict):
        raise ValueError("daily Nasdaq episodes must be a JSON object")
    selected = symbol.upper()
    intervals: list[OverlayHaltInterval] = []
    for key, episode in episodes.items():
        if not isinstance(key, str) or not key.startswith(selected + "@"):
            continue
        if not isinstance(episode, dict) or not isinstance(episode.get("record"), dict):
            raise ValueError("daily Nasdaq episode is missing its record")
        record = episode["record"]
        if str(record.get("symbol", "")).upper() != selected:
            raise ValueError("daily Nasdaq episode symbol differs from key")
        keyed_at = _parse_aware(key.split("@", 1)[1], "episode key")
        keyed_date = keyed_at.astimezone(ET).date()
        recorded_date = record.get("halt_date")
        if keyed_date != session_date:
            # A CURRENT feed can still report an episode that began on a
            # previous trading date. Do not present it as today's new halt
            # or block this symbol's otherwise valid quote history.
            # Only ignore it when its key and recorded date agree.
            if recorded_date != keyed_date.strftime("%m/%d/%Y"):
                raise ValueError("daily Nasdaq episode key differs from halt date")
            continue
        if recorded_date != session_date.strftime("%m/%d/%Y"):
            raise ValueError("daily Nasdaq episode belongs to a different session")
        if record.get("retrieval_mode") != "CURRENT":
            raise ValueError("daily Nasdaq episode must have CURRENT retrieval mode")
        reason = str(record.get("reason_code", "")).strip()
        if not reason:
            raise ValueError("daily Nasdaq halt reason is missing")
        start = _time(session_date, record.get("halt_time"))
        if abs((keyed_at - start.astimezone(timezone.utc)).total_seconds()) > 0.001:
            raise ValueError("daily Nasdaq episode key differs from halt time")
        first = _parse_aware(episode.get("first_observed_at"), "first_observed_at")
        last = _parse_aware(episode.get("last_observed_at"), "last_observed_at")
        if first < keyed_at or last < first:
            raise ValueError("daily Nasdaq episode observation times are inconsistent")
        if first.astimezone(ET).date() != session_date:
            raise ValueError("daily Nasdaq first observation falls outside session")
        intervals.append(OverlayHaltInterval(start, None, reason, first))
    return tuple(sorted(intervals, key=lambda item: (item.start_et, item.first_observed_at_utc)))


def _parse_aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} is not an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time(session_date: date, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Nasdaq halt time is missing")
    return datetime.fromisoformat(f"{session_date.isoformat()}T{''.join(value.split())}").replace(tzinfo=ET)
