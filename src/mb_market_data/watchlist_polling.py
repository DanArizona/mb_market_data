"""Wall-clock polling contracts for coordinator-managed Watchlists.

The Watchlist Coordinator owns membership.  This module only describes when
API observations are due and captures the exact membership revision used for
each observation.  It deliberately has no ThinkOrSwim dependency: a slow or
unavailable display adapter must not influence API polling.

The initial production cadences are:

* UniWatchlist: seconds 00 and 30 of every eligible minute.
* FocusWatchlist: seconds 05, 20, 35, and 50.

Callers provide an explicit session window.  Market-calendar resolution and
holiday handling therefore remain outside this small scheduling primitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

from mb_market_data.schwab_quotes import normalize_symbols


ET = ZoneInfo("America/New_York")


class WatchlistKind(str, Enum):
    """Coordinator-managed Watchlists with an active API polling cadence."""

    UNI = "uni"
    FOCUS = "focus"


class MembershipUnavailableError(RuntimeError):
    """No hierarchy revision is effective for a required polling slot."""


class MembershipProvider(Protocol):
    """Resolve immutable channel membership at a historical instant."""

    def latest_effective_snapshot(
        self,
        watchlist_kind: WatchlistKind,
        *,
        at: datetime,
    ) -> WatchlistSnapshot | None: ...


POLL_SECONDS: dict[WatchlistKind, tuple[int, ...]] = {
    WatchlistKind.UNI: (0, 30),
    WatchlistKind.FOCUS: (5, 20, 35, 50),
}


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class PollWindow:
    """Half-open interval in which polling may be scheduled: [start, end)."""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.start_at, "start_at")
        _require_aware(self.end_at, "end_at")

        if self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")

    @property
    def session_date(self) -> date:
        return self.start_at.astimezone(ET).date()

    def contains(self, value: datetime) -> bool:
        _require_aware(value, "value")
        return self.start_at <= value < self.end_at


@dataclass(frozen=True)
class PollSlot:
    """One scheduled API acquisition for one Watchlist."""

    watchlist_kind: WatchlistKind
    scheduled_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.scheduled_at, "scheduled_at")

        local = self.scheduled_at.astimezone(ET)
        expected_seconds = POLL_SECONDS[self.watchlist_kind]

        if local.second not in expected_seconds or local.microsecond != 0:
            raise ValueError(
                f"{self.watchlist_kind.value} polling is not scheduled "
                f"at {local:%H:%M:%S.%f}"
            )

    @property
    def session_date(self) -> date:
        return self.scheduled_at.astimezone(ET).date()

    @property
    def slot_id(self) -> str:
        local = self.scheduled_at.astimezone(ET)
        return (
            f"{self.watchlist_kind.value}:"
            f"{local.isoformat(timespec='seconds')}"
        )


@dataclass(frozen=True)
class WatchlistSnapshot:
    """Immutable coordinator membership captured under one revision."""

    watchlist_kind: WatchlistKind
    session_date: date
    revision: int
    effective_at: datetime
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_aware(self.effective_at, "effective_at")

        if isinstance(self.revision, bool) or not isinstance(
            self.revision, int
        ):
            raise TypeError("revision must be an integer")

        if self.revision < 0:
            raise ValueError("revision must be nonnegative")

        object.__setattr__(self, "symbols", normalize_symbols(self.symbols))


@dataclass(frozen=True)
class PollRequest:
    """Dispatch record binding a slot to an exact membership revision."""

    slot: PollSlot
    watchlist_revision: int
    symbols: tuple[str, ...]
    dispatched_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.dispatched_at, "dispatched_at")

        if self.dispatched_at < self.slot.scheduled_at:
            raise ValueError("dispatched_at cannot precede scheduled_at")

        normalized = normalize_symbols(self.symbols)
        if not normalized:
            raise ValueError("symbols must contain at least one symbol")
        object.__setattr__(self, "symbols", normalized)

    @property
    def watchlist_kind(self) -> WatchlistKind:
        return self.slot.watchlist_kind

    @property
    def session_date(self) -> date:
        return self.slot.session_date

    @property
    def slot_id(self) -> str:
        return self.slot.slot_id

    @property
    def batch_id(self) -> str:
        return f"{self.slot_id}:r{self.watchlist_revision}"

    @property
    def dispatch_lateness_seconds(self) -> float:
        return (
            self.dispatched_at - self.slot.scheduled_at
        ).total_seconds()


@dataclass(frozen=True)
class SkippedPollSlot:
    """A due slot intentionally skipped because its channel is empty."""

    slot: PollSlot
    watchlist_revision: int
    membership_effective_at: datetime
    observed_at: datetime
    reason: str = "empty_membership"

    def __post_init__(self) -> None:
        _require_aware(
            self.membership_effective_at,
            "membership_effective_at",
        )
        _require_aware(self.observed_at, "observed_at")
        if self.observed_at < self.slot.scheduled_at:
            raise ValueError("observed_at cannot precede scheduled_at")
        if self.membership_effective_at > self.slot.scheduled_at:
            raise ValueError(
                "membership_effective_at cannot follow scheduled_at"
            )
        if isinstance(self.watchlist_revision, bool) or not isinstance(
            self.watchlist_revision, int
        ):
            raise TypeError("watchlist_revision must be an integer")
        if self.watchlist_revision < 0:
            raise ValueError("watchlist_revision must be nonnegative")

    @property
    def slot_id(self) -> str:
        return self.slot.slot_id


def capture_poll_request(
    slot: PollSlot,
    snapshot: WatchlistSnapshot,
    *,
    dispatched_at: datetime,
) -> PollRequest:
    """Capture the current membership revision for a scheduled acquisition."""

    _require_aware(dispatched_at, "dispatched_at")

    if slot.watchlist_kind != snapshot.watchlist_kind:
        raise ValueError("slot and snapshot Watchlist kinds do not match")

    if slot.session_date != snapshot.session_date:
        raise ValueError("slot and snapshot session dates do not match")

    if snapshot.effective_at > dispatched_at:
        raise ValueError("snapshot is not yet effective at dispatch time")

    return PollRequest(
        slot=slot,
        watchlist_revision=snapshot.revision,
        symbols=snapshot.symbols,
        dispatched_at=dispatched_at,
    )


def resolve_provider_poll_slot(
    slot: PollSlot,
    provider: MembershipProvider,
    *,
    dispatched_at: datetime,
) -> PollRequest | SkippedPollSlot:
    """Resolve slot-time membership and capture a request when nonempty.

    An empty channel returns a durable skip value instead of constructing an
    empty Schwab request. Absence of any effective hierarchy revision raises
    instead of guessing from startup membership.
    """

    snapshot = provider.latest_effective_snapshot(
        slot.watchlist_kind,
        at=slot.scheduled_at,
    )
    if snapshot is None:
        raise MembershipUnavailableError(
            "No hierarchy revision is effective for polling slot: "
            f"{slot.slot_id}"
        )
    if slot.watchlist_kind != snapshot.watchlist_kind:
        raise ValueError("slot and snapshot Watchlist kinds do not match")
    if slot.session_date != snapshot.session_date:
        raise ValueError("slot and snapshot session dates do not match")
    if snapshot.effective_at > slot.scheduled_at:
        raise ValueError("snapshot is not yet effective at slot time")
    if not snapshot.symbols:
        return SkippedPollSlot(
            slot=slot,
            watchlist_revision=snapshot.revision,
            membership_effective_at=snapshot.effective_at,
            observed_at=dispatched_at,
        )
    return capture_poll_request(
        slot,
        snapshot,
        dispatched_at=dispatched_at,
    )


def next_poll_slot(
    watchlist_kind: WatchlistKind,
    *,
    after: datetime,
    window: PollWindow,
) -> PollSlot | None:
    """Return the first eligible slot strictly later than ``after``.

    Strictly-later scheduling is intentional.  A caller should calculate the
    next slot after each acquisition completes.  If work overruns a slot, that
    old slot is skipped instead of being replayed in a catch-up burst.
    """

    _require_aware(after, "after")

    local_after = after.astimezone(ET)
    local_start = window.start_at.astimezone(ET)
    local_end = window.end_at.astimezone(ET)
    search_from = max(local_after, local_start - timedelta(microseconds=1))
    minute = search_from.replace(second=0, microsecond=0)

    for minute_offset in range(0, 60 * 48):
        candidate_minute = minute + timedelta(minutes=minute_offset)

        for second in POLL_SECONDS[watchlist_kind]:
            candidate = candidate_minute.replace(second=second)

            if candidate <= search_from:
                continue

            if candidate < local_start:
                continue

            if candidate >= local_end:
                return None

            return PollSlot(
                watchlist_kind=watchlist_kind,
                scheduled_at=candidate,
            )

    raise RuntimeError("poll-window search exceeded 48 hours")


def poll_slots_between(
    *,
    after: datetime,
    through: datetime,
    window: PollWindow,
    watchlist_kinds: Iterable[WatchlistKind] = tuple(WatchlistKind),
) -> tuple[PollSlot, ...]:
    """Return all eligible slots in ``(after, through]`` in time order."""

    _require_aware(after, "after")
    _require_aware(through, "through")

    if through < after:
        raise ValueError("through cannot precede after")

    slots: list[PollSlot] = []

    for kind in tuple(watchlist_kinds):
        cursor = after

        while True:
            slot = next_poll_slot(
                kind,
                after=cursor,
                window=window,
            )

            if slot is None or slot.scheduled_at > through:
                break

            slots.append(slot)
            cursor = slot.scheduled_at

    return tuple(
        sorted(
            slots,
            key=lambda item: (
                item.scheduled_at,
                item.watchlist_kind.value,
            ),
        )
    )


@dataclass
class PollSlotGuard:
    """In-memory guard against dispatching a slot more than once."""

    _claimed_slot_ids: set[str] = field(default_factory=set)

    def claim(self, slot: PollSlot) -> bool:
        """Return ``True`` once for a slot and ``False`` for duplicates."""

        if slot.slot_id in self._claimed_slot_ids:
            return False

        self._claimed_slot_ids.add(slot.slot_id)
        return True
