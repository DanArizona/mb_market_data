"""Isolated, immutable intraday Schwab OHLCV evidence for the Real Time Overlay.

The producer accepts a caller-supplied, already-authenticated price_history
client. There is NO login, credential access, polling loop or live provider
invocation in this module. The Real Time viewer uses only LiveOHLCVReader,
which reads separate immutable evidence bundles and never starts a client.

A candle is retained only if its full five-minute interval ended BEFORE the
request cutoff, and by the time the response was received. This prevents
Schwab's provisional last bar from masquerading as a completed OHLCV bar.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping

from .observation_overlay import (
    ET, OHLCV_INTERVAL, ObservationOverlayOHLCVCache,
    load_observation_overlay_cache, write_observation_overlay_cache,
)
from .observation_overlay_acquisition import (
    ObservationOverlayDataError, ObservationOverlayRequestError,
    _epoch_ms_to_et, _read_response_payload, _response_status,
    parse_observation_overlay_payload, request_bounds,
)

UTC = timezone.utc
BUNDLE_RAW = "price_history_raw.json"
BUNDLE_CACHE = "ohlcv_cache.json"
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,19}$")
BUNDLE_PATTERN = re.compile(r"^(\d{8}T\d{12}Z)-([a-f0-9]{16})$")


class LiveOHLCVError(ValueError):
    """Live candle evidence is unsafe, inconsistent or unavailable."""


class LiveOHLCVNotReady(LiveOHLCVError):
    """No five-minute candle has completed in the requested session yet."""


@dataclass(frozen=True)
class LiveOHLCVAcquisition:
    cache: ObservationOverlayOHLCVCache
    raw_payload: bytes
    request_started_at_utc: datetime
    response_received_at_utc: datetime
    skipped_provisional_candles: int
    http_status: int | None


def _symbol(value: str) -> str:
    if not isinstance(value, str):
        raise LiveOHLCVError("Live OHLCV symbol must be a string")
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol) or ".." in symbol:
        raise LiveOHLCVError("Invalid exact symbol for isolated OHLCV files")
    return symbol


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LiveOHLCVError("Live OHLCV clock must be timezone-aware")
    return value.astimezone(UTC)


def _closed_payload(
    payload: Mapping[str, Any], *,
    session_date: date,
    requested_until_et: datetime,
) -> tuple[dict[str, Any], int]:
    """Keep only source candles that closed by the *request* boundary.

    The received response is potentially later than the request. The earlier
    boundary is deliberately the publication limit, even if a five-minute
    interval closes while the HTTP request is in flight.
    """
    raw = payload.get("candles")
    if not isinstance(raw, list):
        raise ObservationOverlayDataError("price-history response has no candle list")
    bounds_start, regular_end = request_bounds(session_date)
    if not bounds_start < requested_until_et <= regular_end:
        raise LiveOHLCVError("Live candle request is outside the session bounds")
    complete: list[Mapping[str, Any]] = []
    skipped = 0
    for item in raw:
        if not isinstance(item, Mapping):
            raise ObservationOverlayDataError("candle must be a JSON object")
        start = _epoch_ms_to_et(item.get("datetime"))
        if start.date() != session_date or start < bounds_start or start >= regular_end:
            continue
        if start + OHLCV_INTERVAL > requested_until_et:
            skipped += 1
            continue
        complete.append(item)
    return {**payload, "candles": complete}, skipped


def _build_closed_cache(
    payload: Mapping[str, Any], *, symbol: str, session_date: date,
    requested_until_et: datetime, received_at_utc: datetime, raw_sha: str,
) -> tuple[ObservationOverlayOHLCVCache, int]:
    filtered, skipped = _closed_payload(
        payload, session_date=session_date,
        requested_until_et=requested_until_et,
    )
    if not filtered["candles"]:
        raise LiveOHLCVNotReady(
            "No completed five-minute Schwab candle available at the request cutoff"
        )
    cache = parse_observation_overlay_payload(
        filtered,
        symbol=symbol,
        session_date=session_date,
        acquired_at_utc=received_at_utc,
        source_payload_sha256=raw_sha,
    )
    # The existing completed-session parser defaults its upper bound to 16:00.
    # Intraday evidence instead records the actual request cutoff.
    return replace(cache, request_end_et=requested_until_et), skipped


def acquire_live_ohlcv(
    client: Any, *, symbol: str, session_date: date,
    now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LiveOHLCVAcquisition:
    """One bounded request with an injected client; no credentials or scheduler.

    Caller owns the SDK client and must not invoke this function from the
    read-only viewer or today's production quote polling process.
    """
    selected = _symbol(symbol)
    started = _utc(now_factory())
    if started.astimezone(ET).date() != session_date:
        raise LiveOHLCVError("Live OHLCV requires today's exact Eastern session")
    start_et, end_et = request_bounds(session_date)
    requested_until_et = min(started.astimezone(ET), end_et)
    if requested_until_et <= start_et:
        raise LiveOHLCVNotReady("Price-history session has not begun")
    try:
        response = client.price_history(
            selected, frequencyType="minute", frequency=5,
            startDate=start_et, endDate=requested_until_et,
            needExtendedHoursData=True, needPreviousClose=False,
        )
    except Exception as error:
        raise ObservationOverlayRequestError(
            f"live price_history failed: {type(error).__name__}: {error}"
        ) from error
    received = _utc(now_factory())
    if received < started or received.astimezone(ET).date() != session_date:
        raise LiveOHLCVError("Live OHLCV response clock regressed or crossed session date")
    if not bool(getattr(response, "ok", False)):
        raise ObservationOverlayRequestError(
            f"live price_history HTTP {_response_status(response)}"
        )
    payload, raw = _read_response_payload(response)
    cache, skipped = _build_closed_cache(
        payload, symbol=selected, session_date=session_date,
        requested_until_et=requested_until_et, received_at_utc=received,
        raw_sha=hashlib.sha256(raw).hexdigest(),
    )
    return LiveOHLCVAcquisition(
        cache, raw, started, received, skipped, _response_status(response),
    )


def publish_live_ohlcv(
    root: str | Path, acquisition: LiveOHLCVAcquisition,
) -> Path:
    """Publish a complete immutable raw+cache bundle with one atomic rename.

    Root is explicitly provided by a separate authorized producer. No writing
    into a quote-observation SQLite journal or daily controller path occurs.
    """
    cache = acquisition.cache
    symbol = _symbol(cache.symbol)
    received = _utc(acquisition.response_received_at_utc)
    if received != cache.acquired_at_utc or received.astimezone(ET).date() != cache.session_date:
        raise LiveOHLCVError("Live acquisition and cache timestamps differ")
    if hashlib.sha256(acquisition.raw_payload).hexdigest() != cache.source_payload_sha256:
        raise LiveOHLCVError("Live raw payload digest does not match cache")
    requested_end = cache.request_end_et
    if requested_end > received.astimezone(ET):
        raise LiveOHLCVError("Live cache cutoff is later than response receipt")
    payload = json.loads(acquisition.raw_payload)
    expected, _ = _build_closed_cache(
        payload, symbol=symbol, session_date=cache.session_date,
        requested_until_et=requested_end,
        received_at_utc=received, raw_sha=cache.source_payload_sha256,
    )
    if expected != cache:
        raise LiveOHLCVError("Live OHLCV cache differs from raw Schwab evidence")
    dated = Path(root).resolve() / cache.session_date.isoformat() / symbol
    dated.mkdir(parents=True, exist_ok=True)
    name = (
        received.strftime("%Y%m%dT%H%M%S%fZ")
        + "-" + cache.source_payload_sha256[:16]
    )
    destination = dated / name
    if destination.exists():
        raise FileExistsError(f"Immutable live OHLCV bundle already exists: {destination}")
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=dated))
    try:
        (staging / BUNDLE_RAW).write_bytes(acquisition.raw_payload)
        write_observation_overlay_cache(staging / BUNDLE_CACHE, cache)
        os.rename(staging, destination)
    finally:
        if staging.exists():
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
    return destination


class LiveOHLCVReader:
    """Read only the newest complete bundle for an exact day and symbol.

    Never authenticates, calls Schwab, writes journals, or imports credentials.
    An invalid newest bundle is an error, not a reason to silently show older
    evidence as though the latest acquisition succeeded.
    """

    def __init__(self, root: str | Path, *, session_date: date) -> None:
        if not isinstance(session_date, date):
            raise TypeError("session_date must be a date")
        self.root = Path(root).resolve()
        self.session_date = session_date
        self._memo: dict[str, tuple[Path, ObservationOverlayOHLCVCache]] = {}

    def latest(
        self, symbol: str, *, now: datetime | None = None,
    ) -> ObservationOverlayOHLCVCache | None:
        selected = _symbol(symbol)
        cutoff = _utc(now or datetime.now(UTC))
        if cutoff.astimezone(ET).date() != self.session_date:
            raise LiveOHLCVError("Live OHLCV read outside the exact Eastern session")
        folder = self.root / self.session_date.isoformat() / selected
        if not folder.is_dir():
            return None
        candidates = sorted(
            (entry for entry in folder.iterdir()
             if entry.is_dir() and BUNDLE_PATTERN.fullmatch(entry.name)),
            key=lambda entry: entry.name,
        )
        if not candidates:
            return None
        newest = candidates[-1]
        cached = self._memo.get(selected)
        if cached is not None and cached[0] == newest:
            return cached[1]
        raw = (newest / BUNDLE_RAW).read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        cache = load_observation_overlay_cache(newest / BUNDLE_CACHE)
        name = BUNDLE_PATTERN.fullmatch(newest.name)
        assert name is not None
        expected_time = cache.acquired_at_utc.strftime("%Y%m%dT%H%M%S%fZ")
        if (
            name.group(1) != expected_time
            or name.group(2) != source_hash[:16]
            or cache.source_payload_sha256 != source_hash
            or cache.symbol != selected
            or cache.session_date != self.session_date
            or cache.acquired_at_utc > cutoff
            or cache.source != "schwabdev.Client.price_history"
        ):
            raise LiveOHLCVError("Live OHLCV bundle provenance/identity mismatch")
        payload = json.loads(raw)
        expected, _ = _build_closed_cache(
            payload, symbol=selected, session_date=self.session_date,
            requested_until_et=cache.request_end_et,
            received_at_utc=cache.acquired_at_utc, raw_sha=source_hash,
        )
        if expected != cache:
            raise LiveOHLCVError("Live OHLCV cache does not match raw Schwab evidence")
        self._memo[selected] = (newest, cache)
        return cache
