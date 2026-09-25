"""Nonblocking replay control for an interactive quote dashboard."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from mb_market_data.observation_overlay import (
    ObservationOverlayData,
    ObservationOverlayProjector,
)
from mb_market_data.quote_event_state import (
    QuoteEventStateProjector,
    QuoteEventStateSnapshot,
)
from mb_market_data.quote_journal_replay import ReplayEvent, ReplayTimeline


ET = ZoneInfo("America/New_York")


class DashboardReplayStatus(StrEnum):
    """Interactive replay lifecycle states."""

    PAUSED = "paused"
    PLAYING = "playing"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class DashboardReplaySnapshot:
    """One thread-safe controller snapshot for dashboard rendering."""

    state: QuoteEventStateSnapshot
    status: DashboardReplayStatus
    speed: float
    replay_time_utc: datetime | None
    first_event_time_utc: datetime | None
    last_event_time_utc: datetime | None
    total_event_count: int
    observation_overlay: ObservationOverlayData | None = None

    @property
    def applied_event_count(self) -> int:
        return self.state.event_count

    @property
    def progress_fraction(self) -> float:
        if self.total_event_count == 0:
            return 1.0
        return min(1.0, self.applied_event_count / self.total_event_count)


class QuoteDashboardReplayController:
    """Advance a projector without blocking Dash's request thread."""

    def __init__(
        self,
        *,
        timeline: ReplayTimeline,
        event_factory: Callable[[], Iterator[ReplayEvent]],
        speed: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        overlay_projector_factory: (
            Callable[[], ObservationOverlayProjector] | None
        ) = None,
    ) -> None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise TypeError("speed must be a number")
        if speed <= 0:
            raise ValueError("speed must be greater than zero")
        self._timeline = timeline
        self._event_factory = event_factory
        self._speed = float(speed)
        self._monotonic = monotonic
        self._overlay_projector_factory = overlay_projector_factory
        self._lock = threading.RLock()
        self._projector: QuoteEventStateProjector
        self._events: Iterator[ReplayEvent]
        self._pending: ReplayEvent | None
        self._status: DashboardReplayStatus
        self._position_utc: datetime | None
        self._overlay_projector: ObservationOverlayProjector | None
        self._anchor_position_utc: datetime | None = None
        self._anchor_monotonic: float | None = None
        self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        self._projector = QuoteEventStateProjector(
            session_date=self._timeline.session_date
        )
        self._overlay_projector = (
            self._overlay_projector_factory()
            if self._overlay_projector_factory is not None
            else None
        )
        self._events = iter(self._event_factory())
        self._pending = next(self._events, None)
        self._position_utc = self._timeline.first_available_at_utc
        self._anchor_position_utc = None
        self._anchor_monotonic = None
        self._status = (
            DashboardReplayStatus.PAUSED
            if self._pending is not None
            else DashboardReplayStatus.COMPLETE
        )

    def _next_unlocked(self) -> bool:
        if self._pending is None:
            self._status = DashboardReplayStatus.COMPLETE
            return False
        event = self._pending
        if self._overlay_projector is not None:
            self._overlay_projector.apply(event)
        self._projector.apply(event)
        self._position_utc = event.available_at_utc
        self._pending = next(self._events, None)
        if self._pending is None:
            self._status = DashboardReplayStatus.COMPLETE
        return True

    def _target_time_unlocked(self, now: float) -> datetime | None:
        if (
            self._anchor_position_utc is None
            or self._anchor_monotonic is None
        ):
            return self._position_utc
        elapsed = max(0.0, now - self._anchor_monotonic)
        target = self._anchor_position_utc + timedelta(
            seconds=elapsed * self._speed
        )
        last = self._timeline.last_available_at_utc
        return min(target, last) if last is not None else target

    def _advance_unlocked(self, now: float) -> None:
        if self._status is not DashboardReplayStatus.PLAYING:
            return
        target = self._target_time_unlocked(now)
        if target is None:
            self._status = DashboardReplayStatus.COMPLETE
            return
        while (
            self._pending is not None
            and self._pending.available_at_utc <= target
        ):
            self._next_unlocked()
        self._position_utc = target
        if self._pending is None:
            self._status = DashboardReplayStatus.COMPLETE
            self._position_utc = self._timeline.last_available_at_utc

    def _reanchor_unlocked(self, now: float) -> None:
        self._anchor_position_utc = self._position_utc
        self._anchor_monotonic = now

    def snapshot(self) -> DashboardReplaySnapshot:
        with self._lock:
            overlay = (
                self._overlay_projector.snapshot(self._position_utc)
                if (
                    self._overlay_projector is not None
                    and self._position_utc is not None
                )
                else None
            )
            return DashboardReplaySnapshot(
                state=self._projector.snapshot(),
                status=self._status,
                speed=self._speed,
                replay_time_utc=self._position_utc,
                first_event_time_utc=(
                    self._timeline.first_available_at_utc
                ),
                last_event_time_utc=self._timeline.last_available_at_utc,
                total_event_count=self._timeline.event_count,
                observation_overlay=overlay,
            )

    def play(self) -> DashboardReplaySnapshot:
        with self._lock:
            if self._status is DashboardReplayStatus.COMPLETE:
                return self.snapshot()
            now = self._monotonic()
            self._status = DashboardReplayStatus.PLAYING
            self._reanchor_unlocked(now)
            self._advance_unlocked(now)
            return self.snapshot()

    def pause(self) -> DashboardReplaySnapshot:
        with self._lock:
            if self._status is DashboardReplayStatus.PLAYING:
                now = self._monotonic()
                self._advance_unlocked(now)
                if self._status is not DashboardReplayStatus.COMPLETE:
                    self._status = DashboardReplayStatus.PAUSED
                self._anchor_position_utc = None
                self._anchor_monotonic = None
            return self.snapshot()

    def toggle(self) -> DashboardReplaySnapshot:
        with self._lock:
            if self._status is DashboardReplayStatus.PLAYING:
                return self.pause()
            return self.play()

    def tick(self) -> DashboardReplaySnapshot:
        with self._lock:
            self._advance_unlocked(self._monotonic())
            return self.snapshot()

    def step(self) -> DashboardReplaySnapshot:
        with self._lock:
            self.pause()
            self._next_unlocked()
            return self.snapshot()

    def restart(self) -> DashboardReplaySnapshot:
        with self._lock:
            self._reset_unlocked()
            return self.snapshot()

    def seek(self, target_utc: datetime) -> DashboardReplaySnapshot:
        """Reconstruct state through an inclusive historical cutoff."""

        if not isinstance(target_utc, datetime):
            raise TypeError("target_utc must be a datetime")
        if target_utc.tzinfo is None or target_utc.utcoffset() is None:
            raise ValueError("target_utc must be timezone-aware")
        target_utc = target_utc.astimezone(timezone.utc)
        if target_utc.astimezone(ET).date() != self._timeline.session_date:
            raise ValueError(
                "target_utc must fall on the journal session date in ET"
            )

        with self._lock:
            self._reset_unlocked()
            while (
                self._pending is not None
                and self._pending.available_at_utc <= target_utc
            ):
                self._next_unlocked()
            self._position_utc = target_utc
            self._anchor_position_utc = None
            self._anchor_monotonic = None
            self._status = (
                DashboardReplayStatus.PAUSED
                if self._pending is not None
                else DashboardReplayStatus.COMPLETE
            )
            return self.snapshot()

    def set_speed(self, speed: float) -> DashboardReplaySnapshot:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise TypeError("speed must be a number")
        if speed <= 0:
            raise ValueError("speed must be greater than zero")
        with self._lock:
            now = self._monotonic()
            self._advance_unlocked(now)
            self._speed = float(speed)
            if self._status is DashboardReplayStatus.PLAYING:
                self._reanchor_unlocked(now)
            return self.snapshot()

    def finish(self) -> DashboardReplaySnapshot:
        """Advance immediately to the final event."""

        with self._lock:
            while self._pending is not None:
                self._next_unlocked()
            self._position_utc = self._timeline.last_available_at_utc
            self._status = DashboardReplayStatus.COMPLETE
            return self.snapshot()
