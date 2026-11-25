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
    ObservationOverlayOHLCVCache,
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
        overlay_end_at_utc: datetime | None = None,
        symbol_projector_factory: (
            Callable[[str], ObservationOverlayProjector] | None
        ) = None,
        indexed_symbol_projector_factory: (
            Callable[[str, datetime | None, ReplayEvent | None], ObservationOverlayProjector] | None
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
        self._symbol_projector_factory = symbol_projector_factory
        self._indexed_symbol_projector_factory = indexed_symbol_projector_factory
        if overlay_end_at_utc is not None:
            if overlay_projector_factory is None:
                raise ValueError("overlay_end_at_utc requires an overlay projector")
            if (
                overlay_end_at_utc.tzinfo is None
                or overlay_end_at_utc.utcoffset() is None
            ):
                raise ValueError("overlay_end_at_utc must be timezone-aware")
            if overlay_end_at_utc.astimezone(ET).date() != timeline.session_date:
                raise ValueError(
                    "overlay_end_at_utc must be on the journal session date"
                )
            overlay_end_at_utc = overlay_end_at_utc.astimezone(timezone.utc)
        self._end_time_utc = max(
            (
                value
                for value in (timeline.last_available_at_utc, overlay_end_at_utc)
                if value is not None
            ),
            default=None,
        )
        self._lock = threading.RLock()
        self._projector: QuoteEventStateProjector
        self._events: Iterator[ReplayEvent]
        self._pending: ReplayEvent | None
        self._status: DashboardReplayStatus
        self._position_utc: datetime | None
        self._overlay_projector: ObservationOverlayProjector | None
        self._anchor_position_utc: datetime | None = None
        self._anchor_monotonic: float | None = None
        self._live_readonly = False
        self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        if self._live_readonly:
            raise RuntimeError("Cannot reset a live read-only controller")
        self._applied_event_ids: set[str] = set()
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
            or (
                self._position_utc is not None
                and self._end_time_utc is not None
                and self._position_utc < self._end_time_utc
            )
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
        self._applied_event_ids.add(event.event_id)
        self._position_utc = event.available_at_utc
        self._pending = next(self._events, None)
        if self._pending is None and (
            self._end_time_utc is None or self._position_utc >= self._end_time_utc
        ):
            self._status = DashboardReplayStatus.COMPLETE
        return True

    def _target_time_unlocked(self, now: float) -> datetime | None:
        if self._anchor_position_utc is None or self._anchor_monotonic is None:
            return self._position_utc
        elapsed = max(0.0, now - self._anchor_monotonic)
        target = self._anchor_position_utc + timedelta(seconds=elapsed * self._speed)
        last = self._end_time_utc
        return min(target, last) if last is not None else target

    def _advance_unlocked(self, now: float) -> None:
        if self._status is not DashboardReplayStatus.PLAYING:
            return
        target = self._target_time_unlocked(now)
        if target is None:
            self._status = DashboardReplayStatus.COMPLETE
            return
        while self._pending is not None and self._pending.available_at_utc <= target:
            self._next_unlocked()
        self._position_utc = target
        if self._pending is None and (
            self._end_time_utc is None or target >= self._end_time_utc
        ):
            self._status = DashboardReplayStatus.COMPLETE
            self._position_utc = self._end_time_utc

    def _reanchor_unlocked(self, now: float) -> None:
        self._anchor_position_utc = self._position_utc
        self._anchor_monotonic = now

    def snapshot(self) -> DashboardReplaySnapshot:
        with self._lock:
            overlay_cutoff = self._position_utc
            # Live candle evidence can arrive after the last committed quote
            # acquisition. Its own reception time is an independent causal
            # display cutoff, never an invented quote/journal event.
            if (
                self._live_readonly
                and self._overlay_projector is not None
                and self._overlay_projector.cache is not None
            ):
                acquired = self._overlay_projector.cache.acquired_at_utc
                overlay_cutoff = max(
                    (time for time in (overlay_cutoff, acquired) if time is not None),
                    default=None,
                )
            overlay = (
                self._overlay_projector.snapshot(overlay_cutoff)
                if (
                    self._overlay_projector is not None
                    and overlay_cutoff is not None
                )
                else None
            )
            return DashboardReplaySnapshot(
                state=self._projector.snapshot(),
                status=self._status,
                speed=self._speed,
                replay_time_utc=self._position_utc,
                first_event_time_utc=(self._timeline.first_available_at_utc),
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
            if self._pending is None and self._end_time_utc is not None:
                self._position_utc = self._end_time_utc
                self._status = DashboardReplayStatus.COMPLETE
            else:
                self._next_unlocked()
            return self.snapshot()

    def restart(self) -> DashboardReplaySnapshot:
        with self._lock:
            self._reset_unlocked()
            return self.snapshot()

    def applied_event_ids(self) -> frozenset[str]:
        """The exact event IDs projected, including any equal-time prefix."""
        with self._lock:
            return frozenset(self._applied_event_ids)

    def activate_live_readonly(self) -> DashboardReplaySnapshot:
        """Freeze replay controls; subsequent updates must be committed events.

        Call only after seek(now) on a validated current-day journal.
        """
        with self._lock:
            if self._live_readonly:
                return self.snapshot()
            projected = self._projector.snapshot()
            self._live_readonly = True
            self._pending = None
            self._events = iter(())
            self._position_utc = projected.current_time_utc
            self._end_time_utc = projected.current_time_utc
            self._timeline = ReplayTimeline(
                session_date=self._timeline.session_date,
                event_count=projected.event_count,
                first_available_at_utc=self._timeline.first_available_at_utc,
                last_available_at_utc=projected.current_time_utc,
            )
            self._status = DashboardReplayStatus.COMPLETE
            return self.snapshot()

    def append_live_committed(self, events: tuple[ReplayEvent, ...]) -> DashboardReplaySnapshot:
        """Project newly committed, causally ordered events, never journal writes."""
        with self._lock:
            if not self._live_readonly:
                raise RuntimeError("Committed updates require read-only live mode")
            prior_time = self._projector.snapshot().current_time_utc
            for event in events:
                if event.event_id in self._applied_event_ids:
                    raise ValueError(f"Duplicate live event: {event.event_id}")
                if prior_time is not None and event.available_at_utc < prior_time:
                    raise ValueError("New committed event predates current projection")
                self._projector.apply(event)
                if self._overlay_projector is not None:
                    self._overlay_projector.apply(event)
                self._applied_event_ids.add(event.event_id)
                prior_time = event.available_at_utc
            if events:
                self._position_utc = prior_time
                self._end_time_utc = prior_time
                applied = self._projector.snapshot()
                self._timeline = ReplayTimeline(
                    session_date=self._timeline.session_date,
                    event_count=applied.event_count,
                    first_available_at_utc=self._timeline.first_available_at_utc,
                    last_available_at_utc=prior_time,
                )
            return self.snapshot()

    def install_live_ohlcv_cache(self, cache: ObservationOverlayOHLCVCache) -> bool:
        """Read-only Real Time chart update from independently saved evidence.

        The controller never acquires candles, authenticates or edits a quote
        journal. A cache for a previously selected symbol is refused.
        Historical mode cannot update its immutable replay cache.
        """
        with self._lock:
            if not self._live_readonly:
                raise RuntimeError("Live OHLCV updates require Real Time mode")
            if self._overlay_projector is None:
                raise ValueError("No selected Overlay symbol for live OHLCV")
            if cache.symbol != self._overlay_projector.symbol:
                raise ValueError("Live OHLCV cache is for another selected symbol")
            if cache.session_date != self._timeline.session_date:
                raise ValueError("Live OHLCV cache belongs to another journal date")
            return self._overlay_projector.update_live_cache(cache)

    def select_overlay_symbol(self, symbol: str) -> DashboardReplaySnapshot:
        """Switch the inspected symbol without changing the main replay clock.

        Rebuild only the selected-symbol Overlay from the same immutable event
        source through the present causal cutoff. Current hierarchy and replay
        progress remain untouched. Invalid/incompatible candidates fail before
        replacing the visible Overlay.
        """
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Chart symbol must be nonblank text")
        normalized = symbol.strip().upper()
        with self._lock:
            if self._overlay_projector is not None and self._overlay_projector.symbol == normalized:
                return self.snapshot()
            if self._symbol_projector_factory is None:
                raise RuntimeError("Historical chart symbol switching is unavailable")
            cutoff = self._position_utc
            if self._indexed_symbol_projector_factory is not None:
                replacement = self._indexed_symbol_projector_factory(
                    normalized, cutoff, self._pending
                )
            else:
                replacement = self._symbol_projector_factory(normalized)
                if cutoff is not None:
                    for event in self._event_factory():
                        if event.available_at_utc > cutoff:
                            break
                        replacement.apply(event)
            if replacement.symbol != normalized or replacement.session_date != self._timeline.session_date:
                raise ValueError("Selected-symbol projector identity mismatch")
            self._overlay_projector = replacement
            # The next seek/restart must retain the newly inspected symbol.
            self._overlay_projector_factory = lambda: self._symbol_projector_factory(normalized)
            return self.snapshot()

    def seek(self, target_utc: datetime) -> DashboardReplaySnapshot:
        """Reconstruct state through an inclusive historical cutoff."""

        if not isinstance(target_utc, datetime):
            raise TypeError("target_utc must be a datetime")
        if target_utc.tzinfo is None or target_utc.utcoffset() is None:
            raise ValueError("target_utc must be timezone-aware")
        target_utc = target_utc.astimezone(timezone.utc)
        if target_utc.astimezone(ET).date() != self._timeline.session_date:
            raise ValueError("target_utc must fall on the journal session date in ET")

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
                or (self._end_time_utc is not None and target_utc < self._end_time_utc)
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
            self._position_utc = self._end_time_utc
            self._status = DashboardReplayStatus.COMPLETE
            return self.snapshot()
