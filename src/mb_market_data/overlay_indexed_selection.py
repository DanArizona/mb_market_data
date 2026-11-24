"""Indexed, read-only selected-symbol projection for the historical Overlay.

Full journal replay remains the authority for acquisition cardinality, audits,
and deterministic recovery. This supplementary view reads only the indexed
symbol's observations plus acquisition/membership *headers* and never edits
the journal. Keep the selected symbol's replay prefix identical to the main
controller, including partially stepped equal-timestamp events.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from mb_market_data.observation_overlay import ObservationOverlayProjector
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    MembershipRevisionEvent,
    QuoteAcquisitionEvent,
    QuoteJournalReplayReader,
    ReplayEvent,
    ReplayIntegrityError,
)
from mb_market_data.quote_observation_store import (
    HIERARCHY_SCHEMA_VERSION,
    StoredAcquisition,
    StoredQuoteObservation,
)

MAX_SELECTED_ROWS = 100_000


def _order_key(event: ReplayEvent) -> tuple:
    """Match the full reader's original same-time ordering."""
    if isinstance(event, (MembershipRevisionEvent, ChannelRevisionEvent)):
        return (
            event.available_at_utc,
            0,
            event.channel,
            event.revision.revision,
            event.event_id,
        )
    return (
        event.available_at_utc,
        1,
        event.channel,
        0,
        event.acquisition.acquisition_id,
    )


class IndexedOverlaySymbolSelection:
    """Lazy reusable acquisition-header map; indexed symbol rows per selection."""

    def __init__(
        self,
        reader: QuoteJournalReplayReader,
        projector_factory: Callable[[str], ObservationOverlayProjector],
    ) -> None:
        if reader.schema_version != HIERARCHY_SCHEMA_VERSION:
            raise ValueError("Indexed Overlay requires a schema-v2 journal")
        self.reader = reader
        self.projector_factory = projector_factory
        self._membership_events: tuple[MembershipRevisionEvent, ...] | None = None
        self._acquisitions: dict[str, StoredAcquisition] | None = None

    def _headers(self) -> tuple[tuple[MembershipRevisionEvent, ...], dict[str, StoredAcquisition]]:
        if self._membership_events is None or self._acquisitions is None:
            # Full headers/revisions are much smaller than all 700k+ symbol
            # observations. These are loaded once, and retain validation.
            self._membership_events = tuple(
                MembershipRevisionEvent(item)
                for item in self.reader.store.membership_revisions_in_effective_order()
            )
            self._acquisitions = {
                item.acquisition_id: item
                for item in self.reader.store.acquisitions_in_replay_order()
            }
        return self._membership_events, self._acquisitions

    def __call__(
        self,
        symbol: str,
        cutoff: datetime | None,
        pending: ReplayEvent | None,
    ) -> ObservationOverlayProjector:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Indexed symbol must be nonblank")
        selected = symbol.strip().upper()
        projector = self.projector_factory(selected)
        if projector.symbol != selected or projector.session_date != self.reader.session_date:
            raise ReplayIntegrityError("Indexed Overlay projector identity mismatch")
        if cutoff is None:
            return projector
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("Indexed Overlay cutoff must be timezone-aware")
        cutoff = cutoff.astimezone(timezone.utc)
        if cutoff.astimezone(projector.cache.request_start_et.tzinfo if projector.cache
                                 else __import__("zoneinfo").ZoneInfo("America/New_York")).date() != self.reader.session_date:
            raise ValueError("Indexed Overlay cutoff belongs to another session")

        revisions, acquisitions = self._headers()
        observations = self.reader.store.recent_observations(selected, limit=MAX_SELECTED_ROWS)
        if len(observations) >= MAX_SELECTED_ROWS:
            raise ReplayIntegrityError("Indexed selection limit reached; refusing truncated symbol history")

        # Preserve the exact applied main replay event prefix. Timestamp alone
        # can otherwise include an unprocessed event tied with the last Step.
        next_key = _order_key(pending) if pending is not None else None
        combined: list[tuple[tuple, MembershipRevisionEvent | None, StoredAcquisition | None, StoredQuoteObservation | None]] = []
        for event in revisions:
            key = _order_key(event)
            if key[0] <= cutoff and (next_key is None or key < next_key):
                combined.append((key, event, None, None))
        for observation in observations:
            acquisition = acquisitions.get(observation.acquisition_id)
            if acquisition is None:
                raise ReplayIntegrityError("Indexed observation has no acquisition header")
            key = (
                acquisition.completed_at_utc,
                1,
                acquisition.channel,
                0,
                acquisition.acquisition_id,
            )
            if key[0] <= cutoff and (next_key is None or key < next_key):
                combined.append((key, None, acquisition, observation))
        combined.sort(key=lambda entry: entry[0])
        for _, revision, acquisition, observation in combined:
            if revision is not None:
                projector.apply(revision)
            else:
                assert acquisition is not None and observation is not None
                projector.apply_selected_observation(acquisition, observation)
        return projector
