"""Acquire immutable Schwab five-minute evidence for Observation Overlay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from mb_market_data.observation_overlay import (
    ET,
    OHLCV_FREQUENCY_MINUTES,
    ObservationOverlayOHLCVCache,
    OverlayCandle,
    sha256_file,
    write_observation_overlay_cache,
)


UTC = timezone.utc
ACQUISITION_VERSION = "observation-overlay-acquisition-v1"
REQUEST_START = time(0, 0)
REQUEST_END = time(16, 0)
SOURCE_METHOD = "schwabdev.Client.price_history"


class ObservationOverlayAcquisitionError(RuntimeError):
    """Base error for one-symbol OO OHLCV acquisition."""


class ObservationOverlayRequestError(ObservationOverlayAcquisitionError):
    """The Schwab price-history request did not produce readable JSON."""


class ObservationOverlayDataError(ObservationOverlayAcquisitionError):
    """The Schwab payload cannot form an unambiguous OO cache."""


@dataclass(frozen=True, slots=True)
class ObservationOverlayAcquisition:
    """One successful request plus its normalized immutable cache."""

    cache: ObservationOverlayOHLCVCache
    raw_payload: bytes
    request_started_at_utc: datetime
    response_received_at_utc: datetime
    http_status: int | None


@dataclass(frozen=True, slots=True)
class ObservationOverlayAcquisitionArtifacts:
    """Paths in one immutable OO acquisition bundle."""

    root: Path
    raw_payload: Path
    cache: Path
    manifest: Path


def request_bounds(session_date: date) -> tuple[datetime, datetime]:
    """Return the fixed one-session MVP request interval in ET."""

    if not isinstance(session_date, date):
        raise TypeError("session_date must be a date")
    return (
        datetime.combine(session_date, REQUEST_START, tzinfo=ET),
        datetime.combine(session_date, REQUEST_END, tzinfo=ET),
    )


def validate_completed_session(
    session_date: date,
    now_et: datetime,
) -> None:
    """Reject current/future sessions whose full candle interval is open."""

    _, required_through = request_bounds(session_date)
    if not isinstance(now_et, datetime):
        raise TypeError("now_et must be a datetime")
    if now_et.tzinfo is None or now_et.utcoffset() is None:
        raise ValueError("now_et must be timezone-aware")
    observed = now_et.astimezone(ET)
    if observed < required_through:
        raise ValueError(
            "Observation Overlay acquisition requires a completed session; "
            f"wait until {required_through.isoformat()}"
        )


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("symbol must be nonblank text")
    return value.strip().upper()


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _response_status(response: Any) -> int | None:
    value = getattr(response, "status_code", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _canonical_payload(payload: Any) -> bytes:
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ObservationOverlayRequestError(
            f"price-history JSON is not serializable: {error}"
        ) from error
    return (text + "\n").encode("utf-8")


def _read_response_payload(response: Any) -> tuple[Mapping[str, Any], bytes]:
    content = getattr(response, "content", None)
    if isinstance(content, bytes) and content:
        raw_payload = content
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ObservationOverlayRequestError(
                f"price-history response body is not valid JSON: {error}"
            ) from error
    else:
        try:
            payload = response.json()
        except Exception as error:
            raise ObservationOverlayRequestError(
                f"price-history response has unreadable JSON: {error}"
            ) from error
        raw_payload = _canonical_payload(payload)
    if not isinstance(payload, Mapping):
        raise ObservationOverlayRequestError(
            "price-history response must contain one JSON object"
        )
    return payload, raw_payload


def _epoch_ms_to_et(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationOverlayDataError(
            "candle datetime must be numeric epoch milliseconds"
        )
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ObservationOverlayDataError("candle datetime must be finite")
    return datetime.fromtimestamp(numeric / 1000.0, tz=ET)


def _number(candle: Mapping[str, Any], name: str) -> float:
    value = candle.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationOverlayDataError(f"candle {name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ObservationOverlayDataError(f"candle {name} must be finite")
    return result


def _volume(candle: Mapping[str, Any]) -> int:
    value = candle.get("volume")
    if isinstance(value, bool):
        raise ObservationOverlayDataError(
            "candle volume must be a nonnegative integer"
        )
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    else:
        raise ObservationOverlayDataError(
            "candle volume must be a nonnegative integer"
        )
    if result < 0:
        raise ObservationOverlayDataError(
            "candle volume must be a nonnegative integer"
        )
    return result


def parse_observation_overlay_payload(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    session_date: date,
    acquired_at_utc: datetime,
    source_payload_sha256: str,
) -> ObservationOverlayOHLCVCache:
    """Normalize one Schwab response into the strict OO cache schema."""

    normalized_symbol = _symbol(symbol)
    if not isinstance(payload, Mapping):
        raise ObservationOverlayDataError(
            "price-history response must contain one JSON object"
        )
    payload_symbol = payload.get("symbol")
    if (
        payload_symbol is not None
        and str(payload_symbol).strip().upper() != normalized_symbol
    ):
        raise ObservationOverlayDataError(
            "price-history symbol mismatch: "
            f"requested {normalized_symbol}, received {payload_symbol!r}"
        )
    raw_candles = payload.get("candles")
    if not isinstance(raw_candles, list):
        raise ObservationOverlayDataError(
            "price-history response has no candle list"
        )

    request_start, request_end = request_bounds(session_date)
    candles: list[OverlayCandle] = []
    seen_starts: set[datetime] = set()
    for raw_candle in raw_candles:
        if not isinstance(raw_candle, Mapping):
            raise ObservationOverlayDataError("candle must be a JSON object")
        start_et = _epoch_ms_to_et(raw_candle.get("datetime"))
        if not request_start <= start_et < request_end:
            continue
        if start_et in seen_starts:
            raise ObservationOverlayDataError(
                f"duplicate candle start: {start_et.isoformat()}"
            )
        seen_starts.add(start_et)
        try:
            candles.append(
                OverlayCandle(
                    symbol=normalized_symbol,
                    start_et=start_et,
                    open=_number(raw_candle, "open"),
                    high=_number(raw_candle, "high"),
                    low=_number(raw_candle, "low"),
                    close=_number(raw_candle, "close"),
                    volume=_volume(raw_candle),
                )
            )
        except (TypeError, ValueError) as error:
            raise ObservationOverlayDataError(str(error)) from error
    candles.sort(key=lambda item: item.start_et)
    if not candles:
        raise ObservationOverlayDataError(
            f"no five-minute candles returned for {normalized_symbol} "
            f"on {session_date}"
        )
    return ObservationOverlayOHLCVCache(
        symbol=normalized_symbol,
        session_date=session_date,
        provider="Schwab",
        source=SOURCE_METHOD,
        acquired_at_utc=_utc(acquired_at_utc, "acquired_at_utc"),
        request_start_et=request_start,
        request_end_et=request_end,
        source_payload_sha256=source_payload_sha256,
        candles=tuple(candles),
    )


def acquire_observation_overlay_ohlcv(
    client: Any,
    *,
    symbol: str,
    session_date: date,
    now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ObservationOverlayAcquisition:
    """Acquire one completed session without authenticating or persisting."""

    normalized_symbol = _symbol(symbol)
    request_start, request_end = request_bounds(session_date)
    request_started = _utc(now_factory(), "now_factory result")
    try:
        response = client.price_history(
            normalized_symbol,
            frequencyType="minute",
            frequency=OHLCV_FREQUENCY_MINUTES,
            startDate=request_start,
            endDate=request_end,
            needExtendedHoursData=True,
            needPreviousClose=False,
        )
    except Exception as error:
        raise ObservationOverlayRequestError(
            f"price_history request failed: {type(error).__name__}: {error}"
        ) from error
    response_received = _utc(now_factory(), "now_factory result")
    status = _response_status(response)
    if not bool(getattr(response, "ok", False)):
        raise ObservationOverlayRequestError(
            "price_history returned HTTP "
            + (str(status) if status is not None else "unknown")
        )
    payload, raw_payload = _read_response_payload(response)
    payload_sha256 = hashlib.sha256(raw_payload).hexdigest()
    cache = parse_observation_overlay_payload(
        payload,
        symbol=normalized_symbol,
        session_date=session_date,
        acquired_at_utc=response_received,
        source_payload_sha256=payload_sha256,
    )
    return ObservationOverlayAcquisition(
        cache=cache,
        raw_payload=raw_payload,
        request_started_at_utc=request_started,
        response_received_at_utc=response_received,
        http_status=status,
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def write_observation_overlay_acquisition(
    output_dir: str | Path,
    acquisition: ObservationOverlayAcquisition,
) -> ObservationOverlayAcquisitionArtifacts:
    """Write one immutable raw/cache/manifest evidence bundle."""

    if not isinstance(acquisition, ObservationOverlayAcquisition):
        raise TypeError("acquisition must be ObservationOverlayAcquisition")
    if not acquisition.cache.candles:
        raise ObservationOverlayDataError(
            "acquisition cache must contain at least one candle"
        )
    raw_sha256 = hashlib.sha256(acquisition.raw_payload).hexdigest()
    if raw_sha256 != acquisition.cache.source_payload_sha256:
        raise ObservationOverlayDataError(
            "raw payload hash differs from cache provenance"
        )

    root = Path(output_dir)
    if root.exists():
        raise FileExistsError(f"output directory already exists: {root}")
    root.mkdir(parents=True)
    raw_path = root / "price_history_raw.json"
    cache_path = root / (
        f"{acquisition.cache.symbol}-{acquisition.cache.session_date}.json"
    )
    manifest_path = root / "manifest.json"

    with raw_path.open("xb") as output:
        output.write(acquisition.raw_payload)
    write_observation_overlay_cache(cache_path, acquisition.cache)
    written_raw_sha256 = sha256_file(raw_path)
    if written_raw_sha256 != raw_sha256:
        raise ObservationOverlayDataError(
            "written raw payload hash differs from acquisition bytes"
        )
    manifest = {
        "acquisition_version": ACQUISITION_VERSION,
        "provider": acquisition.cache.provider,
        "source": acquisition.cache.source,
        "symbol": acquisition.cache.symbol,
        "session_date": acquisition.cache.session_date.isoformat(),
        "frequency_minutes": acquisition.cache.frequency_minutes,
        "extended_hours": acquisition.cache.extended_hours,
        "request_start_et": acquisition.cache.request_start_et.isoformat(),
        "request_end_et": acquisition.cache.request_end_et.isoformat(),
        "request_started_at_utc": _utc_text(
            acquisition.request_started_at_utc
        ),
        "response_received_at_utc": _utc_text(
            acquisition.response_received_at_utc
        ),
        "http_status": acquisition.http_status,
        "candle_count": len(acquisition.cache.candles),
        "first_candle_et": acquisition.cache.candles[0].start_et.isoformat(),
        "last_candle_et": acquisition.cache.candles[-1].start_et.isoformat(),
        "artifacts": {
            "raw_payload": {
                "path": raw_path.name,
                "sha256": written_raw_sha256,
            },
            "cache": {
                "path": cache_path.name,
                "sha256": sha256_file(cache_path),
            },
        },
    }
    with manifest_path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(manifest, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")
    return ObservationOverlayAcquisitionArtifacts(
        root=root,
        raw_payload=raw_path,
        cache=cache_path,
        manifest=manifest_path,
    )
