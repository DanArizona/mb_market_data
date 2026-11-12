"""Acquire and preserve API-derived opening Overnight Volume evidence.

The production decision window is interpreted in America/New_York:

    00:00 <= candle start < 09:00

ThinkOrSwim is not an input.  One explicit result is retained for every
requested symbol, including request and data failures.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import time as time_module
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
UTC = timezone.utc
API_OV_VERSION = "schwab-price-history-ov-v1"
OV_WINDOW_START = time(0, 0)
OV_WINDOW_END = time(9, 0)
OV_FREQUENCY_MINUTES = 5
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class APIOvernightVolumeError(RuntimeError):
    """Base error for API Overnight Volume operations."""


class APIOvernightVolumeDataError(APIOvernightVolumeError):
    """A price-history payload contained unusable candle data."""


class APIOvernightVolumeStatus(str, Enum):
    """Terminal acquisition state for one requested symbol."""

    OK = "ok"
    REQUEST_ERROR = "request_error"
    RESPONSE_ERROR = "response_error"
    DATA_ERROR = "data_error"


@dataclass(frozen=True, slots=True)
class APIOvernightVolumeCandle:
    """One selected five-minute candle retained as calculation evidence."""

    start_et: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int


@dataclass(frozen=True, slots=True)
class APIOvernightVolumeObservation:
    """One symbol's complete API Overnight Volume acquisition result."""

    symbol: str
    trade_date: date
    window_start_et: datetime
    window_end_et: datetime
    status: APIOvernightVolumeStatus
    ov_decision: int | None
    candle_count: int
    attempts: int
    request_started_at_utc: datetime
    response_received_at_utc: datetime | None
    http_status: int | None
    detail: str | None
    candles: tuple[APIOvernightVolumeCandle, ...]

    @property
    def usable(self) -> bool:
        return (
            self.status == APIOvernightVolumeStatus.OK
            and self.ov_decision is not None
        )


@dataclass(frozen=True, slots=True)
class APIOvernightVolumeBatch:
    """Immutable complete result for one requested symbol roster."""

    trade_date: date
    window_start_et: datetime
    window_end_et: datetime
    started_at_utc: datetime
    completed_at_utc: datetime
    request_interval_seconds: float
    max_attempts: int
    observations: tuple[APIOvernightVolumeObservation, ...]

    @property
    def request_count(self) -> int:
        return sum(item.attempts for item in self.observations)

    @property
    def successful_count(self) -> int:
        return sum(item.usable for item in self.observations)

    @property
    def failed_count(self) -> int:
        return len(self.observations) - self.successful_count

    def status_counts(self) -> Counter[APIOvernightVolumeStatus]:
        return Counter(item.status for item in self.observations)


@dataclass(frozen=True, slots=True)
class APIOvernightVolumeArtifacts:
    """Paths written for one immutable acquisition bundle."""

    root: Path
    observations: Path
    candles: Path
    manifest: Path


def window_bounds(
    trade_date: date,
    *,
    window_end: time = OV_WINDOW_END,
) -> tuple[datetime, datetime]:
    """Return API OV acquisition bounds in Eastern Time.

    Production callers use the default 09:00 boundary.  Retrospective
    analysis may request a later boundary without changing production
    behavior.
    """

    if not isinstance(trade_date, date):
        raise TypeError("trade_date must be a date")
    if not isinstance(window_end, time):
        raise TypeError("window_end must be a time")
    if window_end <= OV_WINDOW_START:
        raise ValueError("window_end must follow the window start")
    return (
        datetime.combine(trade_date, OV_WINDOW_START, tzinfo=ET),
        datetime.combine(trade_date, window_end, tzinfo=ET),
    )


