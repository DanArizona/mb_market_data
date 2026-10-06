"""Experimental live quotes/membership controller using the replay projectors."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from mb_market_data.observation_overlay import (
    ObservationOverlayError, ObservationOverlayProjector,
)
from mb_market_data.quote_dashboard_replay import (
    DashboardReplaySnapshot, DashboardReplayStatus,
)
from mb_market_data.quote_event_state import (
    QuoteEventStateProjector, StateProjectionError,
)
from mb_market_data.quote_journal_live import (
    JournalHeader, LiveJournalCursor, QuoteJournalLiveReader,
    header_event_id, header_sort_key,
)
from mb_market_data.quote_journal_replay import ReplayIntegrityError

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


@dataclass
class _Rebuild:
    headers: tuple[JournalHeader, ...]
    target: datetime
    state: QuoteEventStateProjector
    overlay: ObservationOverlayProjector
    index: int = 0


class QuoteDashboardLiveController:
    """Follow a growing journal; pause/seek affect display, never acquisition.

    Only immutable headers are retained for ordering. Full quote observations
    are loaded in bounded batches, and the existing latest-state and selected-
    symbol projectors retain their usual compact projections. Reordering is
    rebuilt in batches while the last coherent snapshot remains displayed.
    """

    def __init__(
        self, *, reader: QuoteJournalLiveReader, symbol: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        batch_limit: int = 32,
    ) -> None:
        if isinstance(batch_limit, bool) or not isinstance(batch_limit, int) or batch_limit <= 0:
            raise ValueError("batch_limit must be a positive integer")
        self.reader = reader
        self.symbol = symbol.strip().upper()
        if not self.symbol:
            raise ValueError("symbol must not be blank")
        self._now = now
        self._batch_limit = batch_limit
        self._lock = threading.RLock()
        self._start = datetime.combine(reader.session_date, time.min, tzinfo=ET).astimezone(UTC)
        self._end = datetime.combine(
            reader.session_date + timedelta(days=1), time.min, tzinfo=ET,
        ).astimezone(UTC) - timedelta(microseconds=1)
        self._cursor = LiveJournalCursor()
        self._known: dict[str, JournalHeader] = {}
        self._headers: list[JournalHeader] = []
        self._state = QuoteEventStateProjector(session_date=reader.session_date)
        self._overlay = self._new_overlay()
        self._applied_index = 0
        self._position = self._clock()
        self._target = self._position
        self._following = True
        self._discovery_complete = False
        self._needs_rebuild = False
        self._rebuild: _Rebuild | None = None
        self._source_error: str | None = None
        self.tick()

    def _new_overlay(self) -> ObservationOverlayProjector:
        return ObservationOverlayProjector(
            symbol=self.symbol, session_date=self.reader.session_date,
        )

    def _clock(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("live clock must be timezone-aware")
        return min(max(value.astimezone(UTC), self._start), self._end)

    def _discover(self) -> None:
        page = self.reader.read_page(self._cursor, limit=self._batch_limit)
        old_prefix = tuple(header_event_id(h) for h in self._headers[:self._applied_index])
        for header in page.headers:
            event_id = header_event_id(header)
            previous = self._known.get(event_id)
            if previous is not None and previous != header:
                raise ReplayIntegrityError("Stable live event ID was reused with different content")
        self._known.update((header_event_id(h), h) for h in page.headers)
        self._headers = sorted(self._known.values(), key=header_sort_key)
        if old_prefix != tuple(header_event_id(h) for h in self._headers[:self._applied_index]):
            self._needs_rebuild = True
        self._cursor = page.cursor
        self._discovery_complete = page.caught_up

    def _visible_headers(self, target: datetime) -> tuple[JournalHeader, ...]:
        return tuple(h for h in self._headers if header_sort_key(h)[0] <= target)

    def _advance(self) -> None:
        if not self._discovery_complete:
            return
        if self._needs_rebuild or self._rebuild is not None:
            if self._rebuild is None:
                self._rebuild = _Rebuild(
                    self._visible_headers(self._target), self._target,
                    QuoteEventStateProjector(session_date=self.reader.session_date),
                    self._new_overlay(),
                )
            job = self._rebuild
            batch = job.headers[job.index:job.index + self._batch_limit]
            for event in self.reader.load_events(batch):
                job.state.apply(event)
                job.overlay.apply(event)
                job.index += 1
            if job.index == len(job.headers):
                # A later discovery can insert before the frozen rebuild's
                # frontier. Publish only if the rebuilt prefix is still valid.
                prefix = tuple(header_event_id(h) for h in self._headers[:job.index])
                if prefix == tuple(header_event_id(h) for h in job.headers):
                    self._state, self._overlay = job.state, job.overlay
                    self._applied_index = job.index
                    self._position = job.target
                    self._needs_rebuild = False
                else:
                    self._needs_rebuild = True
                self._rebuild = None
            return

        batch = tuple(
            h for h in self._headers[self._applied_index:]
            if header_sort_key(h)[0] <= self._target
        )[:self._batch_limit]
        state, overlay = self._state.fork(), self._overlay.fork()
        for event in self.reader.load_events(batch):
            state.apply(event)
            overlay.apply(event)
        self._state, self._overlay = state, overlay
        self._applied_index += len(batch)
        if self._applied_index < len(self._headers) and (
            header_sort_key(self._headers[self._applied_index])[0] <= self._target
        ):
            self._position = header_sort_key(self._headers[self._applied_index - 1])[0]
        else:
            self._position = self._target

    def snapshot(self) -> DashboardReplaySnapshot:
        with self._lock:
            pending_visible = self._applied_index < len(self._headers) and (
                header_sort_key(self._headers[self._applied_index])[0] <= self._target
            )
            return DashboardReplaySnapshot(
                state=self._state.snapshot(),
                status=DashboardReplayStatus.LIVE if self._following else DashboardReplayStatus.PAUSED,
                speed=1.0,
                replay_time_utc=self._position,
                first_event_time_utc=(header_sort_key(self._headers[0])[0] if self._headers else None),
                last_event_time_utc=(header_sort_key(self._headers[-1])[0] if self._headers else None),
                total_event_count=len(self._headers),
                observation_overlay=self._overlay.snapshot(self._position),
                live_source=True,
                catching_up=(not self._discovery_complete or self._needs_rebuild
                             or self._rebuild is not None or pending_visible),
                source_error=self._source_error,
            )

    def tick(self) -> DashboardReplaySnapshot:
        with self._lock:
            if self._following:
                self._target = self._clock()
            try:
                self._discover()
                self._advance()
            except (sqlite3.Error, ReplayIntegrityError, ObservationOverlayError,
                    StateProjectionError, ValueError, TypeError) as exc:
                self._source_error = f"{type(exc).__name__}: {exc}"
                self._needs_rebuild = True
                self._rebuild = None
            else:
                self._source_error = None
            return self.snapshot()

    def pause(self) -> DashboardReplaySnapshot:
        with self._lock:
            self._following = False
            self._target = self._position
            if self._rebuild is not None:
                self._rebuild = None
                self._needs_rebuild = True
            return self.snapshot()

    def play(self) -> DashboardReplaySnapshot:
        """Return to Live and catch up, irrespective of historical position."""
        with self._lock:
            self._following = True
            return self.tick()

    def toggle(self) -> DashboardReplaySnapshot:
        with self._lock:
            return self.pause() if self._following else self.play()

    def seek(self, target_utc: datetime) -> DashboardReplaySnapshot:
        if not isinstance(target_utc, datetime):
            raise TypeError("target_utc must be a datetime")
        if target_utc.tzinfo is None or target_utc.utcoffset() is None:
            raise ValueError("target_utc must be timezone-aware")
        target = target_utc.astimezone(UTC)
        if target.astimezone(ET).date() != self.reader.session_date:
            raise ValueError("target_utc must fall on the journal session date in ET")
        if target > self._clock():
            raise ValueError("Cannot inspect a future live time")
        with self._lock:
            self._following = False
            self._target = target
            self._needs_rebuild = True
            self._rebuild = None
            return self.tick()

    def restart(self) -> DashboardReplaySnapshot:
        return self.seek(self._start)

    def step(self) -> DashboardReplaySnapshot:
        with self._lock:
            self.pause()
            targets = [header_sort_key(h)[0] for h in self._headers
                       if self._position < header_sort_key(h)[0] <= self._clock()]
            return self.seek(min(targets)) if targets else self.snapshot()

    def set_speed(self, speed: float) -> DashboardReplaySnapshot:
        # Shared dashboard protocol; the live source uses the wall clock.
        if isinstance(speed, bool) or not isinstance(speed, (float, int)) or speed <= 0:
            raise ValueError("speed must be a positive number")
        return self.snapshot()

    def finish(self) -> DashboardReplaySnapshot:
        return self.play()
