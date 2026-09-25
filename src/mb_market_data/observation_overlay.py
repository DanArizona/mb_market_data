"""Replay-causal preparation for the Observation Overlay MVP.

The overlay is a read-only diagnostic derived from two independent inputs:

* a schema-v2 quote-journal event stream; and
* a separately cached Schwab five-minute OHLCV series.

Membership becomes visible at its hierarchy effective time, quotes at the
acquisition completion time, and a candle only after its five-minute interval
has closed.  Nothing in this module writes to or influences the journal.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    MembershipRevisionEvent,
    QuoteAcquisitionEvent,
    ReplayEvent,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision


ET = ZoneInfo("America/New_York")
UTC = timezone.utc
OHLCV_CACHE_VERSION = "observation-overlay-ohlcv-v1"
OHLCV_FREQUENCY_MINUTES = 5
OHLCV_INTERVAL = timedelta(minutes=OHLCV_FREQUENCY_MINUTES)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ObservationOverlayError(RuntimeError):
    """The requested overlay cannot be prepared without ambiguity."""


class MembershipBand(StrEnum):
    """Highest active hierarchy tier for one symbol."""

    OUTSIDE_UNI = "outside_uni"
    UNI = "uni"
    FOCUS = "focus"
    HOT = "hot"


MEMBERSHIP_BAND_COLORS: Mapping[MembershipBand, str] = MappingProxyType(
    {
        MembershipBand.OUTSIDE_UNI: "#808080",
        MembershipBand.UNI: "#00bcd4",
        MembershipBand.FOCUS: "#d4a017",
        MembershipBand.HOT: "#d100d1",
    }
)


def _nonblank(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank text")
    return value.strip()


def _symbol(value: str) -> str:
    return _nonblank(value, "symbol").upper()


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _utc(value: datetime, name: str) -> datetime:
    return _aware(value, name).astimezone(UTC)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _volume(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("volume must be an integer")
    if value < 0:
        raise ValueError("volume must be nonnegative")
    return value


def _sha256(value: str, name: str) -> str:
    normalized = _nonblank(value, name).casefold()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return normalized


@dataclass(frozen=True, slots=True)
class OverlayCandle:
    """One immutable five-minute OHLCV candle in Eastern Time."""

    symbol: str
    start_et: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        normalized_symbol = _symbol(self.symbol)
        normalized_start = _aware(self.start_et, "start_et").astimezone(ET)
        if (
            normalized_start.second != 0
            or normalized_start.microsecond != 0
            or normalized_start.minute % OHLCV_FREQUENCY_MINUTES != 0
        ):
            raise ValueError("start_et must lie on a five-minute ET boundary")
        open_price = _finite(self.open, "open")
        high_price = _finite(self.high, "high")
        low_price = _finite(self.low, "low")
        close_price = _finite(self.close, "close")
        if high_price < max(open_price, low_price, close_price):
            raise ValueError("high is below another OHLC price")
        if low_price > min(open_price, high_price, close_price):
            raise ValueError("low is above another OHLC price")

        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(self, "start_et", normalized_start)
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high_price)
        object.__setattr__(self, "low", low_price)
        object.__setattr__(self, "close", close_price)
        object.__setattr__(self, "volume", _volume(self.volume))

    @property
    def end_et(self) -> datetime:
        return self.start_et + OHLCV_INTERVAL

    @property
    def available_at_utc(self) -> datetime:
        """Causal visibility boundary: the completed candle's end time."""

        return self.end_et.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ObservationOverlayOHLCVCache:
    """Validated, separately persisted Schwab OHLCV evidence."""

    symbol: str
    session_date: date
    provider: str
    source: str
    acquired_at_utc: datetime
    request_start_et: datetime
    request_end_et: datetime
    source_payload_sha256: str
    candles: tuple[OverlayCandle, ...]
    frequency_minutes: int = OHLCV_FREQUENCY_MINUTES
    extended_hours: bool = True
    cache_version: str = OHLCV_CACHE_VERSION

    def __post_init__(self) -> None:
        normalized_symbol = _symbol(self.symbol)
        if self.cache_version != OHLCV_CACHE_VERSION:
            raise ValueError(
                f"unsupported OHLCV cache version: {self.cache_version!r}"
            )
        provider = _nonblank(self.provider, "provider")
        if provider.casefold() != "schwab":
            raise ValueError("Observation Overlay MVP requires Schwab OHLCV")
        source = _nonblank(self.source, "source")
        acquired = _utc(self.acquired_at_utc, "acquired_at_utc")
        request_start = _aware(
            self.request_start_et, "request_start_et"
        ).astimezone(ET)
        request_end = _aware(
            self.request_end_et, "request_end_et"
        ).astimezone(ET)
        if request_start >= request_end:
            raise ValueError("request_start_et must precede request_end_et")
        if (
            request_start.date() != self.session_date
            or request_end.date() != self.session_date
        ):
            raise ValueError("OHLCV request bounds must be on session_date")
        if self.frequency_minutes != OHLCV_FREQUENCY_MINUTES:
            raise ValueError("Observation Overlay MVP requires five-minute data")
        if self.extended_hours is not True:
            raise ValueError("Observation Overlay MVP requires extended hours")

        candles = tuple(self.candles)
        previous_start: datetime | None = None
        for candle in candles:
            if not isinstance(candle, OverlayCandle):
                raise TypeError("candles must contain OverlayCandle values")
            if candle.symbol != normalized_symbol:
                raise ValueError("candle symbol differs from cache symbol")
            if candle.start_et.date() != self.session_date:
                raise ValueError("candle is not on cache session_date")
            if not request_start <= candle.start_et < request_end:
                raise ValueError("candle lies outside OHLCV request bounds")
            if previous_start is not None and candle.start_et <= previous_start:
                raise ValueError("candles must be unique and strictly ordered")
            previous_start = candle.start_et

        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(self, "provider", "Schwab")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "acquired_at_utc", acquired)
        object.__setattr__(self, "request_start_et", request_start)
        object.__setattr__(self, "request_end_et", request_end)
        object.__setattr__(
            self,
            "source_payload_sha256",
            _sha256(self.source_payload_sha256, "source_payload_sha256"),
        )
        object.__setattr__(self, "candles", candles)

    def visible_candles(
        self,
        replay_time_utc: datetime,
    ) -> tuple[OverlayCandle, ...]:
        """Return exactly those candles completed by the replay cutoff."""

        cutoff = _utc(replay_time_utc, "replay_time_utc")
        return tuple(
            candle
            for candle in self.candles
            if candle.available_at_utc <= cutoff
        )