def normalize_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    """Normalize a nonempty, duplicate-free symbol roster."""

    normalized = tuple(str(symbol).strip().upper() for symbol in symbols)
    if not normalized or any(not symbol for symbol in normalized):
        raise ValueError("symbols must contain nonblank values")
    if len(normalized) != len(set(normalized)):
        raise ValueError("symbols contain duplicates")
    return normalized


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _epoch_ms_to_et(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise APIOvernightVolumeDataError(
            "candle datetime must be numeric epoch milliseconds"
        )
    numeric = float(value)
    if not math.isfinite(numeric):
        raise APIOvernightVolumeDataError("candle datetime must be finite")
    return datetime.fromtimestamp(numeric / 1000.0, tz=ET)


def _volume(value: Any) -> int:
    if isinstance(value, bool):
        raise APIOvernightVolumeDataError(
            "candle volume must be a nonnegative integer"
        )
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    else:
        raise APIOvernightVolumeDataError(
            "candle volume must be a nonnegative integer"
        )
    if result < 0:
        raise APIOvernightVolumeDataError(
            "candle volume must be a nonnegative integer"
        )
    return result


def _optional_number(candle: Mapping[str, Any], name: str) -> float | None:
    value = candle.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise APIOvernightVolumeDataError(
            f"candle {name} must be numeric when present"
        )
    result = float(value)
    if not math.isfinite(result):
        raise APIOvernightVolumeDataError(
            f"candle {name} must be finite when present"
        )
    return result


def parse_price_history_payload(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date,
    window_end: time = OV_WINDOW_END,
) -> tuple[tuple[APIOvernightVolumeCandle, ...], int]:
    """Select the decision-window candles and return their volume sum."""

    if not isinstance(payload, Mapping):
        raise APIOvernightVolumeDataError(
            "price-history response must be a JSON object"
        )
    payload_symbol = payload.get("symbol")
    if payload_symbol is not None and str(payload_symbol).strip().upper() != symbol:
        raise APIOvernightVolumeDataError(
            f"price-history symbol mismatch for {symbol}: {payload_symbol!r}"
        )
    raw_candles = payload.get("candles")
    if not isinstance(raw_candles, list):
        raise APIOvernightVolumeDataError(
            "price-history response has no candle list"
        )

    start_et, end_et = window_bounds(trade_date, window_end=window_end)
    selected: list[APIOvernightVolumeCandle] = []
    seen_starts: set[datetime] = set()
    for raw in raw_candles:
        if not isinstance(raw, Mapping):
            raise APIOvernightVolumeDataError("candle must be a JSON object")
        start = _epoch_ms_to_et(raw.get("datetime"))
        if not (start_et <= start < end_et):
            continue
        if start in seen_starts:
            raise APIOvernightVolumeDataError(
                f"duplicate candle start for {symbol}: {start.isoformat()}"
            )
        seen_starts.add(start)
        selected.append(
            APIOvernightVolumeCandle(
                start_et=start,
                open=_optional_number(raw, "open"),
                high=_optional_number(raw, "high"),
                low=_optional_number(raw, "low"),
                close=_optional_number(raw, "close"),
                volume=_volume(raw.get("volume")),
            )
        )
    selected.sort(key=lambda item: item.start_et)
    candles = tuple(selected)
    return candles, sum(item.volume for item in candles)


def _response_status(response: Any) -> int | None:
    value = getattr(response, "status_code", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def acquire_api_overnight_volume(
    client: Any,
    symbols: Sequence[str],
    *,
    trade_date: date,
    window_end: time = OV_WINDOW_END,
    request_interval_seconds: float = 0.5,
    max_attempts: int = 3,
    now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time_module.sleep,
) -> APIOvernightVolumeBatch:
    """Acquire one explicit price-history result for every symbol."""

    roster = normalize_symbols(symbols)
    if request_interval_seconds < 0:
        raise ValueError("request_interval_seconds cannot be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    start_et, end_et = window_bounds(trade_date, window_end=window_end)
    started_at = _aware_utc(now_factory(), "now_factory result")
    observations: list[APIOvernightVolumeObservation] = []
    first_request = True

    for symbol in roster:
        first_started: datetime | None = None
        last_received: datetime | None = None
        last_http_status: int | None = None
        detail: str | None = None
        status = APIOvernightVolumeStatus.REQUEST_ERROR
        candles: tuple[APIOvernightVolumeCandle, ...] = ()
        ov_decision: int | None = None
        attempts = 0

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            if not first_request and request_interval_seconds:
                sleep(request_interval_seconds)
            first_request = False
            request_started = _aware_utc(
                now_factory(), "now_factory result"
            )
            if first_started is None:
                first_started = request_started

            response = None
            try:
                response = client.price_history(
                    symbol,
                    frequencyType="minute",
                    frequency=OV_FREQUENCY_MINUTES,
                    startDate=start_et,
                    endDate=end_et,
                    needExtendedHoursData=True,
                    needPreviousClose=False,
                )
                last_received = _aware_utc(
                    now_factory(), "now_factory result"
                )
                last_http_status = _response_status(response)
                if not bool(getattr(response, "ok", False)):
                    detail = (
                        "price_history returned HTTP "
                        + (
                            str(last_http_status)
                            if last_http_status is not None
                            else "unknown"
                        )
                    )
                    if (
                        last_http_status in RETRYABLE_HTTP_STATUSES
                        and attempt < max_attempts
                    ):
                        retry_after = _retry_after_seconds(response)
                        if retry_after:
                            sleep(retry_after)
                        continue
                    status = APIOvernightVolumeStatus.REQUEST_ERROR
                    break

                try:
                    payload = response.json()
                except Exception as error:
                    status = APIOvernightVolumeStatus.RESPONSE_ERROR
                    detail = f"unreadable JSON response: {error}"
                    break
                try:
                    candles, ov_decision = parse_price_history_payload(
                        payload,
                        symbol=symbol,
                        trade_date=trade_date,
                        window_end=window_end,
                    )
                except APIOvernightVolumeDataError as error:
                    status = APIOvernightVolumeStatus.DATA_ERROR
                    detail = str(error)
                    break
                status = APIOvernightVolumeStatus.OK
                detail = None
                break
            except Exception as error:
                last_received = _aware_utc(
                    now_factory(), "now_factory result"
                )
                detail = f"{type(error).__name__}: {error}"
                status = APIOvernightVolumeStatus.REQUEST_ERROR
                if attempt >= max_attempts:
                    break

        assert first_started is not None
        observations.append(
            APIOvernightVolumeObservation(
                symbol=symbol,
                trade_date=trade_date,
                window_start_et=start_et,
                window_end_et=end_et,
                status=status,
                ov_decision=ov_decision,
                candle_count=len(candles),
                attempts=attempts,
                request_started_at_utc=first_started,
                response_received_at_utc=last_received,
                http_status=last_http_status,
                detail=detail,
                candles=candles,
            )
        )

    completed_at = _aware_utc(now_factory(), "now_factory result")
    return APIOvernightVolumeBatch(
        trade_date=trade_date,
        window_start_et=start_et,
        window_end_et=end_et,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        request_interval_seconds=request_interval_seconds,
        max_attempts=max_attempts,
        observations=tuple(observations),
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _candle_payload(candle: APIOvernightVolumeCandle) -> dict[str, Any]:
    return {
        "start_et": candle.start_et.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def write_api_overnight_volume_artifacts(
    output_dir: str | Path,
    *,
    opening_proposal_path: str | Path,
    opening_content_sha256: str,
    opening_uni_count: int,
    opening_effective_at: datetime,
    complete_opening_uni: bool,
    batch: APIOvernightVolumeBatch,
) -> APIOvernightVolumeArtifacts:
    """Write one immutable, hashed API OV evidence bundle."""

    root = Path(output_dir)
    opening_path = Path(opening_proposal_path).resolve()
    effective_at = _aware_utc(opening_effective_at, "opening_effective_at")
    if opening_effective_at.astimezone(ET).date() != batch.trade_date:
        raise ValueError("opening effective time is not on the trade date")
    if isinstance(opening_uni_count, bool) or not isinstance(
        opening_uni_count, int
    ):
        raise TypeError("opening_uni_count must be an integer")
    if opening_uni_count < 1:
        raise ValueError("opening_uni_count must be a positive integer")
    if not isinstance(complete_opening_uni, bool):
        raise TypeError("complete_opening_uni must be a boolean")
    requested_count = len(batch.observations)
    if requested_count > opening_uni_count:
        raise ValueError("requested symbols exceed opening Uni count")
    if complete_opening_uni and requested_count != opening_uni_count:
        raise ValueError(
            "complete opening-Uni evidence must request every opening symbol"
        )
    if root.exists():
        raise FileExistsError(f"output directory already exists: {root}")
    if not opening_path.is_file():
        raise FileNotFoundError(opening_path)
    root.mkdir(parents=True)
    observations_path = root / "api_ov_observations.csv"
    candles_path = root / "api_ov_candles.jsonl"
    manifest_path = root / "manifest.json"

    fields = [
        "symbol",
        "status",
        "ov_decision",
        "candle_count",
        "attempts",
        "window_start_et",
        "window_end_et",
        "request_started_at_utc",
        "response_received_at_utc",
        "http_status",
        "detail",
    ]
    with observations_path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in batch.observations:
            writer.writerow(
                {
                    "symbol": item.symbol,
                    "status": item.status.value,
                    "ov_decision": (
                        "" if item.ov_decision is None else item.ov_decision
                    ),
                    "candle_count": item.candle_count,
                    "attempts": item.attempts,
                    "window_start_et": item.window_start_et.isoformat(),
                    "window_end_et": item.window_end_et.isoformat(),
                    "request_started_at_utc": _utc_text(
                        item.request_started_at_utc
                    ),
                    "response_received_at_utc": _utc_text(
                        item.response_received_at_utc
                    ),
                    "http_status": (
                        "" if item.http_status is None else item.http_status
                    ),
                    "detail": item.detail or "",
                }
            )

    with candles_path.open("x", encoding="utf-8", newline="\n") as output:
        for item in batch.observations:
            payload = {
                "symbol": item.symbol,
                "status": item.status.value,
                "ov_decision": item.ov_decision,
                "candles": [_candle_payload(candle) for candle in item.candles],
            }
            output.write(json.dumps(payload, sort_keys=True) + "\n")

    counts = {
        status.value: count
        for status, count in sorted(
            batch.status_counts().items(), key=lambda item: item[0].value
        )
    }
    started_at_or_after_window_end = (
        _aware_utc(batch.started_at_utc, "batch.started_at_utc")
        >= batch.window_end_et.astimezone(UTC)
    )
    completed_before_opening = batch.completed_at_utc < effective_at
    production_eligible = (
        complete_opening_uni
        and requested_count == opening_uni_count
        and batch.failed_count == 0
        and started_at_or_after_window_end
        and completed_before_opening
    )
    manifest = {
        "production_version": API_OV_VERSION,
        "session_date": batch.trade_date.isoformat(),
        "window_start_et": batch.window_start_et.isoformat(),
        "window_end_et": batch.window_end_et.isoformat(),
        "frequency_minutes": OV_FREQUENCY_MINUTES,
        "started_at_utc": _utc_text(batch.started_at_utc),
        "completed_at_utc": _utc_text(batch.completed_at_utc),
        "request_interval_seconds": batch.request_interval_seconds,
        "max_attempts": batch.max_attempts,
        "request_count": batch.request_count,
        "requested_symbols": len(batch.observations),
        "successful_symbols": batch.successful_count,
        "failed_symbols": batch.failed_count,
        "status_counts": counts,
        "opening_uni_count": opening_uni_count,
        "complete_opening_uni": complete_opening_uni,
        "started_at_or_after_window_end": started_at_or_after_window_end,
        "completed_before_opening": completed_before_opening,
        "production_eligible": production_eligible,
        "opening": {
            "path": str(opening_path),
            "sha256": sha256_file(opening_path),
            "content_sha256": opening_content_sha256,
            "effective_at": opening_effective_at.isoformat(),
        },
        "artifacts": {
            "api_ov_observations": {
                "path": observations_path.name,
                "sha256": sha256_file(observations_path),
            },
            "api_ov_candles": {
                "path": candles_path.name,
                "sha256": sha256_file(candles_path),
            },
        },
    }
    with manifest_path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(manifest, output, indent=2, sort_keys=True)
        output.write("\n")

    return APIOvernightVolumeArtifacts(
        root=root,
        observations=observations_path,
        candles=candles_path,
        manifest=manifest_path,
    )
