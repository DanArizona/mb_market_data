"""In-memory state projected from live or replayed quote events.

The projector contains no SQLite or dashboard dependency.  It consumes the
same normalized event contract in either mode and maintains a deterministic
view of current channel membership plus the latest outcome observed for each
``(channel, symbol)`` pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping

from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    QuoteAcquisitionEvent,
    ReplayEvent,
)
from mb_market_data.quote_observation_store import (
    SamplingChannelRevision,
    StoredAcquisition,
    StoredQuoteObservation,
)
from mb_market_data.schwab_quotes import normalize_symbols


class StateProjectionError(RuntimeError):
    """An event cannot be applied without making state inconsistent."""


@dataclass(frozen=True, slots=True)
class ProjectedQuoteObservation:
    """Latest known outcome and the acquisition that made it available."""

    acquisition: StoredAcquisition
    observation: StoredQuoteObservation

    @property
    def available_at_utc(self) -> datetime:
        return self.acquisition.completed_at_utc


@dataclass(frozen=True, slots=True)
class ChannelMemberState:
    """One current channel member in coordinator-defined order."""

    channel: str
    revision: int
    ordinal: int
    symbol: str
    latest: ProjectedQuoteObservation | None


@dataclass(frozen=True, slots=True)
class QuoteEventStateSnapshot:
    """Immutable point-in-time view suitable for dashboard consumers."""

    session_date: date | None
    current_time_utc: datetime | None
    event_count: int
    revision_count: int
    acquisition_count: int
    observation_count: int
    channel_revisions: Mapping[str, SamplingChannelRevision]
    latest_observations: Mapping[
        tuple[str, str], ProjectedQuoteObservation
    ]

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(sorted(self.channel_revisions))

    def current_members(self, channel: str) -> tuple[str, ...]:
        revision = self.channel_revisions.get(_channel_key(channel))
        return revision.symbols if revision is not None else ()

    def latest(
        self,
        channel: str,
        symbol: str,
    ) -> ProjectedQuoteObservation | None:
        normalized_symbols = normalize_symbols((symbol,))
        if not normalized_symbols:
            return None
        return self.latest_observations.get(
            (_channel_key(channel), normalized_symbols[0])
        )

    def memberships_for(self, symbol: str) -> tuple[str, ...]:
        normalized_symbols = normalize_symbols((symbol,))
        if not normalized_symbols:
            return ()
        normalized = normalized_symbols[0]
        return tuple(
            channel
            for channel in self.channels
            if normalized in self.channel_revisions[channel].symbols
        )

    def channel_rows(self, channel: str) -> tuple[ChannelMemberState, ...]:
        normalized = _channel_key(channel)
        revision = self.channel_revisions.get(normalized)
        if revision is None:
            return ()
        return tuple(
            ChannelMemberState(
                channel=normalized,
                revision=revision.revision,
                ordinal=ordinal,
                symbol=symbol,
                latest=self.latest_observations.get((normalized, symbol)),
            )
            for ordinal, symbol in enumerate(revision.symbols)
        )


def _channel_key(channel: str) -> str:
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("channel must be a nonblank string")
    return channel.strip().casefold()


def _event_session_date(event: ReplayEvent) -> date:
    if isinstance(event, ChannelRevisionEvent):
        return event.revision.session_date
    return event.acquisition.session_date


class QuoteEventStateProjector:
    """Apply normalized events to deterministic, queryable current state."""

    def __init__(self, *, session_date: date | None = None) -> None:
        self._session_date = session_date
        self._current_time_utc: datetime | None = None
        # Keep compact immutable headers for redelivery detection. Retaining
        # every full acquisition event would also retain every observation in
        # memory, defeating the projector's bounded latest-state design.
        self._seen_event_headers: dict[
            str, SamplingChannelRevision | StoredAcquisition
        ] = {}
        self._revisions: dict[
            tuple[str, int], SamplingChannelRevision
        ] = {}
        self._current_revisions: dict[str, SamplingChannelRevision] = {}
        self._latest: dict[
            tuple[str, str], ProjectedQuoteObservation
        ] = {}
        self._revision_count = 0
        self._acquisition_count = 0
        self._observation_count = 0

    def apply(self, event: ReplayEvent) -> bool:
        """Apply one event; return false only for an identical redelivery."""

        event_header = (
            event.revision
            if isinstance(event, ChannelRevisionEvent)
            else event.acquisition
        )
        existing = self._seen_event_headers.get(event.event_id)
        if existing is not None:
            if existing != event_header:
                raise StateProjectionError(
                    f"Event ID has conflicting content: {event.event_id}"
                )
            return False

        if (
            self._current_time_utc is not None
            and event.available_at_utc < self._current_time_utc
        ):
            raise StateProjectionError(
                "Event precedes current projected time: "
                f"{event.event_id}"
            )

        event_session_date = _event_session_date(event)
        if self._session_date is None:
            self._session_date = event_session_date
        elif event_session_date != self._session_date:
            raise StateProjectionError(
                f"Event belongs to {event_session_date}, not "
                f"{self._session_date}: {event.event_id}"
            )

        if isinstance(event, ChannelRevisionEvent):
            self._apply_revision(event)
            self._revision_count += 1
        else:
            self._apply_acquisition(event)
            self._acquisition_count += 1
            self._observation_count += len(event.observations)

        self._seen_event_headers[event.event_id] = event_header
        self._current_time_utc = event.available_at_utc
        return True

    def _apply_revision(self, event: ChannelRevisionEvent) -> None:
        revision = event.revision
        channel = _channel_key(revision.channel)
        current = self._current_revisions.get(channel)
        if current is not None and revision.revision <= current.revision:
            raise StateProjectionError(
                "Channel revision did not increase: "
                f"{channel} r{revision.revision} after r{current.revision}"
            )

        key = (channel, revision.revision)
        if key in self._revisions:
            raise StateProjectionError(
                f"Channel revision was already applied: "
                f"{channel} r{revision.revision}"
            )
        self._revisions[key] = revision
        self._current_revisions[channel] = revision

    def _apply_acquisition(self, event: QuoteAcquisitionEvent) -> None:
        acquisition = event.acquisition
        channel = _channel_key(acquisition.channel)
        revision = self._revisions.get(
            (channel, acquisition.channel_revision)
        )
        if revision is None:
            raise StateProjectionError(
                "Acquisition refers to an unapplied channel revision: "
                f"{channel} r{acquisition.channel_revision}"
            )

        symbols = tuple(item.symbol for item in event.observations)
        if symbols != revision.symbols:
            raise StateProjectionError(
                "Acquisition symbols differ from its channel revision: "
                f"{acquisition.acquisition_id}"
            )
        if tuple(item.ordinal for item in event.observations) != tuple(
            range(len(event.observations))
        ):
            raise StateProjectionError(
                "Acquisition observation ordinals are not contiguous: "
                f"{acquisition.acquisition_id}"
            )
        if any(
            _channel_key(item.channel) != channel
            or item.channel_revision != acquisition.channel_revision
            or item.scheduled_at_utc != acquisition.scheduled_at_utc
            for item in event.observations
        ):
            raise StateProjectionError(
                "Observation provenance differs from its acquisition: "
                f"{acquisition.acquisition_id}"
            )

        projected = tuple(
            (
                (channel, observation.symbol),
                ProjectedQuoteObservation(acquisition, observation),
            )
            for observation in event.observations
        )
        for key, observation in projected:
            self._latest[key] = observation

    def snapshot(self) -> QuoteEventStateSnapshot:
        """Return an immutable copy isolated from subsequent events."""

        return QuoteEventStateSnapshot(
            session_date=self._session_date,
            current_time_utc=self._current_time_utc,
            event_count=len(self._seen_event_headers),
            revision_count=self._revision_count,
            acquisition_count=self._acquisition_count,
            observation_count=self._observation_count,
            channel_revisions=MappingProxyType(
                dict(self._current_revisions)
            ),
            latest_observations=MappingProxyType(dict(self._latest)),
        )