@dataclass(frozen=True, slots=True)
class OverlayMembershipTransition:
    """One visible change in the symbol's highest hierarchy tier."""

    available_at_utc: datetime
    revision: int
    band: MembershipBand
    source: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class OverlayQuotePoint:
    """One selected-symbol outcome visible at acquisition completion."""

    available_at_utc: datetime
    scheduled_at_utc: datetime
    acquisition_id: str
    channel: str
    channel_revision: int
    status: str
    detail: str | None
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ObservationOverlayData:
    """Complete replay-causal input for one symbol and one replay instant."""

    session_date: date
    symbol: str
    replay_time_utc: datetime
    cache: ObservationOverlayOHLCVCache
    candles: tuple[OverlayCandle, ...]
    membership_transitions: tuple[OverlayMembershipTransition, ...]
    quote_points: tuple[OverlayQuotePoint, ...]

    @property
    def current_band(self) -> MembershipBand:
        if not self.membership_transitions:
            return MembershipBand.OUTSIDE_UNI
        return self.membership_transitions[-1].band

    @property
    def current_band_color(self) -> str:
        return MEMBERSHIP_BAND_COLORS[self.current_band]

    @property
    def latest_quote(self) -> OverlayQuotePoint | None:
        return self.quote_points[-1] if self.quote_points else None


