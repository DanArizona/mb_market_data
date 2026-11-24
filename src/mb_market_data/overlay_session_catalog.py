"""Safe, read-only discovery of dated daily-flow journals for Overlay attachment.

This does not start a viewer, contact Schwab, modify the journal, infer liveness
from a saved PID, or authorize operator membership actions. Run from a separate
experimental checkout, pointing to the actual production *data* root.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 2
MEMBERSHIP_CONTRACT = "nested-uni-focus-hot-v1"


class SessionDiscoveryError(ValueError):
    """A candidate cannot be attached as a verified dated session."""


@dataclass(frozen=True)
class DatedSession:
    session_date: str
    data_root: str
    plan_path: str
    journal_path: str
    controller_phase_saved: str | None
    status_saved_at: str | None
    nasdaq_events_path: str | None
    polling_health_directory: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _session_day(value: str | date) -> str:
    try:
        day = date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise SessionDiscoveryError("Invalid ET session date") from error
    return day.isoformat()


def _json_object(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SessionDiscoveryError(f"Unreadable session evidence: {path.name}") from error
    if not isinstance(obj, dict):
        raise SessionDiscoveryError(f"Invalid session evidence object: {path.name}")
    return obj


def inspect_session(data_root: str | Path, session_date: str | date) -> DatedSession:
    """Validate plan identity against a *read-only* SQLite schema-v2 journal.

    This is an identity/attachment gate, not a complete integrity audit or a
    realtime health verdict. An in-progress journal may be valid to inspect.
    """
    root = Path(data_root).resolve()
    day = _session_day(session_date)
    if not root.is_dir():
        raise SessionDiscoveryError("Data root is not an existing directory")
    state = root / "output" / "daily_session" / day
    plan_path = state / "plan.json"
    journal = root / "output" / f"quote_observation_journal_v2_api_opening_{day}" / f"{day}.sqlite3"
    if not plan_path.is_file():
        raise SessionDiscoveryError(f"No daily controller plan for {day}")
    if not journal.is_file():
        raise SessionDiscoveryError(f"No daily journal for {day}")
    plan = _json_object(plan_path)
    if plan.get("session_date") != day:
        raise SessionDiscoveryError("Plan session date does not match selected day")
    for key, expected in (("data_root", root), ("state_root", state), ("journal", journal)):
        raw = plan.get(key)
        if not isinstance(raw, str) or not raw.strip() or Path(raw).resolve() != expected.resolve():
            raise SessionDiscoveryError(f"Plan {key} differs from selected session")
    if plan.get("contract") != MEMBERSHIP_CONTRACT:
        raise SessionDiscoveryError("Unsupported hierarchy plan contract")

    try:
        with sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0) as db:
            db.execute("PRAGMA query_only=ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise SessionDiscoveryError("Unsupported journal schema version")
            identity = db.execute(
                "SELECT schema_version, session_date, membership_contract "
                "FROM store_identity WHERE singleton=1"
            ).fetchone()
            if identity != (SCHEMA_VERSION, day, MEMBERSHIP_CONTRACT):
                raise SessionDiscoveryError("Journal identity does not match selected day")
    except sqlite3.Error as error:
        raise SessionDiscoveryError("Journal identity cannot be read safely") from error

    status_path = state / "status.json"
    status = _json_object(status_path) if status_path.is_file() else {}
    # Never present saved PID/status as a verified live process heartbeat.
    if status and status.get("session_date") != day:
        raise SessionDiscoveryError("Saved status belongs to a different session")
    nasdaq = state / "nasdaq" / "events.json"
    health = root / "output" / "polling_health" / day
    return DatedSession(
        session_date=day,
        data_root=str(root),
        plan_path=str(plan_path),
        journal_path=str(journal),
        controller_phase_saved=str(status["phase"]) if "phase" in status else None,
        status_saved_at=str(status["at"]) if "at" in status else None,
        nasdaq_events_path=str(nasdaq) if nasdaq.is_file() else None,
        polling_health_directory=str(health) if health.is_dir() else None,
    )


def catalog_sessions(data_root: str | Path) -> tuple[list[DatedSession], dict[str, str]]:
    """Discover *validated* session dates, returning rejected candidates separately."""
    root = Path(data_root).resolve()
    folder = root / "output" / "daily_session"
    if not folder.is_dir():
        return [], {}
    accepted: list[DatedSession] = []
    rejected: dict[str, str] = {}
    for state in sorted(folder.iterdir()):
        if not state.is_dir() or len(state.name) != 10:
            continue
        try:
            day = _session_day(state.name)
        except SessionDiscoveryError:
            continue
        try:
            accepted.append(inspect_session(root, day))
        except SessionDiscoveryError as error:
            rejected[day] = str(error)
    return accepted, rejected
