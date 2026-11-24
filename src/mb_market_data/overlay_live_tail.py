"""Append committed events to the read-only Real Time Overlay.

Only the daily controller writes production. This adapter revalidates its plan,
observes committed SQLite transactions and projects complete new batches into
the same UI projector used by Historical replay. It does not run a poller,
modify a membership, infer missing slots, or certify process liveness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from mb_market_data.overlay_live_attachment import ReadOnlySessionObserver
from mb_market_data.overlay_indexed_selection import IndexedOverlaySymbolSelection
from mb_market_data.quote_dashboard_replay import QuoteDashboardReplayController
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    MembershipRevisionEvent,
    QuoteAcquisitionEvent,
    QuoteJournalReplayReader,
    ReplayEvent,
    ReplayIntegrityError,
)
from mb_market_data.quote_observation_store import StoredAcquisition


def _header_identity(header: ReplayEvent | StoredAcquisition) -> str:
    if isinstance(header, (MembershipRevisionEvent, ChannelRevisionEvent)):
        return header.event_id
    return f"quote_acquisition:{header.acquisition_id}"


def _header_timestamp(header: ReplayEvent | StoredAcquisition) -> datetime:
    if isinstance(header, (MembershipRevisionEvent, ChannelRevisionEvent)):
        return header.available_at_utc
    return header.completed_at_utc


def _header_key(header: ReplayEvent | StoredAcquisition) -> tuple:
    if isinstance(header, (MembershipRevisionEvent, ChannelRevisionEvent)):
        return (
            header.available_at_utc, 0, header.channel,
            header.revision.revision, header.event_id,
        )
    return (
        header.completed_at_utc, 1, header.channel, 0,
        header.acquisition_id,
    )


class ReadOnlyJournalTail:
    """Bridge a verified exact-day journal to a live-readonly replay controller."""

    def __init__(
        self,
        *,
        data_root: str | Path,
        reader: QuoteJournalReplayReader,
        controller: QuoteDashboardReplayController,
        indexed_selection: IndexedOverlaySymbolSelection | None = None,
    ) -> None:
        self._lock = RLock()
        self._reader = reader
        self._controller = controller
        self._index = indexed_selection
        self._observer = ReadOnlySessionObserver(data_root, reader.session_date)
        if Path(self._observer._session.journal_path).resolve() != reader.database_path.resolve():
            raise ReplayIntegrityError("Live observer journal does not match selected dashboard journal")
        self._committed_ids = set(controller.applied_event_ids())
        self._last_event_key: tuple | None = None

        # The projector has consumed its exact current-day causal prefix.
        headers = reader._ordered_headers()
        for header in headers:
            if _header_identity(header) in self._committed_ids:
                key = _header_key(header)
                if self._last_event_key is None or key > self._last_event_key:
                    self._last_event_key = key
        if len(self._committed_ids) != sum(
            _header_identity(header) in self._committed_ids for header in headers
        ):
            raise ReplayIntegrityError("Initial live projection events missing from validated journal")
        controller.activate_live_readonly()

    def refresh(self, *, now: datetime | None = None) -> tuple[int, str]:
        """Return newly applied event count and truthful attachment status text."""
        with self._lock:
            clock = now if now is not None else datetime.now(timezone.utc)
            if clock.tzinfo is None or clock.utcoffset() is None:
                raise ValueError("Live refresh clock must be timezone-aware")
            clock = clock.astimezone(timezone.utc)
            progress = self._observer.observe(now=clock)
            headers = self._reader._ordered_headers()
            candidates: list[tuple[tuple, ReplayEvent | StoredAcquisition]] = []
            for header in headers:
                if _header_identity(header) in self._committed_ids:
                    continue
                if _header_timestamp(header) > clock:
                    continue  # future-effective revision; do not leak early
                key = _header_key(header)
                if self._last_event_key is not None and key < self._last_event_key:
                    raise ReplayIntegrityError(
                        "A newly committed event precedes the displayed live causal prefix"
                    )
                candidates.append((key, header))
            candidates.sort(key=lambda item: item[0])
            next_events: list[ReplayEvent] = []
            for _, header in candidates:
                if isinstance(header, (MembershipRevisionEvent, ChannelRevisionEvent)):
                    next_events.append(header)
                else:
                    observations = self._reader.store.get_observations(header.acquisition_id)
                    next_events.append(QuoteAcquisitionEvent(header, observations))
            if next_events:
                self._controller.append_live_committed(tuple(next_events))
                self._committed_ids.update(event.event_id for event in next_events)
                self._last_event_key = candidates[-1][0]
                if self._index is not None:
                    self._index.invalidate()
            last = progress.latest_acquisition_completed_at_utc or "none"
            saved = progress.controller_phase_saved or "unavailable"
            status = (
                f"Committed journal · {progress.committed_acquisitions:,} acquisitions"
                f" · +{sum(isinstance(x, QuoteAcquisitionEvent) for x in next_events)} new"
                f" · last completion {last} UTC"
                f" · saved controller phase {saved} (liveness not verified)"
            )
            return len(next_events), status