class ObservationOverlayProjector:
    """Incrementally retain one symbol's replay-causal overlay evidence.

    The projector is intentionally small: it keeps membership transitions and
    selected-symbol quote outcomes only.  It does not retain the complete
    journal event stream, so attaching it to a full-day dashboard does not
    duplicate every universe observation in memory.
    """

    def __init__(
        self,
        *,
        cache: ObservationOverlayOHLCVCache,
        symbol: str,
        session_date: date,
    ) -> None:
        normalized_symbol = _symbol(symbol)
        if cache.symbol != normalized_symbol:
            raise ValueError("OHLCV cache symbol differs from overlay symbol")
        if cache.session_date != session_date:
            raise ValueError("OHLCV cache session differs from overlay session")
        self.cache = cache
        self.symbol = normalized_symbol
        self.session_date = session_date
        self._transitions: list[OverlayMembershipTransition] = []
        self._quote_points: list[OverlayQuotePoint] = []
        self._current_band = MembershipBand.OUTSIDE_UNI
        self._last_available: datetime | None = None
        self._last_revision = -1

    def apply(self, event: ReplayEvent) -> None:
        """Validate and incorporate one causally ordered journal event."""

        available = _utc(event.available_at_utc, "event.available_at_utc")
        if (
            self._last_available is not None
            and available < self._last_available
        ):
            raise ObservationOverlayError(
                "journal events are not in causal availability order"
            )
        if _event_session_date(event) != self.session_date:
            raise ObservationOverlayError(
                "journal event belongs to another session"
            )
        if isinstance(event, ChannelRevisionEvent):
            raise ObservationOverlayError(
                "Observation Overlay MVP requires a schema-v2 hierarchy journal"
            )

        self._last_available = available
        if isinstance(event, MembershipRevisionEvent):
            if event.revision.revision <= self._last_revision:
                raise ObservationOverlayError(
                    "hierarchy revisions are not strictly increasing"
                )
            self._last_revision = event.revision.revision
            next_band = membership_band(event.revision, self.symbol)
            if next_band is not self._current_band:
                self._transitions.append(
                    OverlayMembershipTransition(
                        available_at_utc=available,
                        revision=event.revision.revision,
                        band=next_band,
                        source=event.revision.source,
                        reason=event.revision.reason,
                    )
                )
                self._current_band = next_band
            return

        matches = tuple(
            observation
            for observation in event.observations
            if observation.symbol == self.symbol
        )
        if len(matches) > 1:
            raise ObservationOverlayError(
                "acquisition contains duplicate selected-symbol observations"
            )
        if not matches:
            return
        observation = matches[0]
        self._quote_points.append(
            OverlayQuotePoint(
                available_at_utc=available,
                scheduled_at_utc=observation.scheduled_at_utc,
                acquisition_id=event.acquisition.acquisition_id,
                channel=event.acquisition.channel,
                channel_revision=event.acquisition.channel_revision,
                status=observation.status,
                detail=observation.detail,
                values=MappingProxyType(dict(observation.values)),
            )
        )

    def snapshot(self, replay_time_utc: datetime) -> ObservationOverlayData:
        """Return the evidence visible through one inclusive replay cutoff."""

        cutoff = _utc(replay_time_utc, "replay_time_utc")
        if cutoff.astimezone(ET).date() != self.session_date:
            raise ValueError("replay_time_utc must fall on session_date in ET")
        return ObservationOverlayData(
            session_date=self.session_date,
            symbol=self.symbol,
            replay_time_utc=cutoff,
            cache=self.cache,
            candles=self.cache.visible_candles(cutoff),
            membership_transitions=tuple(
                item
                for item in self._transitions
                if item.available_at_utc <= cutoff
            ),
            quote_points=tuple(
                item
                for item in self._quote_points
                if item.available_at_utc <= cutoff
            ),
        )


def membership_band(
    revision: SamplingHierarchyRevision,
    symbol: str,
) -> MembershipBand:
    """Return the highest active tier in one valid hierarchy revision."""

    normalized = _symbol(symbol)
    if normalized in revision.hot_symbols:
        return MembershipBand.HOT
    if normalized in revision.focus_symbols:
        return MembershipBand.FOCUS
    if normalized in revision.uni_symbols:
        return MembershipBand.UNI
    return MembershipBand.OUTSIDE_UNI


def _event_session_date(event: ReplayEvent) -> date:
    if isinstance(event, (ChannelRevisionEvent, MembershipRevisionEvent)):
        return event.revision.session_date
    return event.acquisition.session_date


