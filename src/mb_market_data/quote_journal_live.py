"""Read-only, bounded discovery of appended schema-v2 journal events.

SQLite insertion cursors are transport positions, never market timestamps.
Callers order discovered headers with ``header_sort_key`` and reconstruct
projection when a delayed commit belongs before already applied events.
The journal must remain append-only: no replacement, deletion or VACUUM while
following it. A fresh reader starts at zero and rebuilds from durable data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from mb_market_data.quote_journal_replay import (
    MembershipRevisionEvent,
    QuoteAcquisitionEvent,
    ReplayEvent,
    ReplayIntegrityError,
    _journal_identity,
)
from mb_market_data.quote_observation_store import (
    HIERARCHY_SCHEMA_VERSION,
    QuoteObservationStore,
    QuoteObservationStoreError,
    StoredAcquisition,
)

JournalHeader: TypeAlias = MembershipRevisionEvent | StoredAcquisition


def header_event_id(header: JournalHeader) -> str:
    if isinstance(header, MembershipRevisionEvent):
        return header.event_id
    return f"quote_acquisition:{header.acquisition_id}"


def header_sort_key(header: JournalHeader) -> tuple[datetime, int, str, int, str]:
    """Use exactly the existing replay availability and tie-breaking order."""

    if isinstance(header, MembershipRevisionEvent):
        return (
            header.available_at_utc, 0, header.channel,
            header.revision.revision, header.event_id,
        )
    return (
        header.completed_at_utc, 1, header.channel, 0, header.acquisition_id,
    )


@dataclass(frozen=True, slots=True)
class LiveJournalCursor:
    """In-memory append positions; unusable without the corresponding history."""

    membership_rowid: int = 0
    acquisition_rowid: int = 0
    membership_anchor: tuple[str, int, str] | None = None
    acquisition_anchor: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class LiveJournalPage:
    headers: tuple[JournalHeader, ...]
    cursor: LiveJournalCursor
    caught_up: bool


class QuoteJournalLiveReader:
    """Discover committed headers and load complete events in short snapshots."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        identity = _journal_identity(self.database_path)
        if identity.schema_version != HIERARCHY_SCHEMA_VERSION:
            raise ReplayIntegrityError("Live Overlay requires a schema-v2 journal")
        self.session_date = identity.session_date
        self._store = QuoteObservationStore(
            self.database_path, session_date=self.session_date,
            schema_version=HIERARCHY_SCHEMA_VERSION,
        )
        with self._connection(check_identity=False) as connection:
            self._identity = self._identity_row(connection)

    @staticmethod
    def _identity_row(connection: sqlite3.Connection) -> tuple:
        row = connection.execute(
            "SELECT schema_version, session_date, created_at_utc, "
            "membership_contract FROM store_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ReplayIntegrityError("Live journal identity is missing")
        return tuple(row)

    @contextmanager
    def _connection(self, *, check_identity: bool = True) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path.as_uri() + "?mode=ro", uri=True, timeout=0.25,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            if check_identity and self._identity_row(connection) != self._identity:
                raise ReplayIntegrityError("Live journal identity changed; reopen the viewer")
            yield connection
        finally:
            connection.close()

    def read_page(
        self, cursor: LiveJournalCursor, *, limit: int = 32,
    ) -> LiveJournalPage:
        """Read at most ``limit`` headers per table in one committed snapshot.

        A caller accepts ``page.cursor`` only after accepting all its headers.
        No event is filtered out because its availability time is old or future.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if cursor.membership_rowid < 0 or cursor.acquisition_rowid < 0:
            raise ValueError("cursor positions must be nonnegative")
        with self._connection() as connection:
            for table, position, anchor, columns in (
                ("sampling_membership_revision", cursor.membership_rowid,
                 cursor.membership_anchor, "session_date, revision, content_sha256"),
                ("quote_acquisition", cursor.acquisition_rowid,
                 cursor.acquisition_anchor, "acquisition_id, content_sha256"),
            ):
                if position:
                    row = connection.execute(
                        f"SELECT {columns} FROM {table} WHERE rowid = ?", (position,),
                    ).fetchone()
                    if row is None or anchor is None or tuple(row) != anchor:
                        raise ReplayIntegrityError("Live journal cursor anchor changed")

            revisions = connection.execute(
                "SELECT rowid AS append_rowid, * FROM sampling_membership_revision "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (cursor.membership_rowid, limit),
            ).fetchall()
            acquisitions = connection.execute(
                "SELECT rowid AS append_rowid, * FROM quote_acquisition "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (cursor.acquisition_rowid, limit),
            ).fetchall()
            try:
                headers: list[JournalHeader] = [
                    MembershipRevisionEvent(
                        self._store._membership_revision_from_row(connection, row)
                    ) for row in revisions
                ]
            except QuoteObservationStoreError as exc:
                raise ReplayIntegrityError("Live hierarchy is inconsistent") from exc
            headers.extend(self._store._stored_acquisition(row) for row in acquisitions)
            next_cursor = LiveJournalCursor(
                membership_rowid=(revisions[-1]["append_rowid"] if revisions
                                  else cursor.membership_rowid),
                acquisition_rowid=(acquisitions[-1]["append_rowid"] if acquisitions
                                   else cursor.acquisition_rowid),
                membership_anchor=(
                    (revisions[-1]["session_date"], revisions[-1]["revision"],
                     revisions[-1]["content_sha256"]) if revisions else cursor.membership_anchor
                ),
                acquisition_anchor=(
                    (acquisitions[-1]["acquisition_id"], acquisitions[-1]["content_sha256"])
                    if acquisitions else cursor.acquisition_anchor
                ),
            )
            caught_up = not any(
                connection.execute(
                    f"SELECT 1 FROM {table} WHERE rowid > ? LIMIT 1", (position,),
                ).fetchone()
                for table, position in (
                    ("sampling_membership_revision", next_cursor.membership_rowid),
                    ("quote_acquisition", next_cursor.acquisition_rowid),
                )
            )
        return LiveJournalPage(tuple(headers), next_cursor, caught_up)

    def load_events(self, headers: Sequence[JournalHeader]) -> tuple[ReplayEvent, ...]:
        """Load a caller-bounded batch; keep no observation history in the reader."""

        events: list[ReplayEvent] = []
        with self._connection() as connection:
            for header in headers:
                if isinstance(header, MembershipRevisionEvent):
                    events.append(header)
                    continue
                row = connection.execute(
                    "SELECT * FROM quote_acquisition WHERE acquisition_id = ?",
                    (header.acquisition_id,),
                ).fetchone()
                if row is None or self._store._stored_acquisition(row) != header:
                    raise ReplayIntegrityError("Discovered acquisition changed or disappeared")
                rows = connection.execute(
                    "SELECT observation.*, acquisition.channel, acquisition.channel_revision, "
                    "acquisition.scheduled_at_utc FROM quote_observation AS observation "
                    "JOIN quote_acquisition AS acquisition USING(acquisition_id) "
                    "WHERE observation.acquisition_id = ? ORDER BY observation.ordinal",
                    (header.acquisition_id,),
                ).fetchall()
                events.append(QuoteAcquisitionEvent(
                    header, tuple(self._store._stored_observation(row) for row in rows),
                ))
        return tuple(events)
