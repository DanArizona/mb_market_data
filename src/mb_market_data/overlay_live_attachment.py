"""Read-only committed progress for attaching the unified Overlay to a daily session.

This is the first live attachment *data* adapter. It never calls Schwab,
starts workers, writes to SQLite, or equates a saved controller phase with a
verified running process. It reports only data committed and readable by this
SQLite observer. Real Time chart projection and operator commands are separate.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3

from mb_market_data.overlay_session_catalog import (
    DatedSession,
    SessionDiscoveryError,
    inspect_session,
)


@dataclass(frozen=True, slots=True)
class CommittedSessionProgress:
    """A read-only observation, NOT a full audit or liveness determination."""

    session_date: str
    observed_at_utc: str
    journal_path: str
    committed_acquisitions: int
    latest_acquisition_id: str | None
    latest_acquisition_completed_at_utc: str | None
    effective_membership_revisions: int
    latest_effective_revision: int | None
    new_acquisitions_since_previous_check: int | None
    controller_phase_saved: str | None
    controller_status_saved_at: str | None
    live_process_verified: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class ReadOnlySessionObserver:
    """Revalidate one exact daily session and inspect committed rows only."""

    def __init__(self, data_root: str | Path, session_date: str | date) -> None:
        self.data_root = Path(data_root).resolve()
        self.session_date = str(date.fromisoformat(str(session_date)))
        # Validate at construction and again each time data is read.
        self._session = inspect_session(self.data_root, self.session_date)
        self._previous_acquisition_count: int | None = None

    @staticmethod
    def _utc_text(at: datetime) -> str:
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Observer time must be timezone-aware")
        return at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def observe(self, *, now: datetime | None = None) -> CommittedSessionProgress:
        observed_at = now if now is not None else datetime.now(timezone.utc)
        cutoff = self._utc_text(observed_at)
        session: DatedSession = inspect_session(self.data_root, self.session_date)
        # Detect unexpected session substitution, even if paths otherwise parse.
        if session.plan_path != self._session.plan_path or session.journal_path != self._session.journal_path:
            raise SessionDiscoveryError("Read-only attachment session path changed")
        try:
            # mode=ro and query_only safeguard the production writer boundary.
            uri = Path(session.journal_path).resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                count = connection.execute(
                    "SELECT COUNT(*) FROM quote_acquisition WHERE session_date=?",
                    (self.session_date,),
                ).fetchone()[0]
                newest = connection.execute(
                    "SELECT acquisition_id, completed_at_utc, requested_symbol_count "
                    "FROM quote_acquisition WHERE session_date=? "
                    "ORDER BY completed_at_utc DESC, channel DESC, acquisition_id DESC LIMIT 1",
                    (self.session_date,),
                ).fetchone()
                if newest is not None:
                    # The writer commits an acquisition and its symbol outcomes
                    # atomically. Check the newest batch is readable together.
                    outcomes = connection.execute(
                        "SELECT COUNT(*) FROM quote_observation WHERE acquisition_id=?",
                        (newest[0],),
                    ).fetchone()[0]
                    if outcomes != newest[2]:
                        raise SessionDiscoveryError(
                            "Latest committed acquisition has incomplete quote outcomes"
                        )
                membership = connection.execute(
                    "SELECT COUNT(*), MAX(revision) FROM sampling_membership_revision "
                    "WHERE session_date=? AND effective_at_utc<=?",
                    (self.session_date, cutoff),
                ).fetchone()
                connection.rollback()
        except sqlite3.Error as error:
            raise SessionDiscoveryError(
                "Could not safely inspect committed daily journal data"
            ) from error

        previous = self._previous_acquisition_count
        if previous is not None and count < previous:
            raise SessionDiscoveryError(
                "Committed acquisition count decreased during read-only attachment"
            )
        self._previous_acquisition_count = count
        return CommittedSessionProgress(
            session_date=self.session_date,
            observed_at_utc=cutoff,
            journal_path=session.journal_path,
            committed_acquisitions=count,
            latest_acquisition_id=newest[0] if newest else None,
            latest_acquisition_completed_at_utc=newest[1] if newest else None,
            effective_membership_revisions=membership[0],
            latest_effective_revision=membership[1],
            new_acquisitions_since_previous_check=(
                count - previous if previous is not None else None
            ),
            controller_phase_saved=session.controller_phase_saved,
            controller_status_saved_at=session.status_saved_at,
        )