def prepare_observation_overlay(
    events: Iterable[ReplayEvent],
    *,
    cache: ObservationOverlayOHLCVCache,
    symbol: str,
    session_date: date,
    replay_time_utc: datetime,
) -> ObservationOverlayData:
    """Prepare one deterministic overlay without mutating either input.

    The event stream must be the causally ordered schema-v2 stream produced by
    :class:`QuoteJournalReplayReader`.  The replay cutoff is inclusive.
    """

    projector = ObservationOverlayProjector(
        cache=cache,
        symbol=symbol,
        session_date=session_date,
    )
    for event in events:
        projector.apply(event)
    return projector.snapshot(replay_time_utc)


def _cache_payload(cache: ObservationOverlayOHLCVCache) -> dict[str, Any]:
    return {
        "cache_version": cache.cache_version,
        "provider": cache.provider,
        "source": cache.source,
        "symbol": cache.symbol,
        "session_date": cache.session_date.isoformat(),
        "frequency_minutes": cache.frequency_minutes,
        "extended_hours": cache.extended_hours,
        "acquired_at_utc": cache.acquired_at_utc.isoformat(),
        "request_start_et": cache.request_start_et.isoformat(),
        "request_end_et": cache.request_end_et.isoformat(),
        "source_payload_sha256": cache.source_payload_sha256,
        "candles": [
            {
                "start_et": candle.start_et.isoformat(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in cache.candles
        ],
    }


def write_observation_overlay_cache(
    path: str | Path,
    cache: ObservationOverlayOHLCVCache,
) -> Path:
    """Write one immutable cache file outside the observation journal."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(
            _cache_payload(cache),
            output,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        output.write("\n")
    return target


def load_observation_overlay_cache(
    path: str | Path,
) -> ObservationOverlayOHLCVCache:
    """Load and fully validate one immutable OHLCV cache file."""

    source_path = Path(path)
    with source_path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, Mapping):
        raise ValueError("OHLCV cache must contain one JSON object")
    candle_payloads = payload.get("candles")
    if not isinstance(candle_payloads, Sequence) or isinstance(
        candle_payloads, (str, bytes)
    ):
        raise ValueError("OHLCV cache candles must be an array")

    symbol = _symbol(payload.get("symbol"))
    candles: list[OverlayCandle] = []
    for item in candle_payloads:
        if not isinstance(item, Mapping):
            raise ValueError("OHLCV cache candle must be an object")
        candles.append(
            OverlayCandle(
                symbol=symbol,
                start_et=datetime.fromisoformat(
                    _nonblank(item.get("start_et"), "candle start_et")
                ),
                open=item.get("open"),
                high=item.get("high"),
                low=item.get("low"),
                close=item.get("close"),
                volume=item.get("volume"),
            )
        )

    frequency = payload.get("frequency_minutes")
    if isinstance(frequency, bool) or not isinstance(frequency, int):
        raise TypeError("frequency_minutes must be an integer")
    extended_hours = payload.get("extended_hours")
    if not isinstance(extended_hours, bool):
        raise TypeError("extended_hours must be a boolean")
    return ObservationOverlayOHLCVCache(
        symbol=symbol,
        session_date=date.fromisoformat(
            _nonblank(payload.get("session_date"), "session_date")
        ),
        provider=_nonblank(payload.get("provider"), "provider"),
        source=_nonblank(payload.get("source"), "source"),
        acquired_at_utc=datetime.fromisoformat(
            _nonblank(payload.get("acquired_at_utc"), "acquired_at_utc")
        ),
        request_start_et=datetime.fromisoformat(
            _nonblank(payload.get("request_start_et"), "request_start_et")
        ),
        request_end_et=datetime.fromisoformat(
            _nonblank(payload.get("request_end_et"), "request_end_et")
        ),
        source_payload_sha256=_nonblank(
            payload.get("source_payload_sha256"), "source_payload_sha256"
        ),
        candles=tuple(candles),
        frequency_minutes=frequency,
        extended_hours=extended_hours,
        cache_version=_nonblank(payload.get("cache_version"), "cache_version"),
    )


def sha256_file(path: str | Path) -> str:
    """Return a lowercase SHA-256 digest for one cache artifact."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
