"""Application boundary between scheduled polling and the daily quote journal.

The polling scheduler owns slots and immutable membership snapshots.  The
SQLite store owns durable replay records.  This module joins those two small
interfaces without giving either one responsibility for Schwab authentication,
process scheduling, or probe evidence files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from mb_market_data.quote_observation_store import (
    HIERARCHY_SCHEMA_VERSION,
    PollRunProvenance,
    QuoteObservationStore,
    RecordResult,
    SamplingChannelRevision,
)
from mb_market_data.schwab_quotes import QuoteBatchResult
from mb_market_data.watchlist_polling import (
    PollRequest,
    SkippedPollSlot,
    WatchlistKind,
    WatchlistSnapshot,
)


def daily_quote_journal_path(
    journal_root: str | Path,
    session_date: date,
) -> Path:
    """Return the canonical SQLite path for one trading session."""

    return Path(journal_root) / f"{session_date.isoformat()}.sqlite3"


@dataclass(frozen=True, slots=True)
class PollingJournalSession:
    """One polling process registered with a shared daily journal."""

    store: QuoteObservationStore
    run_id: str
    run_record_result: RecordResult
    revision_record_result: RecordResult | None

    @classmethod
    def start(
        cls,
        database_path: str | Path,
        *,
        run_id: str,
        started_at: datetime,
        software_version: str,
        configuration: Mapping[str, Any],
        snapshot: WatchlistSnapshot,
        revision_source: str,
        revision_reason: str | None = None,
        revision_metadata: Mapping[str, Any] | None = None,
        host: str | None = None,
        command: tuple[str, ...] = (),
        busy_timeout_ms: int = 5_000,
    ) -> PollingJournalSession:
        """Open the daily store and register this run and its membership."""

        store = QuoteObservationStore(
            database_path,
            session_date=snapshot.session_date,
            busy_timeout_ms=busy_timeout_ms,
        )
        store.initialize()
        run_result = store.record_run(
            PollRunProvenance(
                run_id=run_id,
                started_at=started_at,
                software_version=software_version,
                configuration=configuration,
                host=host,
                command=command,
            )
        )
        revision_result = store.record_channel_revision(
            SamplingChannelRevision(
                channel=snapshot.watchlist_kind.value,
                session_date=snapshot.session_date,
                revision=snapshot.revision,
                effective_at=snapshot.effective_at,
                symbols=snapshot.symbols,
                source=revision_source,
                reason=revision_reason,
                metadata=(
                    dict(revision_metadata)
                    if revision_metadata is not None
                    else {}
                ),
            )
        )
        return cls(
            store=store,
            run_id=run_id,
            run_record_result=run_result,
            revision_record_result=revision_result,
        )

    @classmethod
    def register_hierarchy_polling(
        cls,
        database_path: str | Path,
        *,
        session_date: date,
        run_id: str,
        started_at: datetime,
        software_version: str,
        configuration: Mapping[str, Any],
        host: str | None = None,
        command: tuple[str, ...] = (),
        busy_timeout_ms: int = 5_000,
    ) -> PollingJournalSession:
        """Register a schema-v2 poller without publishing membership.

        Hierarchy publication belongs to the coordinator-side publisher.
        A poller only records its run, resolves effective membership, and
        writes acquisitions bound to revisions already present in the store.
        """

        store = QuoteObservationStore(
            database_path,
            session_date=session_date,
            schema_version=HIERARCHY_SCHEMA_VERSION,
            busy_timeout_ms=busy_timeout_ms,
        )
        store.initialize()
        run_result = store.record_run(
            PollRunProvenance(
                run_id=run_id,
                started_at=started_at,
                software_version=software_version,
                configuration=configuration,
                host=host,
                command=command,
            )
        )
        return cls(
            store=store,
            run_id=run_id,
            run_record_result=run_result,
            revision_record_result=None,
        )

    @property
    def database_path(self) -> Path:
        return self.store.database_path

    def latest_effective_snapshot(
        self,
        watchlist_kind: WatchlistKind,
        *,
        at: datetime,
    ) -> WatchlistSnapshot | None:
        """Resolve one channel from the hierarchy effective at ``at``."""

        revision = self.store.latest_membership_revision_effective_at(at)
        if revision is None:
            return None
        return WatchlistSnapshot(
            watchlist_kind=watchlist_kind,
            session_date=revision.session_date,
            revision=revision.revision,
            effective_at=revision.effective_at,
            symbols=revision.symbols_for(watchlist_kind.value),
        )

    def record_acquisition(
        self,
        request: PollRequest,
        result: QuoteBatchResult,
        *,
        completed_at: datetime,
    ) -> RecordResult:
        """Atomically append one completed scheduled acquisition."""

        return self.store.record_acquisition(
            self.run_id,
            request,
            result,
            completed_at=completed_at,
        )

    def record_skipped_slot(
        self,
        skipped: SkippedPollSlot,
    ) -> RecordResult:
        """Atomically record one due slot skipped for empty membership."""

        return self.store.record_skipped_slot(self.run_id, skipped)
