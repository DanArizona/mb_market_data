"""Validated Nasdaq historical halt markers for one replay session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class OverlayHaltMarker:
    time_et: datetime
    kind: str
    reason_code: str


def load_overlay_halt_markers(
    path: Path, *, symbol: str, session_date: date
) -> tuple[OverlayHaltMarker, ...]:
    """Load confirmed events from the probe's normalized.json, without inference."""

    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Nasdaq halt evidence must be a JSON record list")
    markers: list[OverlayHaltMarker] = []
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
        markers.append(OverlayHaltMarker(halt, "halt", reason))
        resume_text = record.get("resumption_trade_time")
        if resume_text:
            resume_date = record.get("resumption_date")
            if resume_date != session_date.strftime("%m/%d/%Y"):
                raise ValueError("Resumption trade date differs from replay session")
            resume = _time(session_date, resume_text)
            if resume < halt:
                raise ValueError("Resumption trade precedes halt")
            markers.append(OverlayHaltMarker(resume, "resume", reason))
    return tuple(sorted(set(markers), key=lambda marker: (marker.time_et, marker.kind)))


def _time(session_date: date, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Nasdaq halt time is missing")
    return datetime.fromisoformat(f"{session_date.isoformat()}T{''.join(value.split())}").replace(tzinfo=ET)
