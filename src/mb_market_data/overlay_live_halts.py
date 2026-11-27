"""Read-only, selected-symbol daily Nasdaq evidence for the live Overlay.

The daily worker owns acquisition and publication. This reader only follows
its atomically replaced events file; it never creates a provider client or
writes to the session. A missing or invalid update must not erase evidence
already displayed or interrupt independent quote and candle refreshes.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Callable

from .overlay_halts import (
    OverlayHaltInterval, load_daily_nasdaq_halt_events, merge_live_halt_intervals,
)

if TYPE_CHECKING:
    from .quote_dashboard_replay import QuoteDashboardReplayController


def live_halt_events_path(
    data_root: str | Path | None, *, session_date: date,
    daily_events_path: str | Path | None = None,
    historical_halts_path: str | Path | None = None,
) -> Path | None:
    """Use explicit daily evidence or the attached session's standard file.

    A supplied historical halt input remains a fixed, retrospective source;
    it is never silently combined with daily first-observation evidence.
    """
    if data_root is None or historical_halts_path is not None:
        return None
    if daily_events_path is not None:
        return Path(daily_events_path)
    return (
        Path(data_root) / "output" / "daily_session" / str(session_date)
        / "nasdaq" / "events.json"
    )


class LiveHaltEventsReader:
    """Follow one session file and retain validated evidence per exact symbol."""

    def __init__(
        self, path: str | Path, *, session_date: date,
        now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.path = Path(path)
        self.session_date = session_date
        self._now = now_factory
        self._lock = RLock()
        self._cache: dict[str, tuple[OverlayHaltInterval, ...]] = {}
        self._versions: dict[str, tuple[int, int, int, int]] = {}
        self._status: dict[str, str] = {}

    @staticmethod
    def _symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Live Nasdaq evidence requires a selected symbol")
        return symbol.strip().upper()

    def _unavailable(self, symbol: str, condition: str) -> str:
        retained = self._cache.get(symbol, ())
        suffix = (
            f"; retaining {len(retained)} previously read episode(s) for {symbol}"
            if retained else f"; no halt evidence loaded for {symbol}"
        )
        return "Nasdaq halts: " + condition + suffix

    def latest(self, symbol: str) -> tuple[OverlayHaltInterval, ...]:
        """Return latest valid evidence, with truthful status on read failures."""
        selected = self._symbol(symbol)
        with self._lock:
            clock = self._now()
            if clock.tzinfo is None or clock.utcoffset() is None:
                raise ValueError("Live Nasdaq evidence clock must include a timezone")
            try:
                stat = self.path.stat()
                version = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                if self._versions.get(selected) != version:
                    intervals = load_daily_nasdaq_halt_events(
                        self.path, symbol=selected, session_date=self.session_date,
                    )
                    # Symbol selection constructs a new projector directly
                    # from latest(), so protect accepted history here as well
                    # as during an existing projector's live installation.
                    self._cache[selected] = merge_live_halt_intervals(
                        self._cache.get(selected, ()), intervals,
                    )
                    self._versions[selected] = version
                intervals = tuple(
                    visible for interval in self._cache.get(selected, ())
                    if (visible := interval.visible_at(clock)) is not None
                )
                self._status[selected] = (
                    f"Nasdaq halts: {len(intervals)} saved episode(s) for {selected}"
                )
            except FileNotFoundError:
                self._status[selected] = self._unavailable(
                    selected, "WAITING for the session events file",
                )
            except (OSError, ValueError, TypeError, KeyError) as error:
                self._status[selected] = self._unavailable(
                    selected, f"UNAVAILABLE ({type(error).__name__})",
                )
            # File versions cache raw evidence, not its current visibility.
            # Later receipts become eligible as the clock advances, without
            # requiring another file update; future receipts never leak early.
            return tuple(
                visible for interval in self._cache.get(selected, ())
                if (visible := interval.visible_at(clock)) is not None
            )

    def status(self, symbol: str) -> str:
        selected = self._symbol(symbol)
        with self._lock:
            return self._status.get(selected, "Nasdaq halts: awaiting first read")

    def refresh(self, controller: QuoteDashboardReplayController) -> tuple[int, str]:
        """Install daily updates for the current symbol without changing it."""
        current = controller.snapshot().observation_overlay
        if current is None:
            return 0, "Nasdaq halts: no symbol selected"
        selected = current.symbol
        with self._lock:
            previous = self._cache.get(selected, ())
            intervals = self.latest(selected)
            candidate = self._cache.get(selected, ())
            version = self._versions.get(selected)
        # Symbol construction calls this reader while the controller is
        # locked. Never take that controller lock while holding our own.
        try:
            changed = controller.install_live_halt_intervals(
                intervals, symbol=selected, session_date=self.session_date,
            )
        except (ValueError, RuntimeError) as error:
            with self._lock:
                # Selection can change between snapshot and installation.
                # Failed validation must not replace that symbol's last good
                # evidence in the reader or escape the independent live tick.
                if self._cache.get(selected, ()) == candidate and self._versions.get(selected) == version:
                    self._cache[selected] = previous
                    self._versions.pop(selected, None)
                self._status[selected] = self._unavailable(
                    selected, f"UNAVAILABLE ({type(error).__name__})",
                )
            changed = False
        return int(changed), self.status(selected)
