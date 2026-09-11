"""Deterministic event replay for a daily quote-observation journal.

Replay events use availability time rather than sampling time.  A channel
revision becomes available at its effective time; a quote acquisition becomes
available when the complete Schwab response has been durably assembled.  Each
quote event still retains its scheduled, dispatched, and completed timestamps.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias

from mb_market_data.quote_observation_store import (
    SCHEMA_VERSION,
    QuoteObservationStore,
    SamplingChannelRevision,
    StoredAcquisition,
    StoredQuoteObservation,
    UnsupportedSchemaVersionError,
)


class ReplayIntegrityError(RuntimeError):
    """The journal cannot produce a complete, internally consistent event."""


class ReplayEventKind(StrEnum):
    """Stable event types shared by replay and future live publication."""

    CHANNEL_REVISION = "channel_revision"
    QUOTE_ACQUISITION = "quote_acquisition"


@dataclass(frozen=True, slots=True)
class ChannelRevisionEvent:
    """A sampling-channel membership revision becoming effective."""

    revision: SamplingChannelRevision

    @property
    def kind(self) -> ReplayEventKind:
        return ReplayEventKind.CHANNEL_REVISION

    @property
    def available_at_utc(self) -> datetime:
        return self.revision.effective_at

    @property
    def channel(self) -> str:
        return self.revision.channel

    @property
    def event_id(self) -> str:
        return (
            f"channel_revision:{self.revision.channel}:"
            f"{self.revision.session_date.isoformat()}:"
            f"r{self.revision.revision}"
        )


@dataclass(frozen=True, slots=True)
class QuoteAcquisitionEvent:
    """One completed acquisition and all requested-symbol outcomes."""

    acquisition: StoredAcquisition
    observations: tuple[StoredQuoteObservation, ...]

    def __post_init__(self) -> None:
        if len(self.observations) != self.acquisition.requested_symbol_count:
            raise ReplayIntegrityError(
                f"{self.acquisition.acquisition_id} declares "
                f"{self.acquisition.requested_symbol_count} observations "
                f"but contains {len(self.observations)}"
            )
        if any(
            item.acquisition_id != self.acquisition.acquisition_id
            for item in self.observations
        ):
            raise ReplayIntegrityError(
                "Quote observation belongs to another acquisition: "
                f"{self.acquisition.acquisition_id}"
            )

    @property
    def kind(self) -> ReplayEventKind:
        return ReplayEventKind.QUOTE_ACQUISITION

    @property
    def available_at_utc(self) -> datetime:
        return self.acquisition.completed_at_utc

    @property
    def channel(self) -> str:
        return self.acquisition.channel

    @property
    def event_id(self) -> str:
        return f"quote_acquisition:{self.acquisition.acquisition_id}"


ReplayEvent: TypeAlias = ChannelRevisionEvent | QuoteAcquisitionEvent


def _journal_session_date(database_path: Path) -> date:
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Unsupported quote-observation schema version: {version}"
            )
        try:
            row = connection.execute(
                "SELECT schema_version, session_date FROM store_identity "
                "WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReplayIntegrityError(
                "Journal has no readable store_identity table"
            ) from exc
        if row is None:
            raise ReplayIntegrityError("Journal has no store_identity row")
        if row[0] != version:
            raise ReplayIntegrityError(
                "store_identity schema version differs from PRAGMA "
                "user_version"
            )
        return date.fromisoformat(row[1])
    finally:
        connection.close()


def _event_sort_key(event: ReplayEvent) -> tuple[datetime, int, str, str]:
    # Membership must win a same-timestamp tie so consumers know the active
    # channel state before receiving an acquisition.
    kind_order = 0 if isinstance(event, ChannelRevisionEvent) else 1
    return (
        event.available_at_utc,
        kind_order,
        event.channel,
        event.event_id,
    )


class QuoteJournalReplayReader:
    """Read complete events from one daily journal in causal order."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.session_date = _journal_session_date(self.database_path)
        self.store = QuoteObservationStore(
            self.database_path,
            session_date=self.session_date,
        )

    def events(self) -> Iterator[ReplayEvent]:
        """Yield immutable events ordered by when they became available."""

        header_payloads: list[ReplayEvent | StoredAcquisition] = []
        for revision in self.store.channel_revisions_in_effective_order():
            header_payloads.append(ChannelRevisionEvent(revision))
        header_payloads.extend(self.store.acquisitions_in_replay_order())

        def payload_sort_key(
            payload: ReplayEvent | StoredAcquisition,
        ) -> tuple[datetime, int, str, str]:
            if isinstance(payload, ChannelRevisionEvent):
                return _event_sort_key(payload)
            return (
                payload.completed_at_utc,
                1,
                payload.channel,
                payload.acquisition_id,
            )

        header_payloads.sort(key=payload_sort_key)

        for payload in header_payloads:
            if isinstance(payload, ChannelRevisionEvent):
                yield payload
                continue

            observations = self.store.get_observations(
                payload.acquisition_id
            )
            yield QuoteAcquisitionEvent(
                acquisition=payload,
                observations=observations,
            )


def paced_replay(
    events: Iterable[ReplayEvent],
    *,
    speed: float | None = None,
    step_waiter: Callable[[ReplayEvent], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[ReplayEvent]:
    """Yield events immediately, at scaled historical time, or stepwise.

    ``speed=None`` performs immediate replay.  A speed of 1.0 is real time;
    60.0 maps one historical minute to one wall-clock second.  ``step_waiter``
    is called before every event and is mutually exclusive with a speed.
    Absolute deadlines avoid accumulating consumer and sleep overhead.
    """

    if speed is not None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise TypeError("speed must be a number or None")
        if speed <= 0:
            raise ValueError("speed must be greater than zero")
    if speed is not None and step_waiter is not None:
        raise ValueError("speed and step_waiter are mutually exclusive")

    first_event_time: datetime | None = None
    replay_started_at: float | None = None
    previous_event_time: datetime | None = None

    for event in events:
        if (
            previous_event_time is not None
            and event.available_at_utc < previous_event_time
        ):
            raise ValueError("Replay events are not in availability order")

        if step_waiter is not None:
            step_waiter(event)
        elif speed is not None:
            if first_event_time is None:
                first_event_time = event.available_at_utc
                replay_started_at = monotonic()
            assert replay_started_at is not None
            target_elapsed = (
                event.available_at_utc - first_event_time
            ).total_seconds() / speed
            remaining = replay_started_at + target_elapsed - monotonic()
            if remaining > 0:
                sleeper(remaining)

        yield event
        previous_event_time = event.available_at_utc
